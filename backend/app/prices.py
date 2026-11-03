import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Company, PriceQuote, TargetAlert, TargetRevision
from .notifications import create_dry_run_outbox

logger = logging.getLogger(__name__)
INDIA_TZ = ZoneInfo("Asia/Kolkata")
MAX_QUOTE_AGE = timedelta(minutes=15)
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
        )


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
        and timedelta(0) <= current_time - normalize_utc(quote.quoted_at) <= MAX_QUOTE_AGE
    )
    return {
        "id": quote.id,
        "exchange": quote.exchange,
        "symbol": quote.symbol,
        "price": float(quote.price),
        "currency": quote.currency,
        "provider": quote.provider,
        "quoted_at": normalize_utc(quote.quoted_at),
        "received_at": normalize_utc(quote.received_at),
        "is_simulated": quote.is_simulated,
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
    if current_time - quoted_at > MAX_QUOTE_AGE:
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
        quoted_at=quoted_at,
        received_at=current_time,
        is_simulated=quote.is_simulated,
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
