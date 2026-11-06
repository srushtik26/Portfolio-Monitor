import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic, sleep
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AuditLog,
    Company,
    PriceQuote,
    TargetAlert,
    TargetAlertState,
    TargetRevision,
)
from .notifications import queue_alert_notifications

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
    """Explicit test/demo quotes. These are always simulated and never live-alert eligible."""

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


_VERIFIED_INDIAN_STOCKS_NSE = (
    "TEGA",
    "BHEL",
    "RELIANCE",
    "HDFCBANK",
    "TCS",
    "ICICIBANK",
    "INFY",
    "BHARTIARTL",
    "ITC",
    "LT",
    "SBIN",
    "HINDUNILVR",
    "AXISBANK",
    "WIPRO",
    "TATAMOTORS",
    "MARUTI",
    "ASIANPAINT",
    "BAJFINANCE",
    "SUNPHARMA",
    "TITAN",
    "ADANIPORTS",
    "KOTAKBANK",
    "NTPC",
    "POWERGRID",
    "ULTRACEMCO",
    "NESTLEIND",
    "HCLTECH",
    "TECHM",
    "M&M",
    "TATASTEEL",
    "JSWSTEEL",
    "COALINDIA",
    "DRREDDY",
    "CIPLA",
    "BAJAJ-AUTO",
    "EICHERMOT",
    "INDUSINDBK",
    "APOLLOHOSP",
    "TRENT",
    "ZOMATO",
    "BEL",
    "SBILIFE",
    "HAL",
    "ONGC",
    "ADANIENT",
    "PFC",
    "RECLTD",
    "IRCTC",
    "POLYCAB",
    "DIXON",
    "SIEMENS",
    "ABB",
    "PIDILITIND",
    "GRASIM",
    "HINDALCO",
    "VEDL",
    "SHREECEM",
    "BRITANNIA",
    "DIVISLAB",
    "TATACONSUM",
    "HEROMOTOCO",
    "BPCL",
    "IOC",
    "GAIL",
)

_VERIFIED_INDIAN_STOCKS_BSE = (
    "BHEL",
    "RELIANCE",
    "HDFCBANK",
    "TCS",
    "ICICIBANK",
    "INFY",
    "SBIN",
    "LT",
    "TRENT",
    "SBILIFE",
    "BEL",
    "TATAMOTORS",
    "TATASTEEL",
    "ITC",
    "ZOMATO",
    "HAL",
    "NTPC",
    "POWERGRID",
    "COALINDIA",
    "ONGC",
)

YAHOO_TICKER_MAP: dict[tuple[str, str], str] = {
    **{("NSE", sym): f"{sym}.NS" for sym in _VERIFIED_INDIAN_STOCKS_NSE},
    **{("BSE", sym): f"{sym}.BO" for sym in _VERIFIED_INDIAN_STOCKS_BSE},
}

YFINANCE_STALE_AFTER_SECONDS = 5 * 60
YFINANCE_CACHE_TTL_SECONDS = 60
YFINANCE_MAX_RETRIES = 3
YFINANCE_BACKOFF_BASE_SECONDS = 2
YFINANCE_BACKOFF_MAX_SECONDS = 30


def register_verified_ticker(exchange: str, symbol: str) -> str:
    """Register an approved company symbol in the explicit Yahoo Finance ticker map."""
    identity = (exchange.strip().upper(), symbol.strip().upper())
    suffix = {"NSE": ".NS", "BSE": ".BO"}.get(identity[0])
    if suffix is None:
        raise ProviderError(f"Unsupported exchange for ticker mapping: {identity[0]}")
    ticker = f"{identity[1]}{suffix}"
    YAHOO_TICKER_MAP[identity] = ticker
    return ticker


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
    """Yahoo Finance 1-minute candle-close adapter with batching, caching, and bounded retries."""

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
            history = yahoo_ticker.history(
                period="5d",
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
                if not hasattr(ticker_frame, "attrs") or "currency" not in ticker_frame.attrs:
                    if hasattr(ticker_frame, "attrs"):
                        ticker_frame.attrs["currency"] = "INR"
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


class NseBseExchangeCalendar:
    """Authoritative NSE & BSE Indian equity market calendar (Asia/Kolkata)."""

    name = "nse_bse"
    regular_session = ExchangeSession(time(9, 15), time(15, 30))
    holidays = frozenset(
        {
            # 2026 NSE/BSE trading holidays
            date(2026, 1, 26),  # Republic Day
            date(2026, 2, 15),  # Maha Shivaratri
            date(2026, 3, 3),   # Holi
            date(2026, 3, 20),  # Id-Ul-Fitr
            date(2026, 3, 26),  # Shri Ram Navami
            date(2026, 3, 31),  # Shri Mahavir Jayanti
            date(2026, 4, 3),   # Good Friday
            date(2026, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
            date(2026, 5, 1),   # Maharashtra Day
            date(2026, 5, 27),  # Bakri Id
            date(2026, 6, 26),  # Muharram
            date(2026, 8, 15),  # Independence Day
            date(2026, 9, 14),  # Ganesh Chaturthi
            date(2026, 10, 2),  # Mahatma Gandhi Jayanti
            date(2026, 10, 20), # Dussehra
            date(2026, 11, 10), # Diwali Balipratipada
            date(2026, 11, 24), # Gurunanak Jayanti
            date(2026, 12, 25), # Christmas
        }
    )
    special_sessions = {
        date(2026, 11, 8): ExchangeSession(time(18, 0), time(19, 0)),  # Diwali Muhurat Trading
    }

    def __init__(self, *, allow_extended_hours: bool | None = None) -> None:
        self.allow_extended_hours = (
            allow_extended_hours
            if allow_extended_hours is not None
            else os.environ.get("MARKET_HOURS_OVERRIDE", "").strip().lower() == "always_open"
        )

    def session_for(self, exchange: str, session_date: date) -> ExchangeSession | None:
        if exchange not in {"NSE", "BSE"}:
            return None
        if self.allow_extended_hours:
            return ExchangeSession(time(0, 0), time(23, 59, 59))
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


def serialize_alert_state(
    company: Company,
    kind: str,
    state_row: TargetAlertState | None,
) -> dict[str, Any]:
    direction = company.base_direction if kind == "base" else company.bull_direction
    trigger_mode = company.base_trigger_mode if kind == "base" else company.bull_trigger_mode
    target_val = company.base_target if kind == "base" else company.bull_target
    if target_val is None or direction == "pending":
        effective_state = "pending_configuration"
    elif state_row is not None:
        effective_state = state_row.state
    else:
        effective_state = "armed"
    return {
        "target_kind": kind,
        "state": effective_state,
        "comparison_direction": direction,
        "trigger_mode": trigger_mode,
        "rearm_policy": company.rearm_policy,
        "last_evaluated_price": float(state_row.last_evaluated_price)
        if state_row and state_row.last_evaluated_price is not None
        else None,
        "last_evaluated_at": normalize_utc(state_row.last_evaluated_at)
        if state_row and state_row.last_evaluated_at
        else None,
        "last_condition_met": bool(state_row.last_condition_met) if state_row else False,
        "last_triggered_at": normalize_utc(state_row.last_triggered_at)
        if state_row and state_row.last_triggered_at
        else None,
        "last_triggered_price": float(state_row.last_triggered_price)
        if state_row and state_row.last_triggered_price is not None
        else None,
        "last_alert_id": state_row.last_alert_id if state_row else None,
        "trigger_cycle": state_row.trigger_cycle if state_row else 0,
        "acknowledged_at": normalize_utc(state_row.acknowledged_at)
        if state_row and state_row.acknowledged_at
        else None,
        "resolved_at": normalize_utc(state_row.resolved_at)
        if state_row and state_row.resolved_at
        else None,
        "reset_at": normalize_utc(state_row.reset_at)
        if state_row and state_row.reset_at
        else None,
    }


def enrich_company_dict(
    company: Company,
    company_data: dict[str, Any],
    quote_data: dict[str, Any] | None,
    base_state: TargetAlertState | None = None,
    bull_state: TargetAlertState | None = None,
) -> dict[str, Any]:
    company_data["latest_quote"] = quote_data
    base_state_dict = serialize_alert_state(company, "base", base_state)
    bull_state_dict = serialize_alert_state(company, "bull", bull_state)
    company_data["base_alert_state"] = base_state_dict
    company_data["bull_alert_state"] = bull_state_dict

    live_price: Decimal | None = None
    if quote_data is not None and quote_data.get("price") is not None:
        live_price = Decimal(str(quote_data["price"]))

    dist_base_abs: float | None = None
    dist_base_pct: float | None = None
    dist_bull_abs: float | None = None
    dist_bull_pct: float | None = None
    nearest_pct: float | None = None

    if live_price is not None:
        if company.base_target is not None and company.base_target > 0:
            diff_base = live_price - company.base_target
            dist_base_abs = round(float(diff_base), 2)
            dist_base_pct = round(float((diff_base / company.base_target) * Decimal("100")), 2)
        if company.bull_target is not None and company.bull_target > 0:
            diff_bull = live_price - company.bull_target
            dist_bull_abs = round(float(diff_bull), 2)
            dist_bull_pct = round(float((diff_bull / company.bull_target) * Decimal("100")), 2)
        candidates = [abs(x) for x in (dist_base_pct, dist_bull_pct) if x is not None]
        if candidates:
            nearest_pct = round(min(candidates), 2)

    company_data["distance_to_base_abs"] = dist_base_abs
    company_data["distance_to_base_pct"] = dist_base_pct
    company_data["distance_to_bull_abs"] = dist_bull_abs
    company_data["distance_to_bull_pct"] = dist_bull_pct
    company_data["nearest_target_pct"] = nearest_pct

    if not company.is_approved:
        alert_status = "pending_approval"
    elif not company.monitoring_active:
        alert_status = "paused"
    elif (
        (company.base_target is not None and company.base_direction == "pending")
        or (company.bull_target is not None and company.bull_direction == "pending")
    ):
        alert_status = "pending_configuration"
    elif base_state_dict["state"] == "triggered" and bull_state_dict["state"] == "triggered":
        alert_status = "both_triggered"
    elif base_state_dict["state"] == "triggered":
        alert_status = "base_triggered"
    elif bull_state_dict["state"] == "triggered":
        alert_status = "bull_triggered"
    elif quote_data is not None and not quote_data.get("is_fresh") and not quote_data.get("is_simulated"):
        alert_status = "stale_quote"
    elif nearest_pct is not None and nearest_pct <= 2.0:
        alert_status = "approaching"
    else:
        alert_status = "monitoring"

    company_data["alert_status"] = alert_status
    return company_data


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


def _evaluate_company_target(
    session: Session,
    *,
    company: Company,
    revision: TargetRevision,
    recorded_quote: PriceQuote,
    kind: str,
    current_time: datetime,
) -> TargetAlert | None:
    target = company.base_target if kind == "base" else company.bull_target
    direction = company.base_direction if kind == "base" else company.bull_direction
    trigger_mode = company.base_trigger_mode if kind == "base" else company.bull_trigger_mode
    price = recorded_quote.price
    is_simulated = recorded_quote.is_simulated

    state_key = (company.exchange, company.symbol, kind, is_simulated)
    state_row = session.get(TargetAlertState, state_key)
    if state_row is None:
        state_row = TargetAlertState(
            exchange=company.exchange,
            symbol=company.symbol,
            target_kind=kind,
            is_simulated=is_simulated,
            state="armed",
            target_revision_id=revision.id,
            last_condition_met=False,
            trigger_cycle=0,
        )
        session.add(state_row)
        session.flush()

    if state_row.target_revision_id != revision.id:
        state_row.target_revision_id = revision.id
        state_row.state = "armed"
        state_row.last_condition_met = False
        state_row.last_evaluated_price = None
        state_row.last_evaluated_at = None
        state_row.trigger_cycle = 0
        state_row.acknowledged_at = None
        state_row.resolved_at = None
        state_row.reset_at = None

    if target is None or direction == "pending":
        state_row.state = "pending_configuration"
        state_row.last_evaluated_price = price
        state_row.last_evaluated_at = current_time
        state_row.last_condition_met = False
        return None

    if state_row.state == "pending_configuration":
        state_row.state = "armed"

    prev_price = state_row.last_evaluated_price
    was_condition_met = state_row.last_condition_met

    if direction == "below":
        condition_met = price <= target
        crossed = prev_price is not None and prev_price > target and price <= target
    elif direction == "above":
        condition_met = price >= target
        crossed = prev_price is not None and prev_price < target and price >= target
    else:
        state_row.state = "pending_configuration"
        state_row.last_evaluated_price = price
        state_row.last_evaluated_at = current_time
        return None

    if trigger_mode == "crosses":
        qualifies = crossed
    else:
        qualifies = condition_met

    # Handle re-arming policies when condition recovers or cooldown expires
    if not condition_met and was_condition_met:
        if company.rearm_policy == "rearm_after_recovery" and state_row.state in (
            "triggered",
            "acknowledged",
            "resolved",
        ):
            state_row.state = "armed"

    if (
        company.rearm_policy == "cooldown"
        and state_row.state in ("triggered", "acknowledged", "resolved")
        and state_row.last_triggered_at is not None
    ):
        last_trig = normalize_utc(state_row.last_triggered_at)
        if current_time - last_trig >= timedelta(minutes=max(1, company.cooldown_minutes)):
            state_row.state = "armed"

    state_row.last_evaluated_price = price
    state_row.last_evaluated_at = current_time
    state_row.last_condition_met = condition_met

    if not qualifies or state_row.state != "armed":
        return None

    next_cycle = state_row.trigger_cycle + 1
    try:
        with session.begin_nested():
            event = TargetAlert(
                target_revision_id=revision.id,
                target_kind=kind,
                quote_id=recorded_quote.id,
                triggered_at=current_time,
                price=price,
                is_simulated=is_simulated,
                trigger_cycle=next_cycle,
                comparison_direction=direction,
                trigger_mode=trigger_mode,
                target_price_snapshot=target,
                lifecycle_status="active",
            )
            session.add(event)
            session.flush()
            queue_alert_notifications(
                session,
                event,
                now=current_time,
            )
            state_row.state = "triggered"
            state_row.trigger_cycle = next_cycle
            state_row.last_triggered_at = current_time
            state_row.last_triggered_price = price
            state_row.last_alert_id = event.id
            return event
    except IntegrityError:
        logger.info(
            "Target alert already exists for revision %s (%s, cycle %s).",
            revision.id,
            kind,
            next_cycle,
        )
        state_row.state = "triggered"
        return None


def apply_alert_lifecycle_action(
    session: Session,
    alert_id: int,
    action: str,
    *,
    actor: str = "Srushti Kadam",
    now: datetime | None = None,
) -> TargetAlert:
    current_time = normalize_utc(now or datetime.now(timezone.utc))
    alert = session.get(TargetAlert, alert_id)
    if alert is None:
        raise ValueError(f"Alert {alert_id} not found.")
    revision = session.get(TargetRevision, alert.target_revision_id)
    if revision is None:
        raise ValueError(f"Revision for alert {alert_id} not found.")
    state_key = (revision.exchange, revision.symbol, alert.target_kind, alert.is_simulated)
    state_row = session.get(TargetAlertState, state_key)

    if action == "acknowledge":
        alert.lifecycle_status = "acknowledged"
        alert.acknowledged_at = current_time
        if state_row is not None and state_row.state == "triggered":
            state_row.state = "acknowledged"
            state_row.acknowledged_at = current_time
    elif action == "resolve":
        alert.lifecycle_status = "resolved"
        alert.resolved_at = current_time
        if state_row is not None:
            company = session.get(Company, (revision.exchange, revision.symbol))
            if company and company.rearm_policy in ("rearm_on_reset", "rearm_after_recovery"):
                state_row.state = "armed"
                state_row.last_condition_met = False
            else:
                state_row.state = "resolved"
            state_row.resolved_at = current_time
    elif action == "reset":
        alert.lifecycle_status = "reset"
        alert.reset_at = current_time
        if state_row is not None:
            state_row.state = "armed"
            state_row.last_condition_met = False
            state_row.last_evaluated_price = None
            state_row.reset_at = current_time
    else:
        raise ValueError(f"Unsupported alert action: {action}")

    session.add(
        AuditLog(
            action=f"alert_{action}",
            actor=actor,
            entity_type="target_alert",
            entity_id=str(alert.id),
            details={
                "exchange": revision.exchange,
                "symbol": revision.symbol,
                "target_kind": alert.target_kind,
                "trigger_cycle": alert.trigger_cycle,
            },
            created_at=current_time,
        )
    )
    session.commit()
    session.refresh(alert)
    return alert


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
    if quote.quote_type not in {"last_trade", "candle_close", "fixture", "provider_quote", "websocket_tick"}:
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
        (
            alerts_enabled
            and status == "fresh"
            and not quote.is_simulated
            and company.is_approved
            and company.monitoring_active
        )
        or (
            fixture_alerts_enabled
            and status == "simulated"
            and quote.is_simulated
            and company.is_approved
            and company.monitoring_active
        )
    )
    if alert_eligible:
        revision = session.scalar(
            select(TargetRevision)
            .where(
                TargetRevision.exchange == identity[0],
                TargetRevision.symbol == identity[1],
                TargetRevision.version <= company.version,
            )
            .order_by(TargetRevision.version.desc(), TargetRevision.id.desc())
            .limit(1)
        )
        if revision is not None:
            for kind in ("base", "bull"):
                _evaluate_company_target(
                    session,
                    company=company,
                    revision=revision,
                    recorded_quote=recorded,
                    kind=kind,
                    current_time=current_time,
                )

    session.commit()
    session.refresh(recorded)
    return recorded
