"""Asynchronous price-monitoring worker consuming live CMP ticks from the Shoonya WebSocket service.

Architecture:
Shoonya WebSocket Stream -> Async Tick Consumer (worker.py) -> Target Comparison Engine (prices.py)
                         -> Alert & Outbox Persistence -> Multi-Channel Notification Delivery
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time as time_module
from collections.abc import AsyncIterable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Company, PriceQuote, WorkerHeartbeat
from .notifications import MultiChannelNotifier, drain_outbox
from .prices import (
    INDIA_TZ,
    LIVE_TICK_DEDUPLICATOR,
    ExchangeCalendar,
    FixtureExchangeCalendar,
    FixturePriceProvider,
    NseBseExchangeCalendar,
    PriceProvider,
    ProviderError,
    ProviderQuote,
    RateLimitError,
    YFinanceProvider,
    evaluate_live_cmp_tick,
    normalize_utc,
    record_quote,
    register_verified_ticker,
)
from .shoonya_feed import (
    ShoonyaConfig,
    ShoonyaPriceProvider,
    ShoonyaTokenResolver,
    ShoonyaWebSocketService,
    parse_shoonya_tick,
)

logger = logging.getLogger("portfolio.price_worker")
LOCK_PATH = Path(__file__).resolve().parents[1] / ".price-worker.lock"


@dataclass(frozen=True)
class PollResult:
    attempted: int
    recorded: int
    alerts_enabled: bool
    failed: int = 0


@dataclass(frozen=True)
class TickStreamResult:
    received: int
    recorded: int
    failed: int
    notifications_sent: int
    alerts_enabled: bool


def _session_open(calendar: ExchangeCalendar, exchange: str, now: datetime) -> bool:
    local_now = now.astimezone(INDIA_TZ)
    trading_session = calendar.session_for(exchange, local_now.date())
    return (
        trading_session is not None
        and trading_session.opens_at <= local_now.time() <= trading_session.closes_at
    )


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


def process_incoming_tick(
    tick: ProviderQuote | dict[str, Any],
    calendar: ExchangeCalendar,
    *,
    resolver: ShoonyaTokenResolver | None = None,
    freshness_seconds: int = 300,
    now: datetime | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    notifier: MultiChannelNotifier | None = None,
    enforce_market_hours: bool = False,
    dedup_window_seconds: float | None = None,
) -> PriceQuote | None:
    """Validate an incoming Shoonya CMP tick and immediately pass it to `evaluate_live_cmp_tick`
    in `app/prices.py`.

    Executes the quote write, uploaded Excel rule lookup, Base/Bull target comparison, and
    `notification_outbox` write in a single SQLAlchemy transaction, and only drains the outbox
    when a new alert notification was actually queued.
    """
    received_at = normalize_utc(now or datetime.now(timezone.utc))
    if isinstance(tick, dict):
        active_resolver = resolver or ShoonyaTokenResolver()
        quote = parse_shoonya_tick(
            tick,
            active_resolver,
            freshness_seconds=freshness_seconds,
            fallback_now=received_at,
        )
        if quote is None:
            return None
    else:
        quote = tick

    if enforce_market_hours and not _session_open(calendar, quote.exchange, received_at):
        logger.debug(
            "Ignoring tick for %s:%s outside %s trading session.",
            quote.exchange,
            quote.symbol,
            calendar.name,
        )
        return None

    provider_is_live = not quote.is_simulated and quote.provider != "fixture"
    calendar_is_live = calendar.name != "fixture"
    alerts_enabled = (
        os.environ.get("ALERT_RULES_ENABLED", "true").lower() == "true"
        and provider_is_live
        and calendar_is_live
    )

    if LIVE_TICK_DEDUPLICATOR.should_skip(
        quote.exchange,
        quote.symbol,
        quote.price,
        window_seconds=dedup_window_seconds,
    ):
        return None

    with session_factory() as session:
        outcome = evaluate_live_cmp_tick(
            session,
            quote,
            now=received_at,
            alerts_enabled=alerts_enabled,
            dedup_window_seconds=0.0,
        )

    stored = outcome.quote
    if stored is None:
        return None

    sent_count = 0
    if outcome.outbox_rows:
        active_notifier = notifier or MultiChannelNotifier()
        drain_res = drain_outbox(active_notifier, session_factory=session_factory, now=received_at)
        sent_count = drain_res.sent
    stored._notifications_sent = sent_count  # type: ignore[attr-defined]

    logger.info(
        "Live tick processed: %s:%s CMP=%s status=%s alerts=%d quoted_at=%s (%s).",
        stored.exchange,
        stored.symbol,
        stored.price,
        stored.status,
        len(outcome.triggered_alerts),
        stored.quoted_at.isoformat(),
        stored.provider,
    )
    return stored


async def consume_tick_stream(
    tick_stream: AsyncIterable[ProviderQuote | dict[str, Any]],
    calendar: ExchangeCalendar | None = None,
    *,
    resolver: ShoonyaTokenResolver | None = None,
    freshness_seconds: int = 300,
    session_factory: Callable[[], Session] = SessionLocal,
    enforce_market_hours: bool = False,
    max_ticks: int | None = None,
    heartbeat_interval_seconds: float = 5.0,
    dedup_window_seconds: float | None = None,
) -> TickStreamResult:
    """Asynchronously consume incoming CMP ticks from a live WebSocket stream and pass each price
    immediately to the target comparison logic in `app/prices.py`.
    """
    active_calendar = calendar or NseBseExchangeCalendar()
    active_resolver = resolver or ShoonyaTokenResolver()
    notifier = MultiChannelNotifier()
    started_at = datetime.now(timezone.utc)

    received = 0
    recorded = 0
    failed = 0
    notifications_sent = 0
    alerts_enabled = (
        os.environ.get("ALERT_RULES_ENABLED", "true").lower() == "true"
        and active_calendar.name != "fixture"
    )

    last_heartbeat_mono = 0.0
    last_provider_name = "shoonya"
    last_tick_time: datetime | None = None
    pending_heartbeat_quotes = 0

    async for raw_tick in tick_stream:
        received += 1
        tick_time = datetime.now(timezone.utc)
        try:
            stored = await asyncio.to_thread(
                process_incoming_tick,
                raw_tick,
                active_calendar,
                resolver=active_resolver,
                freshness_seconds=freshness_seconds,
                now=tick_time,
                session_factory=session_factory,
                notifier=notifier,
                enforce_market_hours=enforce_market_hours,
                dedup_window_seconds=dedup_window_seconds,
            )
            if stored is not None:
                recorded += 1
                notifications_sent += getattr(stored, "_notifications_sent", 0)
                last_provider_name = stored.provider
                last_tick_time = tick_time
                pending_heartbeat_quotes += 1
                now_mono = time_module.monotonic()
                if (
                    recorded == 1
                    or heartbeat_interval_seconds <= 0
                    or (now_mono - last_heartbeat_mono) >= heartbeat_interval_seconds
                ):
                    await asyncio.to_thread(
                        _update_stream_heartbeat,
                        provider_name=last_provider_name,
                        calendar_name=active_calendar.name,
                        started_at=started_at,
                        last_tick_at=last_tick_time,
                        last_error=None,
                        recorded_quotes=pending_heartbeat_quotes,
                        failed_quotes=0,
                        session_factory=session_factory,
                    )
                    last_heartbeat_mono = now_mono
                    pending_heartbeat_quotes = 0
        except Exception as exc:
            failed += 1
            err_msg = str(exc)[:1000]
            logger.exception("Failed to process incoming WebSocket CMP tick.")
            await asyncio.to_thread(
                _update_stream_heartbeat,
                provider_name=last_provider_name,
                calendar_name=active_calendar.name,
                started_at=started_at,
                last_tick_at=None,
                last_error=err_msg,
                recorded_quotes=0,
                failed_quotes=1,
                session_factory=session_factory,
            )
            last_heartbeat_mono = time_module.monotonic()

        if max_ticks is not None and received >= max_ticks:
            break

    if pending_heartbeat_quotes > 0 and last_tick_time is not None:
        await asyncio.to_thread(
            _update_stream_heartbeat,
            provider_name=last_provider_name,
            calendar_name=active_calendar.name,
            started_at=started_at,
            last_tick_at=last_tick_time,
            last_error=None,
            recorded_quotes=pending_heartbeat_quotes,
            failed_quotes=0,
            session_factory=session_factory,
        )

    return TickStreamResult(
        received=received,
        recorded=recorded,
        failed=failed,
        notifications_sent=notifications_sent,
        alerts_enabled=alerts_enabled,
    )


def _update_stream_heartbeat(
    *,
    provider_name: str,
    calendar_name: str,
    started_at: datetime,
    last_tick_at: datetime | None,
    last_error: str | None,
    recorded_quotes: int = 0,
    failed_quotes: int = 0,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        monitored, stale = _compute_stale_and_monitored(session, now)
        heartbeat = session.get(WorkerHeartbeat, "price")
        conn_status = "error" if last_error else "connected"
        ws_status = "reconnecting" if last_error else "connected"
        if heartbeat is None:
            heartbeat = WorkerHeartbeat(
                name="price",
                provider=provider_name,
                exchange_calendar=calendar_name,
                started_at=started_at,
                last_poll_at=last_tick_at or now,
                last_error=last_error,
                connection_status=conn_status,
                websocket_status=ws_status,
                last_successful_quote_at=last_tick_at if recorded_quotes > 0 else None,
                last_failed_at=now if (last_error or failed_quotes > 0) else None,
                failed_requests_count=failed_quotes,
                reconnect_count=0,
                monitored_symbols_count=monitored,
                stale_symbols_count=stale,
            )
            session.add(heartbeat)
        else:
            heartbeat.provider = provider_name
            heartbeat.exchange_calendar = calendar_name
            heartbeat.started_at = started_at
            if last_tick_at is not None:
                heartbeat.last_poll_at = last_tick_at
                if recorded_quotes > 0:
                    heartbeat.last_successful_quote_at = last_tick_at
            heartbeat.last_error = last_error
            heartbeat.connection_status = conn_status
            heartbeat.websocket_status = ws_status
            if last_error or failed_quotes > 0:
                heartbeat.last_failed_at = now
                heartbeat.failed_requests_count = (heartbeat.failed_requests_count or 0) + failed_quotes
            heartbeat.monitored_symbols_count = monitored
            heartbeat.stale_symbols_count = stale
        session.commit()


async def run_async_worker(
    ws_service: ShoonyaWebSocketService | None = None,
    calendar: ExchangeCalendar | None = None,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    enforce_market_hours: bool = False,
    max_ticks: int | None = None,
) -> TickStreamResult:
    """Run the asynchronous Shoonya WebSocket price-monitoring worker under a single-instance lock."""
    active_calendar = calendar or NseBseExchangeCalendar()
    service = ws_service or ShoonyaWebSocketService(
        calendar=active_calendar,
        session_factory=session_factory,
    )
    started_at = datetime.now(timezone.utc)
    await asyncio.to_thread(
        _update_stream_heartbeat,
        provider_name="shoonya",
        calendar_name=active_calendar.name,
        started_at=started_at,
        last_tick_at=None,
        last_error=None,
        session_factory=session_factory,
    )
    logger.info(
        "Async Shoonya WebSocket worker started (provider=shoonya, calendar=%s).",
        active_calendar.name,
    )
    return await consume_tick_stream(
        service.stream_quotes(),
        active_calendar,
        resolver=service.resolver,
        freshness_seconds=service.config.quote_freshness_seconds,
        session_factory=session_factory,
        enforce_market_hours=enforce_market_hours,
        max_ticks=max_ticks,
    )


# ---------------------------------------------------------------------------
# Polling Fallback Support (Preserved for REST fallback & test compatibility)
# ---------------------------------------------------------------------------


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
    default_provider = os.environ.get(
        "PRICE_PROVIDER",
        "shoonya_ws" if os.environ.get("SHOONYA_USER_ID") else "yfinance",
    )
    parser = argparse.ArgumentParser(
        description="Run the asynchronous Shoonya WebSocket price-monitoring worker (or polling fallback)."
    )
    parser.add_argument(
        "--provider",
        choices=("shoonya_ws", "shoonya", "yfinance", "fixture"),
        default=default_provider,
    )
    parser.add_argument("--calendar", choices=("nse_bse", "fixture"), default="nse_bse")
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--enforce-market-hours", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        calendar: ExchangeCalendar = (
            FixtureExchangeCalendar()
            if args.calendar == "fixture"
            else NseBseExchangeCalendar()
        )
        if args.provider == "shoonya_ws":
            with single_worker_lock():
                asyncio.run(
                    run_async_worker(
                        calendar=calendar,
                        enforce_market_hours=args.enforce_market_hours,
                    )
                )
            return

        if args.provider == "fixture":
            provider: PriceProvider = FixturePriceProvider()
        elif args.provider == "shoonya":
            provider = ShoonyaPriceProvider(ShoonyaConfig.from_env())
        else:
            provider = YFinanceProvider()

        run_worker(
            provider,
            calendar,
            interval_seconds=args.interval_seconds,
            once=args.once,
        )
    except KeyboardInterrupt:
        logger.info("Worker stopped by user.")
    except (RuntimeError, ValueError, ProviderError) as error:
        logger.error("%s", error)
        sys.exit(2)


if __name__ == "__main__":
    main()
