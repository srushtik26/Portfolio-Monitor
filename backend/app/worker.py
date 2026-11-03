import argparse
import logging
import os
import sys
import time as time_module
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Company, WorkerHeartbeat
from .notifications import DryRunNotifier, drain_outbox
from .prices import (
    INDIA_TZ,
    ExchangeCalendar,
    FixtureExchangeCalendar,
    FixturePriceProvider,
    PriceProvider,
    ProviderError,
    RateLimitError,
    record_quote,
)

logger = logging.getLogger("portfolio.price_worker")
LOCK_PATH = Path(__file__).resolve().parents[1] / ".price-worker.lock"


@dataclass(frozen=True)
class PollResult:
    attempted: int
    recorded: int
    alerts_enabled: bool


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
    current_time = now or datetime.now(timezone.utc)
    provider_is_live = not provider.is_simulated and provider.name != "fixture"
    calendar_is_live = calendar.name != "fixture"
    alerts_enabled = (
        os.environ.get("ALERT_RULES_ENABLED", "").lower() == "true"
        and provider_is_live
        and calendar_is_live
    )
    attempted = 0
    recorded = 0
    with session_factory() as session:
        companies = list(
            session.scalars(select(Company).order_by(Company.exchange, Company.symbol))
        )
        for company in companies:
            if not _session_open(calendar, company.exchange, current_time):
                continue
            attempted += 1
            try:
                quote = provider.fetch_quote(company.exchange, company.symbol)
            except RateLimitError:
                raise
            except ProviderError:
                logger.exception("Provider could not quote %s:%s.", company.exchange, company.symbol)
                continue
            received_at = now if now is not None else datetime.now(timezone.utc)
            try:
                if quote.provider != provider.name:
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
    return PollResult(attempted, recorded, alerts_enabled)


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


def _update_heartbeat(
    provider: PriceProvider,
    calendar: ExchangeCalendar,
    *,
    started_at: datetime,
    last_poll_at: datetime | None,
    last_error: str | None,
) -> None:
    with SessionLocal() as session:
        heartbeat = session.get(WorkerHeartbeat, "price")
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                name="price",
                provider=provider.name,
                exchange_calendar=calendar.name,
                started_at=started_at,
                last_poll_at=last_poll_at,
                last_error=last_error,
            )
            session.add(heartbeat)
        else:
            heartbeat.provider = provider.name
            heartbeat.exchange_calendar = calendar.name
            heartbeat.started_at = started_at
            if last_poll_at is not None:
                heartbeat.last_poll_at = last_poll_at
            heartbeat.last_error = last_error
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
            "simulated fixture" if provider.is_simulated else "not configured for live use",
        )
        while True:
            try:
                result = poll_once(provider, calendar)
                delivery = drain_outbox(DryRunNotifier())
                last_poll_at = datetime.now(timezone.utc)
                _update_heartbeat(
                    provider,
                    calendar,
                    started_at=started_at,
                    last_poll_at=last_poll_at,
                    last_error=None,
                )
                logger.info(
                    "Poll complete: attempted=%d recorded=%d alerts_enabled=%s notifications_sent=%d retried=%d failed=%d.",
                    result.attempted,
                    result.recorded,
                    result.alerts_enabled,
                    delivery.sent,
                    delivery.retried,
                    delivery.failed,
                )
            except RateLimitError as error:
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
    parser.add_argument("--provider", choices=("fixture",), required=True)
    parser.add_argument("--calendar", choices=("fixture",), required=True)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        run_worker(
            FixturePriceProvider(),
            FixtureExchangeCalendar(),
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
    except RuntimeError as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
