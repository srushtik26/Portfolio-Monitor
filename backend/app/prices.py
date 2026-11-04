import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic, sleep
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Company, PriceQuote, TargetAlert, TargetRevision
from .notifications import create_dry_run_outbox

logger = logging.getLogger(__name__)
INDIA_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_QUOTE_FRESHNESS_SECONDS = 15 * 60
MAX_QUOTE_AGE = timedelta(seconds=DEFAULT_QUOTE_FRESHNESS_SECONDS)
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class ProviderQuote:
    exchange: str
    symbol: str
    price: Decimal
    currency: str
    provider: str
    quoted_at: datetime
    is_simulated: bool
    quote_type: str = "last_trade"
    freshness_seconds: int = DEFAULT_QUOTE_FRESHNESS_SECONDS


class RateLimitError(Exception):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"Provider rate limit reached; retry after {retry_after_seconds:g} seconds.")
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class ProviderError(Exception):
    pass


class PriceProvider(Protocol):
    name: str
    is_simulated: bool

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote: ...


@dataclass(frozen=True)
class ExchangeSession:
    opens_at: time
    closes_at: time


class ExchangeCalendar(Protocol):
    name: str

    def session_for(self, exchange: str, session_date: date) -> ExchangeSession | None: ...


class FixturePriceProvider:
    """Explicit test/demo quotes. These are always simulated and never alert eligible."""

    name = "fixture"
    is_simulated = True

    def __init__(
        self,
        prices: dict[tuple[str, str], Decimal] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.prices = prices or {("NSE", "FIXTURE"): Decimal("100.00")}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
        key = (exchange, symbol)
        if key not in self.prices:
            raise ProviderError(f"No fixture quote is defined for {exchange}:{symbol}.")
        return ProviderQuote(
            exchange=exchange,
            symbol=symbol,
            price=self.prices[key],
            currency="INR",
            provider=self.name,
            quoted_at=self.clock(),
            is_simulated=True,
            quote_type="fixture",
        )


YAHOO_TICKER_MAP: dict[tuple[str, str], str] = {
    ("NSE", "TEGA"): "TEGA.NS",
}
YFINANCE_STALE_AFTER_SECONDS = 5 * 60
YFINANCE_CACHE_TTL_SECONDS = 60
YFINANCE_MAX_RETRIES = 3
YFINANCE_BACKOFF_BASE_SECONDS = 2
YFINANCE_BACKOFF_MAX_SECONDS = 30


def yahoo_ticker_for(exchange: str, symbol: str) -> str:
    identity = (exchange.strip().upper(), symbol.strip().upper())
    ticker = YAHOO_TICKER_MAP.get(identity)
    if ticker is None:
        raise ProviderError(
            f"No verified Yahoo Finance ticker mapping for {identity[0]}:{identity[1]}."
        )
    expected_suffix = {"NSE": ".NS", "BSE": ".BO"}.get(identity[0])
    if expected_suffix is None or not ticker.endswith(expected_suffix):
        raise ProviderError(f"Invalid Yahoo Finance ticker mapping for {identity[0]}:{identity[1]}.")
    return ticker


def _environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return parsed


class YFinanceProvider:
    """Unofficial Yahoo Finance 1-minute candle-close adapter, not guaranteed real time."""

    name = "yfinance"
    is_simulated = False
    quote_type = "candle_close"

    def __init__(
        self,
        *,
        history_fetcher: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        cache_ttl_seconds: int | None = None,
        stale_after_seconds: int | None = None,
    ) -> None:
        self._history_fetcher = history_fetcher or self._default_history_fetcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or sleep
        self.cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else _environment_integer(
                "YFINANCE_CACHE_TTL_SECONDS", YFINANCE_CACHE_TTL_SECONDS, 10, 3600
            )
        )
        self.stale_after_seconds = (
            stale_after_seconds
            if stale_after_seconds is not None
            else _environment_integer(
                "YFINANCE_STALE_AFTER_SECONDS", YFINANCE_STALE_AFTER_SECONDS, 60, 86400
            )
        )
        if not 10 <= self.cache_ttl_seconds <= 3600:
            raise ValueError("YFinance cache TTL must be between 10 and 3600 seconds.")
        if not 60 <= self.stale_after_seconds <= 86400:
            raise ValueError("YFinance stale threshold must be between 60 and 86400 seconds.")
        self._cache: dict[tuple[str, str], tuple[float, ProviderQuote]] = {}
        self._cache_lock = RLock()

    @staticmethod
    def _default_history_fetcher(ticker: str) -> object:
        import yfinance as yf

        yahoo_ticker = yf.Ticker(ticker)
        history = yahoo_ticker.history(
            period="1d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
            actions=False,
            repair=False,
            keepna=False,
            timeout=10,
        )
        if getattr(history, "empty", True):
            return history
        metadata = yahoo_ticker.get_history_metadata()
        currency = metadata.get("currency") if isinstance(metadata, dict) else None
        if isinstance(currency, str):
            history.attrs["currency"] = currency
        return history

    def _cached(self, identity: tuple[str, str]) -> ProviderQuote | None:
        cached = self._cache.get(identity)
        if cached is None:
            return None
        cached_at, quote = cached
        if monotonic() - cached_at > self.cache_ttl_seconds:
            self._cache.pop(identity, None)
            return None
        return quote

    def fetch_quote(self, exchange: str, symbol: str) -> ProviderQuote:
        identity = (exchange.strip().upper(), symbol.strip().upper())
        ticker = yahoo_ticker_for(*identity)
        with self._cache_lock:
            cached = self._cached(identity)
            if cached is not None:
                return cached
        last_error: Exception | None = None
        for attempt in range(YFINANCE_MAX_RETRIES):
            try:
                history = self._history_fetcher(ticker)
                quote = self._quote_from_history(identity, ticker, history)
                with self._cache_lock:
                    self._cache[identity] = (monotonic(), quote)
                return quote
            except ProviderError as error:
                if "No verified Yahoo Finance ticker mapping" in str(error):
                    raise
                last_error = error
                if attempt + 1 < YFINANCE_MAX_RETRIES:
                    self._sleep(
                        min(
                            YFINANCE_BACKOFF_BASE_SECONDS * (2**attempt),
                            YFINANCE_BACKOFF_MAX_SECONDS,
                        )
                    )
            except Exception as error:
                last_error = error
                if attempt + 1 < YFINANCE_MAX_RETRIES:
                    if "429" in str(error) or "ratelimit" in type(error).__name__.lower():
                        logger.warning("Yahoo Finance rate limit for %s; applying bounded backoff.", ticker)
                    self._sleep(
                        min(
                            YFINANCE_BACKOFF_BASE_SECONDS * (2**attempt),
                            YFINANCE_BACKOFF_MAX_SECONDS,
                        )
                    )
        raise ProviderError(
            f"Yahoo Finance fetch failed for {ticker} after {YFINANCE_MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    def _quote_from_history(
        self,
        identity: tuple[str, str],
        ticker: str,
        history: object,
    ) -> ProviderQuote:
        if history is None or getattr(history, "empty", True):
            raise ProviderError(f"Yahoo Finance returned no 1-minute bars for {ticker}.")
        try:
            row_count = len(history.index)
        except (AttributeError, TypeError) as error:
            raise ProviderError(f"Yahoo Finance response for {ticker} has no usable candle index.") from error
        if row_count == 0:
            raise ProviderError(f"Yahoo Finance returned no 1-minute bars for {ticker}.")

        latest_error: Exception | None = None
        price: Decimal | None = None
        quoted_at: datetime | None = None
        skipped_bars = 0
        for position in range(row_count - 1, -1, -1):
            try:
                row = history.iloc[position]
                close = row["Close"]
                raw_timestamp = history.index[position]
                timestamp = (
                    raw_timestamp.to_pydatetime()
                    if hasattr(raw_timestamp, "to_pydatetime")
                    else raw_timestamp
                )
                if (
                    not isinstance(timestamp, datetime)
                    or timestamp.tzinfo is None
                    or timestamp.utcoffset() is None
                ):
                    raise ProviderError(
                        f"Yahoo Finance candle timestamp for {ticker} is missing a timezone."
                    )
                parsed_price = _validated_price(Decimal(str(close)))
            except (InvalidOperation, ValueError, KeyError, IndexError, TypeError) as error:
                latest_error = error
                skipped_bars += 1
                continue
            price = parsed_price
            quoted_at = timestamp.astimezone(timezone.utc)
            break
        if price is None or quoted_at is None:
            raise ProviderError(
                f"Yahoo Finance response for {ticker} has no valid close with a timezone-aware timestamp: "
                f"{latest_error}"
            ) from latest_error
        if skipped_bars:
            logger.warning(
                "Skipped %d invalid Yahoo Finance candle(s) for %s; using the last valid bar at %s.",
                skipped_bars,
                ticker,
                quoted_at.isoformat(),
            )
        currency = getattr(history, "attrs", {}).get("currency")
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise ProviderError(f"Yahoo Finance response for {ticker} has no valid currency.")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("YFinanceProvider clock must return a timezone-aware datetime.")
        current_time = now.astimezone(timezone.utc)
        if quoted_at > current_time:
            raise ProviderError(f"Yahoo Finance returned a future candle timestamp for {ticker}.")
        if current_time - quoted_at > timedelta(seconds=self.stale_after_seconds):
            logger.warning(
                "Yahoo Finance candle for %s is stale (timestamp=%s, threshold=%ss).",
                ticker,
                quoted_at.isoformat(),
                self.stale_after_seconds,
            )
        return ProviderQuote(
            exchange=identity[0],
            symbol=identity[1],
            price=price,
            currency=currency,
            provider="Yahoo Finance (unofficial)",
            quoted_at=quoted_at,
            is_simulated=False,
            quote_type=self.quote_type,
            freshness_seconds=self.stale_after_seconds,
        )

    def fetch_quotes(
        self, instruments: list[tuple[str, str]]
    ) -> dict[tuple[str, str], ProviderQuote]:
        # yfinance's download API batches tickers, but only use explicitly verified mappings.
        mapped = {
            (exchange.strip().upper(), symbol.strip().upper()): yahoo_ticker_for(
                exchange, symbol
            )
            for exchange, symbol in instruments
        }
        uncached = []
        results: dict[tuple[str, str], ProviderQuote] = {}
        with self._cache_lock:
            for identity in mapped:
                quote = self._cached(identity)
                if quote is not None:
                    results[identity] = quote
                else:
                    uncached.append(identity)
        if not uncached:
            return results
        if len(uncached) == 1:
            identity = uncached[0]
            results[identity] = self.fetch_quote(*identity)
            return results
        last_error: Exception | None = None
        for attempt in range(YFINANCE_MAX_RETRIES):
            try:
                import yfinance as yf

                frame = yf.download(
                    tickers=" ".join(mapped[identity] for identity in uncached),
                    period="1d",
                    interval="1m",
                    group_by="ticker",
                    auto_adjust=False,
                    prepost=False,
                    actions=False,
                    repair=False,
                    keepna=False,
                    threads=False,
                    progress=False,
                    timeout=10,
                )
                break
            except Exception as error:
                last_error = error
                if attempt + 1 == YFINANCE_MAX_RETRIES:
                    raise ProviderError(
                        f"Yahoo Finance batch fetch failed after {YFINANCE_MAX_RETRIES} attempts: {error}"
                    ) from error
                if "429" in str(error) or "ratelimit" in type(error).__name__.lower():
                    logger.warning("Yahoo Finance batch rate limit; applying bounded backoff.")
                self._sleep(
                    min(
                        YFINANCE_BACKOFF_BASE_SECONDS * (2**attempt),
                        YFINANCE_BACKOFF_MAX_SECONDS,
                    )
                )
        else:
            raise ProviderError(f"Yahoo Finance batch fetch failed: {last_error}") from last_error
        for identity in uncached:
            ticker = mapped[identity]
            try:
                ticker_frame = frame[ticker]
            except (KeyError, TypeError) as error:
                raise ProviderError(f"Yahoo Finance batch response omitted {ticker}.") from error
            quote = self._quote_from_history(identity, ticker, ticker_frame)
            results[identity] = quote
            with self._cache_lock:
                self._cache[identity] = (monotonic(), quote)
        return results


class FixtureExchangeCalendar:
    """Test-only calendar; its dates are not an authoritative exchange schedule."""

    name = "fixture"
    regular_session = ExchangeSession(time(9, 15), time(15, 30))
    holidays = frozenset({date(2026, 1, 1)})
    special_sessions = {
        date(2026, 11, 8): ExchangeSession(time(18, 0), time(19, 0)),
    }

    def session_for(self, exchange: str, session_date: date) -> ExchangeSession | None:
        if exchange not in {"NSE", "BSE"}:
            return None
        if session_date in self.special_sessions:
            return self.special_sessions[session_date]
        if session_date.weekday() >= 5 or session_date in self.holidays:
            return None
        return self.regular_session

    def is_open(self, exchange: str, at: datetime) -> bool:
        local_time = at.astimezone(INDIA_TZ)
        session = self.session_for(exchange, local_time.date())
        return session is not None and session.opens_at <= local_time.time() <= session.closes_at


def normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_quote(quote: PriceQuote, now: datetime | None = None) -> dict[str, object]:
    current_time = normalize_utc(now or datetime.now(timezone.utc))
    is_fresh = (
        quote.status == "fresh"
        and timedelta(0)
        <= current_time - normalize_utc(quote.quoted_at)
        <= timedelta(seconds=quote.freshness_seconds)
    )
    return {
        "id": quote.id,
        "exchange": quote.exchange,
        "symbol": quote.symbol,
        "price": float(quote.price),
        "currency": quote.currency,
        "provider": quote.provider,
        "quote_type": quote.quote_type,
        "quoted_at": normalize_utc(quote.quoted_at),
        "received_at": normalize_utc(quote.received_at),
        "is_simulated": quote.is_simulated,
        "freshness_seconds": quote.freshness_seconds,
        "status": quote.status,
        "is_fresh": is_fresh,
    }


def _validated_price(raw_price: Decimal) -> Decimal:
    try:
        price = Decimal(str(raw_price))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Provider returned a non-numeric price.") from error
    if not price.is_finite() or price <= 0 or price >= Decimal("1000000000000"):
        raise ValueError("Provider returned a price outside the supported positive range.")
    if price.as_tuple().exponent < -6:
        raise ValueError("Provider returned a price with more than six decimal places.")
    return price


def record_quote(
    session: Session,
    quote: ProviderQuote,
    *,
    now: datetime | None = None,
    alerts_enabled: bool = False,
    fixture_alerts_enabled: bool = False,
) -> PriceQuote:
    current_time = normalize_utc(now or datetime.now(timezone.utc))
    identity = (quote.exchange.upper(), quote.symbol.upper())
    company = session.get(Company, identity)
    if company is None:
        raise ValueError(f"Provider returned an untracked company: {identity[0]}:{identity[1]}.")
    if identity != (quote.exchange, quote.symbol):
        raise ValueError("Provider quote identity must use normalized exchange and symbol values.")
    if quote.provider.strip() == "":
        raise ValueError("Provider name must not be blank.")
    if not _CURRENCY.fullmatch(quote.currency):
        raise ValueError("Provider currency must be a three-letter uppercase code.")
    if not isinstance(quote.is_simulated, bool):
        raise ValueError("Quote simulation status is invalid.")
    if quote.quote_type not in {"last_trade", "candle_close", "fixture", "provider_quote"}:
        raise ValueError("Quote type is invalid.")
    if not isinstance(quote.freshness_seconds, int) or quote.freshness_seconds < 1:
        raise ValueError("Quote freshness threshold must be a positive number of seconds.")
    price = _validated_price(quote.price)
    if quote.quoted_at.tzinfo is None or quote.quoted_at.utcoffset() is None:
        raise ValueError("Provider quote time must include a timezone.")
    quoted_at = normalize_utc(quote.quoted_at)
    if quoted_at > current_time:
        raise ValueError("Provider quote time must not be in the future.")

    last_quote = session.scalar(
        select(PriceQuote)
        .where(
            PriceQuote.exchange == identity[0],
            PriceQuote.symbol == identity[1],
        )
        .order_by(PriceQuote.quoted_at.desc(), PriceQuote.id.desc())
        .limit(1)
    )
    status = "fresh"
    if current_time - quoted_at > timedelta(seconds=quote.freshness_seconds):
        status = "stale"
    elif last_quote is not None and quoted_at <= normalize_utc(last_quote.quoted_at):
        status = "out_of_order"
    elif quote.is_simulated:
        status = "simulated"

    recorded = PriceQuote(
        exchange=identity[0],
        symbol=identity[1],
        price=price,
        currency=quote.currency,
        provider=quote.provider,
        quote_type=quote.quote_type,
        quoted_at=quoted_at,
        received_at=current_time,
        is_simulated=quote.is_simulated,
        freshness_seconds=quote.freshness_seconds,
        status=status,
    )
    session.add(recorded)
    session.flush()

    alert_eligible = (
        alerts_enabled and status == "fresh" and not quote.is_simulated
    ) or (
        fixture_alerts_enabled and status == "simulated" and quote.is_simulated
    )
    if alert_eligible:
        revision = session.scalar(
            select(TargetRevision).where(
                TargetRevision.exchange == identity[0],
                TargetRevision.symbol == identity[1],
                TargetRevision.version == company.version,
            )
        )
        if revision is not None:
            for kind, target in (
                ("base", company.base_target),
                ("bull", company.bull_target),
            ):
                if target is None or price < target:
                    continue
                try:
                    with session.begin_nested():
                        event = TargetAlert(
                            target_revision_id=revision.id,
                            target_kind=kind,
                            quote_id=recorded.id,
                            triggered_at=current_time,
                            price=price,
                            is_simulated=quote.is_simulated,
                        )
                        session.add(event)
                        session.flush()
                        create_dry_run_outbox(
                            session,
                            event,
                            now=current_time,
                        )
                except IntegrityError:
                    logger.info(
                        "Target alert already exists for revision %s (%s).",
                        revision.id,
                        kind,
                    )

    session.commit()
    session.refresh(recorded)
    return recorded
