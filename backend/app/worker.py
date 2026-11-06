import argparse
import logging
import os
import sys
import time as time_module
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Company, PriceQuote, WorkerHeartbeat
from .notifications import MultiChannelNotifier, drain_outbox
from .prices import (
    INDIA_TZ,
    ExchangeCalendar,
    FixtureExchangeCalendar,
    FixturePriceProvider,
    NseBseExchangeCalendar,
    PriceProvider,
    ProviderError,
    RateLimitError,
    YFinanceProvider,
    normalize_utc,
     record_quote,
    register_verified_ticker,
)

logger = logging.getLogger("portfolio.price_worker")
LOCK_PATH = Path(__file__).resolve().parents[1] / ".price-worker.lock"


@dataclass(frozen=True)
class PollResult:
    attempted: int
    recorded: int
    alerts_enabled: bool
    failed: int = 0


def _session_open(calendar: ExchangeCalendar, exchange: str, now: datetime) -> bool:
    local_now = now.astimezone(INDIA_TZ)
    trading_session = calendar.session_for(exchange, local_now.date())
    return (
        trading_session is not None
        and trading_session.opens_at <= local_now.time() <= trading_session.closes_at
    )


def poll_once(
    provider: PriceProvider,
    calendar: ExchangeCalendar,
    *,
    now: datetime | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> PollResult:
    if provider.name == "yfinance" and calendar.name == "fixture":
        raise ValueError(
            "Live provider polling requires an authoritative exchange calendar. "
            "Use app.yfinance_smoke for a one-off quote; it does not activate alerts."
        )
    current_time = now or datetime.now(timezone.utc)
    provider_is_live = not provider.is_simulated and provider.name != "fixture"
    calendar_is_live = calendar.name != "fixture"
    alerts_enabled = (
        os.environ.get("ALERT_RULES_ENABLED", "true").lower() == "true"
        and provider_is_live
        and calendar_is_live
    )
    attempted = 0
    recorded = 0
    failed = 0
    with session_factory() as session:
        companies = list(
            session.scalars(
                select(Company)
                .where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))
                .order_by(Company.exchange, Company.symbol)
            )
        )
        for company in companies:
            if not _session_open(calendar, company.exchange, current_time):
                continue
            attempted += 1
            if provider.name == "yfinance":
                register_verified_ticker(company.exchange, company.symbol)
            try:
                quote = provider.fetch_quote(company.exchange, company.symbol)
            except RateLimitError:
                raise
            except ProviderError:
                failed += 1
                logger.exception("Provider could not quote %s:%s.", company.exchange, company.symbol)
                continue
            received_at = now if now is not None else datetime.now(timezone.utc)
            try:
                if quote.provider != provider.name and not (
                    provider.name == "yfinance" and "Yahoo Finance" in quote.provider
                ):
                    raise ValueError("Provider quote names do not match the selected adapter.")
                if quote.is_simulated != provider.is_simulated:
                    raise ValueError("Provider quote simulation status does not match the adapter.")
                stored = record_quote(
                    session,
                    quote,
                    now=received_at,
                    alerts_enabled=alerts_enabled,
                    fixture_alerts_enabled=provider.is_simulated,
                )
            except ValueError:
                failed += 1
                logger.exception("Rejected invalid quote for %s:%s.", company.exchange, company.symbol)
                continue
            recorded += 1
            logger.info(
                "Recorded %s quote for %s:%s at %s (%s).",
                stored.status,
                stored.exchange,
                stored.symbol,
                stored.quoted_at.isoformat(),
                stored.provider,
            )
    if provider.is_simulated:
        logger.warning("Fixture quotes are simulated; live target alerts remain disabled.")
    return PollResult(attempted, recorded, alerts_enabled, failed)


@contextmanager
def single_worker_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("Another price worker already holds the lock.") from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("Another price worker already holds the lock.") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _wait_for_rate_limit(error: RateLimitError, interval_seconds: int) -> None:
    delay = max(float(interval_seconds), error.retry_after_seconds)
    logger.warning("Provider rate limited polling; retrying in %.1f seconds.", delay)
    time_module.sleep(delay)


def _compute_stale_and_monitored(session: Session, now: datetime) -> tuple[int, int]:
    companies = list(
        session.scalars(
            select(Company).where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))
        )
    )
    monitored = len(companies)
    if monitored == 0:
        return 0, 0
    latest_quotes_subq = (
        select(
            PriceQuote.id,
            func.row_number()
            .over(
                partition_by=(PriceQuote.exchange, PriceQuote.symbol),
                order_by=(PriceQuote.quoted_at.desc(), PriceQuote.id.desc()),
            )
            .label("position"),
        )
        .subquery()
    )
    quotes = session.scalars(
        select(PriceQuote)
        .join(latest_quotes_subq, latest_quotes_subq.c.id == PriceQuote.id)
        .where(latest_quotes_subq.c.position == 1)
    )
    quote_map = {(q.exchange, q.symbol): q for q in quotes}
    stale = 0
    for c in companies:
        q = quote_map.get((c.exchange, c.symbol))
        if q is None:
            continue
        is_fresh = (
            q.status == "fresh"
            and timedelta(0) <= now - normalize_utc(q.quoted_at) <= timedelta(seconds=q.freshness_seconds)
        )
        if not is_fresh and not q.is_simulated:
            stale += 1
    return monitored, stale


def _update_heartbeat(
    provider: PriceProvider,
    calendar: ExchangeCalendar,
    *,
    started_at: datetime,
    last_poll_at: datetime | None,
    last_error: str | None,
    recorded_quotes: int = 0,
    failed_quotes: int = 0,
    reconnected: bool = False,
) -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        monitored, stale = _compute_stale_and_monitored(session, now)
        heartbeat = session.get(WorkerHeartbeat, "price")
        conn_status = (
            "error"
            if last_error
            else "polling"
            if not provider.is_simulated
            else "connected"
        )
        ws_status = (
            "fallback_polling"
            if not provider.is_simulated
            else "unavailable"
        )
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                name="price",
                provider=provider.name,
                exchange_calendar=calendar.name,
                started_at=started_at,
                last_poll_at=last_poll_at,
                last_error=last_error,
                connection_status=conn_status,
                websocket_status=ws_status,
                last_successful_quote_at=last_poll_at if recorded_quotes > 0 else None,
                last_failed_at=now if (last_error or failed_quotes > 0) else None,
                failed_requests_count=failed_quotes + (1 if last_error else 0),
                reconnect_count=1 if reconnected else 0,
                monitored_symbols_count=monitored,
                stale_symbols_count=stale,
            )
            session.add(heartbeat)
        else:
            heartbeat.provider = provider.name
            heartbeat.exchange_calendar = calendar.name
            heartbeat.started_at = started_at
            if last_poll_at is not None:
                heartbeat.last_poll_at = last_poll_at
            heartbeat.last_error = last_error
            heartbeat.connection_status = conn_status
            heartbeat.websocket_status = ws_status
            if recorded_quotes > 0 and last_poll_at is not None:
                heartbeat.last_successful_quote_at = last_poll_at
            if last_error or failed_quotes > 0:
                heartbeat.last_failed_at = now
                heartbeat.failed_requests_count = (heartbeat.failed_requests_count or 0) + failed_quotes + (1 if last_error else 0)
            if reconnected:
                heartbeat.reconnect_count = (heartbeat.reconnect_count or 0) + 1
            heartbeat.monitored_symbols_count = monitored
            heartbeat.stale_symbols_count = stale
        session.commit()


def run_worker(
    provider: PriceProvider,
    calendar: ExchangeCalendar,
    *,
    interval_seconds: int = 60,
    once: bool = False,
) -> None:
    if interval_seconds < 1:
        raise ValueError("Polling interval must be at least one second.")
    if provider.name == "yfinance" and calendar.name == "fixture":
        raise ValueError(
            "Live provider polling requires an authoritative exchange calendar. "
            "Use app.yfinance_smoke for a one-off quote; it does not activate alerts."
        )
    notifier = MultiChannelNotifier()
    with single_worker_lock():
        started_at = datetime.now(timezone.utc)
        _update_heartbeat(
            provider,
            calendar,
            started_at=started_at,
            last_poll_at=None,
            last_error=None,
        )
        logger.info(
            "Price worker started (provider=%s, calendar=%s, mode=%s).",
            provider.name,
            calendar.name,
            "simulated fixture" if provider.is_simulated else "live monitoring",
        )
        had_error = False
        while True:
            try:
                result = poll_once(provider, calendar)
                delivery = drain_outbox(notifier)
                last_poll_at = datetime.now(timezone.utc)
                _update_heartbeat(
                    provider,
                    calendar,
                    started_at=started_at,
                    last_poll_at=last_poll_at,
                    last_error=None,
                    recorded_quotes=result.recorded,
                    failed_quotes=result.failed,
                    reconnected=had_error,
                )
                had_error = False
                logger.info(
                    "Poll complete: attempted=%d recorded=%d failed=%d alerts_enabled=%s notifications_sent=%d retried=%d failed=%d.",
                    result.attempted,
                    result.recorded,
                    result.failed,
                    result.alerts_enabled,
                    delivery.sent,
                    delivery.retried,
                    delivery.failed,
                )
            except RateLimitError as error:
                had_error = True
                _update_heartbeat(
                    provider,
                    calendar,
                    started_at=started_at,
                    last_poll_at=None,
                    last_error=str(error)[:1000],
                )
                if once:
                    logger.error("%s", error)
                    raise
                _wait_for_rate_limit(error, interval_seconds)
                continue
            except Exception as error:
                had_error = True
                _update_heartbeat(
                    provider,
                    calendar,
                    started_at=started_at,
                    last_poll_at=None,
                    last_error=str(error)[:1000],
                )
                logger.exception("Price worker cycle failed.")
                if once:
                    raise
            if once:
                return
            time_module.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the separate, single-instance portfolio price worker."
    )
    parser.add_argument("--provider", choices=("fixture", "yfinance"), default="yfinance")
    parser.add_argument("--calendar", choices=("fixture", "nse_bse"), default="nse_bse")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        provider: PriceProvider = (
            FixturePriceProvider()
            if args.provider == "fixture"
            else YFinanceProvider()
        )
        calendar: ExchangeCalendar = (
            FixtureExchangeCalendar()
            if args.calendar == "fixture"
            else NseBseExchangeCalendar()
        )
        run_worker(
            provider,
            calendar,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
    except (RuntimeError, ValueError) as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
