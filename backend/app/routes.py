import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import PurePath
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.exc import StaleDataError

from .database import SessionLocal, get_db
from .imports import (
    create_preview,
    get_batch,
    inspect_upload,
    read_upload,
    restore_import_version,
    serialize_batch,
    update_preview_row,
)
from .models import (
    AuditLog,
    Company,
    ImportBatch,
    ImportRow,
    NotificationOutbox,
    NotificationPreference,
    PriceQuote,
    TargetAlert,
    TargetAlertState,
    TargetRevision,
    WorkerHeartbeat,
)
from .notifications import (
    DryRunNotifier,
    MultiChannelNotifier,
    NotificationMessage,
    PermanentSendError,
    RetryableSendError,
    SmtpEmailNotifier,
    check_live_notification_gate,
    collect_alert_email_recipients,
    drain_outbox,
    format_alert_email,
    format_alert_email_html,
    get_or_create_preferences,
    is_smtp_complete,
    queue_alert_notifications,
)
from .prices import (
    NseBseExchangeCalendar,
    ProviderError,
    YFinanceProvider,
    apply_alert_lifecycle_action,
    enrich_company_dict,
    normalize_utc,
    record_quote,
    register_verified_ticker,
    serialize_quote,
)
from .schemas import (
    AlertActionRequest,
    CompanyRead,
    ImportConfirm,
    ImportRowUpdate,
    NotificationPreferenceUpdate,
    PriceHistoryRead,
    TargetConfigUpdate,
    TargetUpdate,
)
from .security import AdminUser, CurrentUser, get_current_user, record_activity, require_admin

router = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])
public_router = APIRouter(prefix="/api")
DbSession = Annotated[Session, Depends(get_db)]


def _serialize_company(
    session: Session,
    company: Company,
    quote_by_company: dict[tuple[str, str], dict[str, Any]],
    states_by_key: dict[tuple[str, str, str], TargetAlertState],
) -> CompanyRead:
    company_data = CompanyRead.model_validate(company).model_dump()
    identity = (company.exchange, company.symbol)
    quote_data = quote_by_company.get(identity)
    base_state = states_by_key.get((company.exchange, company.symbol, "base"))
    bull_state = states_by_key.get((company.exchange, company.symbol, "bull"))
    enrich_company_dict(company, company_data, quote_data, base_state, bull_state)
    return CompanyRead.model_validate(company_data)


def _load_quotes_and_states(
    session: Session,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str, str], TargetAlertState]]:
    latest_quotes = (
        select(
            PriceQuote.id,
            func.row_number()
            .over(
                partition_by=(PriceQuote.exchange, PriceQuote.symbol),
                order_by=(
                    func.coalesce(PriceQuote.quoted_at, PriceQuote.received_at).desc(),
                    PriceQuote.id.desc(),
                ),
            )
            .label("position"),
        )
        .subquery()
    )
    quotes = session.scalars(
        select(PriceQuote)
        .join(latest_quotes, latest_quotes.c.id == PriceQuote.id)
        .where(latest_quotes.c.position == 1)
    )
    quote_by_company = {
        (quote.exchange, quote.symbol): serialize_quote(quote) for quote in quotes
    }
    state_rows = session.scalars(
        select(TargetAlertState).where(TargetAlertState.is_simulated.is_(False))
    )
    states_by_key = {
        (row.exchange, row.symbol, row.target_kind): row for row in state_rows
    }
    return quote_by_company, states_by_key


@public_router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/companies", response_model=list[CompanyRead])
def list_companies(session: DbSession) -> list[CompanyRead]:
    companies = list(
        session.scalars(select(Company).order_by(Company.exchange, Company.symbol))
    )
    quote_by_company, states_by_key = _load_quotes_and_states(session)
    return [
        _serialize_company(session, company, quote_by_company, states_by_key)
        for company in companies
    ]


_SESSION_CHART_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _fetch_yf_session_bars(exchange: str, symbol: str) -> dict[str, Any] | None:
    """Fetch 5-day 5m intraday bars from Yahoo Finance (cached 120s) to populate full 09:15-15:30 session and previous close."""
    import sys
    import time
    from zoneinfo import ZoneInfo
    from .prices import yahoo_ticker_for

    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        return None
    key = (exchange.upper(), symbol.upper())
    now_mono = time.monotonic()
    cached = _SESSION_CHART_CACHE.get(key)
    if cached and (now_mono - cached[0]) < 120.0:
        return cached[1]
    try:
        import yfinance as yf

        ticker_str = yahoo_ticker_for(exchange, symbol)
        hist = yf.Ticker(ticker_str).history(period="5d", interval="5m")
        if hist is None or hist.empty:
            return None
        ist = ZoneInfo("Asia/Kolkata")
        by_day: dict[str, list[tuple[datetime, float]]] = {}
        for idx_ts, row in hist.iterrows():
            close_val = row.get("Close") if hasattr(row, "get") else row["Close"]
            if close_val is None or float(close_val) != float(close_val) or float(close_val) <= 0:
                continue
            dt_ist = idx_ts.to_pydatetime().astimezone(ist)
            day_key = dt_ist.strftime("%Y-%m-%d")
            by_day.setdefault(day_key, []).append((dt_ist, round(float(close_val), 2)))
        if not by_day:
            return None
        sorted_days = sorted(by_day.keys())
        latest_day = sorted_days[-1]
        intraday_points = []
        for dt_ist, price_val in by_day[latest_day]:
            mins = (dt_ist.hour * 60 + dt_ist.minute) - (9 * 60 + 15)
            if 0 <= mins <= 375:
                intraday_points.append(
                    {
                        "minute": mins,
                        "time_label": dt_ist.strftime("%I:%M %p").lstrip("0"),
                        "timestamp": dt_ist.isoformat(),
                        "price": price_val,
                    }
                )
        daily_closes = []
        for d_key in sorted_days:
            day_bars = by_day[d_key]
            if day_bars:
                last_dt, last_price = day_bars[-1]
                open_price = day_bars[0][1]
                high_price = max(p for _, p in day_bars)
                low_price = min(p for _, p in day_bars)
                daily_closes.append(
                    {
                        "date": d_key,
                        "label": last_dt.strftime("%d %b"),
                        "open": open_price,
                        "high": high_price,
                        "low": low_price,
                        "close": last_price,
                    }
                )
        prev_close = daily_closes[-2]["close"] if len(daily_closes) >= 2 else None
        prev_close_date = daily_closes[-2]["label"] if len(daily_closes) >= 2 else None
        payload = {
            "session_date": latest_day,
            "intraday": intraday_points,
            "daily_closes": daily_closes,
            "previous_close": prev_close,
            "previous_close_date": prev_close_date,
        }
        _SESSION_CHART_CACHE[key] = (now_mono, payload)
        return payload
    except Exception:
        return None


def _build_session_chart_payload(
    exchange: str,
    symbol: str,
    serialized_quotes: list[dict[str, Any]],
    saved_cmp: float | None,
) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    yf_data = _fetch_yf_session_bars(exchange, symbol)

    # Group DB quotes by IST date and 5-minute bucket
    db_by_day: dict[str, list[tuple[datetime, float]]] = {}
    for q in reversed(serialized_quotes):
        raw_ts = q.get("quoted_at") or q.get("received_at")
        price_val = q.get("price")
        if raw_ts is None or price_val is None:
            continue
        if isinstance(raw_ts, str):
            try:
                dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            dt = raw_ts
        dt_ist = normalize_utc(dt).astimezone(ist)
        day_str = dt_ist.strftime("%Y-%m-%d")
        db_by_day.setdefault(day_str, []).append((dt_ist, round(float(price_val), 2)))

    sorted_db_days = sorted(db_by_day.keys())
    session_date = (
        yf_data["session_date"]
        if yf_data and yf_data.get("session_date")
        else (sorted_db_days[-1] if sorted_db_days else datetime.now(ist).strftime("%Y-%m-%d"))
    )

    # Merge intraday points by minute offset from 09:15 AM IST (0..375)
    by_minute: dict[int, dict[str, Any]] = {}
    if yf_data and yf_data.get("intraday"):
        for pt in yf_data["intraday"]:
            by_minute[int(pt["minute"])] = dict(pt)

    for dt_ist, price_val in db_by_day.get(session_date, []):
        mins = max(0, min(375, (dt_ist.hour * 60 + dt_ist.minute) - (9 * 60 + 15)))
        by_minute[mins] = {
            "minute": mins,
            "time_label": dt_ist.strftime("%I:%M %p").lstrip("0"),
            "timestamp": dt_ist.isoformat(),
            "price": price_val,
        }

    intraday_series = [by_minute[m] for m in sorted(by_minute.keys())]

    # Build daily closing history
    daily_closes: list[dict[str, Any]] = []
    if yf_data and yf_data.get("daily_closes"):
        daily_closes = list(yf_data["daily_closes"])
    else:
        for d_key in sorted_db_days:
            pts = db_by_day[d_key]
            if pts:
                last_dt, last_p = pts[-1]
                daily_closes.append(
                    {
                        "date": d_key,
                        "label": last_dt.strftime("%d %b"),
                        "open": pts[0][1],
                        "high": max(p for _, p in pts),
                        "low": min(p for _, p in pts),
                        "close": last_p,
                    }
                )

    previous_close = (
        yf_data.get("previous_close")
        if yf_data and yf_data.get("previous_close") is not None
        else (
            daily_closes[-2]["close"]
            if len(daily_closes) >= 2
            else saved_cmp
        )
    )
    previous_close_date = (
        yf_data.get("previous_close_date")
        if yf_data and yf_data.get("previous_close_date")
        else (
            daily_closes[-2]["label"]
            if len(daily_closes) >= 2
            else "Ref CMP"
        )
    )

    prices_today = [pt["price"] for pt in intraday_series]
    open_price = prices_today[0] if prices_today else None
    closing_price = prices_today[-1] if prices_today else None
    high_price = max(prices_today) if prices_today else None
    low_price = min(prices_today) if prices_today else None

    return {
        "session_date": session_date,
        "market_open_label": "09:15 AM",
        "market_close_label": "03:30 PM",
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "closing_price": closing_price,
        "previous_close": previous_close,
        "previous_close_date": previous_close_date,
        "intraday": intraday_series,
        "daily_closes": daily_closes,
    }


@router.get("/companies/{exchange}/{symbol}/details")
def get_company_details(
    exchange: str,
    symbol: str,
    session: DbSession,
) -> dict[str, Any]:
    identity = (exchange.upper(), symbol.upper())
    company = session.get(Company, identity)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    quote_by_company, states_by_key = _load_quotes_and_states(session)
    company_read = _serialize_company(session, company, quote_by_company, states_by_key)

    quotes = session.scalars(
        select(PriceQuote)
        .where(
            PriceQuote.exchange == identity[0],
            PriceQuote.symbol == identity[1],
        )
        .order_by(
            func.coalesce(PriceQuote.quoted_at, PriceQuote.received_at).desc(),
            PriceQuote.id.desc(),
        )
        .limit(500)
    )
    revisions = session.scalars(
        select(TargetRevision)
        .where(
            TargetRevision.exchange == identity[0],
            TargetRevision.symbol == identity[1],
        )
        .order_by(TargetRevision.version.desc(), TargetRevision.id.desc())
        .limit(50)
    )
    alert_rows = session.execute(
        select(TargetAlert, TargetRevision, PriceQuote, NotificationOutbox)
        .join(TargetRevision, TargetRevision.id == TargetAlert.target_revision_id)
        .join(PriceQuote, PriceQuote.id == TargetAlert.quote_id)
        .outerjoin(
            NotificationOutbox,
            NotificationOutbox.target_alert_id == TargetAlert.id,
        )
        .where(
            TargetRevision.exchange == identity[0],
            TargetRevision.symbol == identity[1],
        )
        .order_by(TargetAlert.triggered_at.desc(), TargetAlert.id.desc())
        .limit(50)
    ).all()

    alerts_serialized = []
    seen_alerts: set[int] = set()
    for alert, rev, quote, notification in alert_rows:
        if alert.id in seen_alerts:
            continue
        seen_alerts.add(alert.id)
        target_val = (
            alert.target_price_snapshot
            if alert.target_price_snapshot is not None
            else (rev.base_target if alert.target_kind == "base" else rev.bull_target)
        )
        alerts_serialized.append(
            {
                "id": alert.id,
                "target_kind": alert.target_kind,
                "target": float(target_val) if target_val is not None else None,
                "price": float(alert.price),
                "comparison_direction": alert.comparison_direction,
                "trigger_mode": alert.trigger_mode,
                "lifecycle_status": alert.lifecycle_status,
                "trigger_cycle": alert.trigger_cycle,
                "triggered_at": normalize_utc(alert.triggered_at),
                "is_simulated": alert.is_simulated,
                "delivery_status": notification.status if notification else "not_queued",
                "channel": notification.channel if notification else None,
            }
        )

    revisions_serialized = [
        {
            "id": r.id,
            "version": r.version,
            "previous_base_target": float(r.previous_base_target) if r.previous_base_target is not None else None,
            "previous_bull_target": float(r.previous_bull_target) if r.previous_bull_target is not None else None,
            "base_target": float(r.base_target) if r.base_target is not None else None,
            "bull_target": float(r.bull_target) if r.bull_target is not None else None,
            "base_direction": r.base_direction,
            "bull_direction": r.bull_direction,
            "source": r.source,
            "import_id": r.import_id,
            "changed_by": r.changed_by,
            "changed_at": normalize_utc(r.changed_at),
            "cell_refs": r.cell_refs,
        }
        for r in revisions
    ]

    serialized_quotes = [serialize_quote(q) for q in quotes]
    saved_cmp_val = float(company.cmp) if company.cmp is not None else None
    session_chart = _build_session_chart_payload(
        identity[0],
        identity[1],
        serialized_quotes,
        saved_cmp_val,
    )
    return {
        "company": company_read.model_dump(),
        "quote_history": serialized_quotes,
        "price_history": serialized_quotes,
        "session_chart": session_chart,
        "alert_history": alerts_serialized,
        "alerts": alerts_serialized,
        "target_revisions": revisions_serialized,
        "revisions": revisions_serialized,
        "excel_source": company.source_metadata,
    }


@router.get("/price-feed/status")
def price_feed_status(session: DbSession) -> dict[str, object]:
    heartbeat = session.get(WorkerHeartbeat, "price")
    live_configured = bool(
        heartbeat is not None
        and heartbeat.provider != "fixture"
        and heartbeat.exchange_calendar != "fixture"
    )
    if not live_configured:
        return {
            "provider": None,
            "exchange_calendar": None,
            "live_mode": "not_configured",
            "alert_rules_enabled": False,
            "reason": "Select and configure a live provider and exchange calendar before enabling alerts.",
            "trigger": "price >= target",
            "already_above": "alert on first eligible quote",
        }
    return {
        "provider": heartbeat.provider,
        "exchange_calendar": heartbeat.exchange_calendar,
        "live_mode": "active",
        "alert_rules_enabled": True,
        "reason": "Live market price monitoring and configurable target alerts are active.",
        "trigger": "configurable (below / above / crosses)",
        "already_above": "alert on first eligible quote",
    }


@router.get("/worker/status")
def worker_status(session: DbSession) -> dict[str, object]:
    from .shoonya_feed import get_latest_feed_health

    heartbeat = session.get(WorkerHeartbeat, "price")
    now = datetime.now(timezone.utc)
    last_poll = normalize_utc(heartbeat.last_poll_at) if heartbeat and heartbeat.last_poll_at else None
    heartbeat_fresh = last_poll is not None and now - last_poll <= timedelta(minutes=3)
    live_provider = bool(heartbeat and heartbeat.provider and heartbeat.provider != "fixture")
    auth_calendar = bool(heartbeat and heartbeat.exchange_calendar and heartbeat.exchange_calendar != "fixture")
    pref = session.get(NotificationPreference, "default")
    live_notif_ready, notif_gate_reason = check_live_notification_gate()
    active_recipients = collect_alert_email_recipients(session, pref) if pref else []
    notif_mode = (
        "live_channels"
        if (
            live_notif_ready
            and pref
            and (pref.dashboard_enabled or (pref.email_enabled and active_recipients))
        )
        else "dry_run_only"
    )

    quote_by_company, _ = _load_quotes_and_states(session)
    companies = list(session.scalars(select(Company).where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))))
    stale_symbols: list[str] = []
    freshness_unknown_symbols: list[str] = []
    fresh_symbols_count = 0
    for c in companies:
        q = quote_by_company.get((c.exchange, c.symbol))
        if q is None:
            continue
        if q.get("status") == "freshness_unknown" or q.get("freshness_unknown"):
            freshness_unknown_symbols.append(f"{c.exchange}:{c.symbol}")
            stale_symbols.append(f"{c.exchange}:{c.symbol}")
        elif not q.get("is_fresh") and not q.get("is_simulated"):
            stale_symbols.append(f"{c.exchange}:{c.symbol}")
        elif q.get("is_fresh") and not q.get("is_simulated"):
            fresh_symbols_count += 1

    calendar = NseBseExchangeCalendar()
    market_open = calendar.is_open("NSE", now)
    feed_health = get_latest_feed_health()
    state = "error" if heartbeat and heartbeat.last_error else "running" if heartbeat_fresh else "stopped"
    connection_status = (
        heartbeat.connection_status
        if (heartbeat and state == "running")
        else "disconnected"
    )
    awaiting_snapshot = bool(feed_health.get("awaiting_snapshot", False)) if heartbeat and heartbeat.provider == "shoonya" else False
    feed_ready = bool(
        state == "running"
        and connection_status in {"connected", "polling"}
        and live_provider
        and fresh_symbols_count > 0
        and len(stale_symbols) == 0
        and not awaiting_snapshot
    )

    return {
        "state": state,
        "provider": heartbeat.provider if heartbeat else None,
        "exchange_calendar": heartbeat.exchange_calendar if heartbeat else None,
        "last_started_at": normalize_utc(heartbeat.started_at) if heartbeat else None,
        "last_poll_at": last_poll,
        "last_successful_quote_at": normalize_utc(heartbeat.last_successful_quote_at)
        if heartbeat and heartbeat.last_successful_quote_at
        else last_poll,
        "last_failed_at": normalize_utc(heartbeat.last_failed_at)
        if heartbeat and heartbeat.last_failed_at
        else None,
        "last_error": heartbeat.last_error if heartbeat else None,
        "connection_status": connection_status,
        "websocket_status": heartbeat.websocket_status if heartbeat else "unavailable",
        "failed_requests_count": heartbeat.failed_requests_count if heartbeat else 0,
        "reconnect_count": max(
            heartbeat.reconnect_count if heartbeat else 0,
            int(feed_health.get("reconnect_count", 0)),
        ),
        "monitored_symbols_count": len(companies),
        "fresh_symbols_count": fresh_symbols_count,
        "stale_symbols_count": len(stale_symbols),
        "stale_symbols": stale_symbols,
        "freshness_unknown_symbols_count": len(freshness_unknown_symbols),
        "freshness_unknown_symbols": freshness_unknown_symbols,
        "feed_ready": feed_ready,
        "awaiting_snapshot": awaiting_snapshot,
        "backlog_state": feed_health.get("backlog_state", "normal"),
        "lag_detected": bool(feed_health.get("lag_detected", False)),
        "queue_maxsize": int(feed_health.get("queue_maxsize", 256)),
        "queue_depth": int(feed_health.get("queue_depth", 0)),
        "queue_overflow_drops": int(feed_health.get("queue_overflow_drops", 0)),
        "protocol_gap_detection": feed_health.get("protocol_gap_detection", "disabled"),
        "protocol_gap_reason": feed_health.get("protocol_gap_reason"),
        "protocol_sequence_gaps": int(feed_health.get("protocol_sequence_gaps", 0)),
        "reconnect_gaps_count": int(feed_health.get("reconnect_gaps_count", 0)),
        "gap_history": feed_health.get("gap_history", []),
        "market_open": market_open,
        "live_provider_configured": live_provider,
        "authoritative_calendar_configured": auth_calendar,
        "live_alerts_enabled": live_provider and auth_calendar,
        "notification_mode": notif_mode,
        "notification_gate_reason": notif_gate_reason,
        "note": (
            "Live NSE/BSE monitoring active."
            if (live_provider and auth_calendar)
            else "Fixture worker data is simulated. Laptop sleep or offline time stops polling."
        ),
    }


@router.post("/price-feed/poll")
def trigger_manual_poll(
    session: DbSession,
    exchange: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
) -> dict[str, Any]:
    """Fetch live quotes on demand for one or all approved active companies and evaluate target rules.
    Uses Shoonya when enabled and automatically falls back to YFinanceProvider as a backup if Shoonya is unavailable or fails.
    """
    yfinance_backup = YFinanceProvider(stale_after_seconds=86400)
    use_shoonya = (
        os.environ.get("PRICE_PROVIDER", "").strip().lower() in {"shoonya", "shoonya_ws"}
        or bool(os.environ.get("SHOONYA_USER_ID"))
    )
    fallback_used = False
    if use_shoonya:
        from .shoonya_feed import ShoonyaPriceProvider

        try:
            provider: Any = ShoonyaPriceProvider()
        except (RuntimeError, ProviderError, ValueError):
            provider = yfinance_backup
            fallback_used = True
    else:
        provider = yfinance_backup
    provider_name = getattr(provider, "name", "yfinance")
    now = datetime.now(timezone.utc)
    if exchange and symbol:
        identity = (exchange.upper(), symbol.upper())
        company = session.get(Company, identity)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found.")
        companies = [company]
    else:
        companies = list(
            session.scalars(
                select(Company)
                .where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))
                .order_by(Company.exchange, Company.symbol)
            )
        )

    attempted = 0
    recorded = 0
    failed = 0
    errors: list[str] = []
    for company in companies:
        attempted += 1
        register_verified_ticker(company.exchange, company.symbol)
        try:
            try:
                quote = provider.fetch_quote(company.exchange, company.symbol)
            except (ProviderError, ValueError, RuntimeError):
                if provider is not yfinance_backup:
                    fallback_used = True
                    provider_name = "yfinance"
                    quote = yfinance_backup.fetch_quote(company.exchange, company.symbol)
                else:
                    raise
            eval_now = datetime.now(timezone.utc)
            if quote.quoted_at is not None and quote.quoted_at > eval_now:
                eval_now = quote.quoted_at
            record_quote(
                session,
                quote,
                now=eval_now,
                alerts_enabled=True,
                fixture_alerts_enabled=False,
            )
            recorded += 1
        except (ProviderError, ValueError, RuntimeError) as exc:
            failed += 1
            errors.append(f"{company.exchange}:{company.symbol}: {exc}")

    delivery = drain_outbox(MultiChannelNotifier(), session_factory=lambda: session, now=now)
    heartbeat = session.get(WorkerHeartbeat, "price")
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            name="price",
            provider=provider_name,
            exchange_calendar="nse_bse",
            started_at=now,
            last_poll_at=now,
            last_error=errors[0][:1000] if errors and recorded == 0 else None,
            connection_status="polling" if recorded > 0 else ("error" if errors else "connected"),
            websocket_status="fallback_polling",
            last_successful_quote_at=now if recorded > 0 else None,
            last_failed_at=now if failed > 0 else None,
            failed_requests_count=failed,
            monitored_symbols_count=len(companies),
        )
        session.add(heartbeat)
    else:
        heartbeat.provider = provider_name
        heartbeat.exchange_calendar = "nse_bse"
        heartbeat.last_poll_at = now
        heartbeat.last_error = errors[0][:1000] if errors and recorded == 0 else None
        heartbeat.connection_status = "polling" if recorded > 0 else ("error" if errors else "connected")
        heartbeat.websocket_status = "fallback_polling"
        if recorded > 0:
            heartbeat.last_successful_quote_at = now
        if failed > 0:
            heartbeat.last_failed_at = now
            heartbeat.failed_requests_count = (heartbeat.failed_requests_count or 0) + failed
    session.commit()

    return {
        "attempted": attempted,
        "recorded": recorded,
        "failed": failed,
        "provider": provider_name,
        "fallback_used": fallback_used,
        "errors": errors,
        "notifications_sent": delivery.sent,
        "notifications_retried": delivery.retried,
        "notifications_failed": delivery.failed,
    }


@router.get("/alerts")
def list_alerts(session: DbSession) -> list[dict[str, object]]:
    pref = session.get(NotificationPreference, "default")
    default_recipients = collect_alert_email_recipients(session, pref)
    rows = session.execute(
        select(TargetAlert, TargetRevision, PriceQuote, Company, NotificationOutbox)
        .join(TargetRevision, TargetRevision.id == TargetAlert.target_revision_id)
        .join(PriceQuote, PriceQuote.id == TargetAlert.quote_id)
        .join(
            Company,
            (Company.exchange == TargetRevision.exchange)
            & (Company.symbol == TargetRevision.symbol),
        )
        .outerjoin(
            NotificationOutbox,
            NotificationOutbox.target_alert_id == TargetAlert.id,
        )
        .order_by(TargetAlert.triggered_at.desc(), TargetAlert.id.desc())
    ).all()

    grouped: dict[int, dict[str, Any]] = {}
    for alert, revision, quote, company, notification in rows:
        if alert.id not in grouped:
            target = (
                alert.target_price_snapshot
                if alert.target_price_snapshot is not None
                else (
                    revision.base_target
                    if alert.target_kind == "base"
                    else revision.bull_target
                )
            )
            quote_data = serialize_quote(quote)
            breach_abs = (
                float(alert.price - target)
                if target is not None
                else None
            )
            breach_pct = (
                float(((alert.price - target) / target) * Decimal("100"))
                if target is not None and target != 0
                else None
            )
            preview_msg = NotificationMessage(
                outbox_id=notification.id if notification else f"preview-{alert.id}",
                target_alert_id=alert.id,
                exchange=company.exchange,
                symbol=company.symbol,
                target_kind=alert.target_kind,
                target_price=str(target) if target is not None else "0.00",
                quote_price=str(alert.price),
                triggered_at=alert.triggered_at,
                is_simulated=alert.is_simulated,
                company_name=company.name,
                comparison_direction=alert.comparison_direction or ("below" if alert.target_kind == "base" else "above"),
                channel=notification.channel if notification else "email",
                destination=notification.destination if notification else (",".join(default_recipients) if default_recipients else None),
                saved_cmp=str(company.cmp) if company.cmp is not None else None,
                sector=company.sector,
                trigger_cycle=alert.trigger_cycle or 1,
            )
            email_subject, email_body = format_alert_email(preview_msg)
            email_html = format_alert_email_html(preview_msg)
            grouped[alert.id] = {
                "id": alert.id,
                "exchange": company.exchange,
                "symbol": company.symbol,
                "company": company.name,
                "sector": company.sector,
                "saved_cmp": float(company.cmp) if company.cmp is not None else None,
                "target_kind": alert.target_kind,
                "target": float(target) if target is not None else None,
                "price": float(alert.price),
                "breach_abs": round(breach_abs, 2) if breach_abs is not None else None,
                "breach_pct": round(breach_pct, 2) if breach_pct is not None else None,
                "comparison_direction": alert.comparison_direction,
                "trigger_mode": alert.trigger_mode,
                "lifecycle_status": alert.lifecycle_status,
                "trigger_cycle": alert.trigger_cycle,
                "triggered_at": normalize_utc(alert.triggered_at),
                "acknowledged_at": normalize_utc(alert.acknowledged_at) if alert.acknowledged_at else None,
                "resolved_at": normalize_utc(alert.resolved_at) if alert.resolved_at else None,
                "reset_at": normalize_utc(alert.reset_at) if alert.reset_at else None,
                "target_revision_id": alert.target_revision_id,
                "is_simulated": alert.is_simulated,
                "event_type": "fixture" if alert.is_simulated else "live",
                "quote_status": quote.status,
                "quote_is_fresh": quote_data["is_fresh"],
                "provider": quote.provider,
                "delivery_status": notification.status if notification else "not_queued",
                "delivery_mode": notification.delivery_mode if notification else None,
                "channel": notification.channel if notification else None,
                "delivery_label": (
                    "Dry run (no network)"
                    if notification and notification.delivery_mode == "dry_run"
                    else f"{notification.channel.title()}: {notification.status}"
                    if notification
                    else "Not queued"
                ),
                "attempts": notification.attempts if notification else 0,
                "max_attempts": notification.max_attempts if notification else 0,
                "last_error": notification.last_error if notification else None,
                "sent_at": normalize_utc(notification.sent_at)
                if notification and notification.sent_at
                else None,
                "provider_message_id": notification.provider_message_id
                if notification
                else None,
                "email_subject": email_subject,
                "email_body": email_body,
                "email_html": email_html,
                "recipients": list(default_recipients),
                "channels": [],
            }
        if notification is not None:
            grouped[alert.id]["channels"].append(
                {
                    "id": notification.id,
                    "channel": notification.channel,
                    "destination": notification.destination,
                    "delivery_mode": notification.delivery_mode,
                    "status": notification.status,
                    "attempts": notification.attempts,
                    "max_attempts": notification.max_attempts,
                    "last_error": notification.last_error,
                    "sent_at": normalize_utc(notification.sent_at) if notification.sent_at else None,
                }
            )
            # Prefer email channel status in top-level summary when present
            if notification.channel == "email":
                grouped[alert.id]["delivery_status"] = notification.status
                grouped[alert.id]["delivery_mode"] = notification.delivery_mode
                grouped[alert.id]["channel"] = notification.channel
                grouped[alert.id]["delivery_label"] = f"Email: {notification.status}"
                grouped[alert.id]["attempts"] = notification.attempts
                grouped[alert.id]["max_attempts"] = notification.max_attempts
                grouped[alert.id]["last_error"] = notification.last_error
                grouped[alert.id]["sent_at"] = (
                    normalize_utc(notification.sent_at) if notification.sent_at else None
                )
                if notification.destination:
                    grouped[alert.id]["recipients"] = [
                        addr.strip() for addr in notification.destination.split(",") if addr.strip()
                    ]
    return list(grouped.values())


@router.post("/alerts/{alert_id}/action")
def alert_action(
    alert_id: int,
    payload: AlertActionRequest,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    try:
        updated = apply_alert_lifecycle_action(
            session,
            alert_id,
            payload.action,
            actor=user.name,
        )
        record_activity(session, user, f"alert_{payload.action}", {"alert_id": alert_id})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "id": updated.id,
        "lifecycle_status": updated.lifecycle_status,
        "acknowledged_at": normalize_utc(updated.acknowledged_at) if updated.acknowledged_at else None,
        "resolved_at": normalize_utc(updated.resolved_at) if updated.resolved_at else None,
        "reset_at": normalize_utc(updated.reset_at) if updated.reset_at else None,
    }


@router.post("/notifications/drain")
def trigger_outbox_drain(session: DbSession) -> dict[str, int]:
    result = drain_outbox(MultiChannelNotifier(), session_factory=lambda: session)
    return {
        "sent": result.sent,
        "retried": result.retried,
        "failed": result.failed,
    }


@router.post("/alerts/{alert_id}/resend")
def resend_alert_notification(alert_id: int, session: DbSession) -> dict[str, Any]:
    alert = session.get(TargetAlert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"Target alert {alert_id} not found.")
    now = datetime.now(timezone.utc)
    outbox_rows = list(
        session.scalars(
            select(NotificationOutbox).where(NotificationOutbox.target_alert_id == alert_id)
        )
    )
    live_ready, _ = check_live_notification_gate()
    if outbox_rows and (not live_ready or any(r.channel != "dry-run" for r in outbox_rows)):
        all_recipients = collect_alert_email_recipients(session)
        for row in outbox_rows:
            if row.channel == "email" and all_recipients:
                row.destination = ",".join(all_recipients)
            row.status = "queued"
            row.attempts = 0
            row.next_attempt_at = now
            row.claimed_at = None
            row.claim_token = None
            row.last_error = None
        session.commit()
    else:
        for row in outbox_rows:
            session.delete(row)
        session.flush()
        queue_alert_notifications(session, alert, now=now)
        session.commit()

    result = drain_outbox(MultiChannelNotifier(), session_factory=lambda: session, now=now)
    return {
        "alert_id": alert_id,
        "sent": result.sent,
        "retried": result.retried,
        "failed": result.failed,
    }


@router.post("/notifications/test-preview")
def generate_test_alert_preview(
    session: DbSession,
    symbol: str | None = Query(default=None),
    target_kind: str = Query(default="base"),
) -> dict[str, Any]:
    pref = get_or_create_preferences(session)
    session.commit()
    company: Company | None = None
    if symbol:
        company = session.scalar(
            select(Company).where(Company.symbol == symbol.strip().upper())
        )
    if company is None:
        company = session.scalar(select(Company).order_by(Company.symbol.asc()).limit(1))

    now = datetime.now(timezone.utc)
    chosen_kind = "bull" if target_kind.lower() == "bull" else "base"
    if company is not None:
        exch = company.exchange
        sym = company.symbol
        comp_name = company.name
        sector = company.sector
        saved_cmp = str(company.cmp) if company.cmp is not None else "1480.00"
        if chosen_kind == "base":
            t_val = company.base_target if company.base_target is not None else Decimal("1250.00")
            q_val = (t_val * Decimal("0.985")).quantize(Decimal("0.01"))
            direction = company.base_direction or "below"
        else:
            t_val = company.bull_target if company.bull_target is not None else Decimal("1850.00")
            q_val = (t_val * Decimal("1.018")).quantize(Decimal("0.01"))
            direction = company.bull_direction or "above"
    else:
        exch = "NSE"
        sym = "RELIANCE"
        comp_name = "Reliance Industries Ltd"
        sector = "Energy & Conglomerate"
        saved_cmp = "1425.00"
        if chosen_kind == "base":
            t_val = Decimal("1350.00")
            q_val = Decimal("1334.50")
            direction = "below"
        else:
            t_val = Decimal("1650.00")
            q_val = Decimal("1672.80")
            direction = "above"

    breach_abs = float(q_val - t_val)
    breach_pct = float(((q_val - t_val) / t_val) * Decimal("100")) if t_val != 0 else 0.0
    env_recipients = [
        addr.strip()
        for addr in os.environ.get("ALERT_EMAIL_RECIPIENTS", "").split(",")
        if addr.strip() and "@" in addr.strip()
    ]
    combined = collect_alert_email_recipients(session, pref)
    recipients = list(combined or env_recipients)
    msg = NotificationMessage(
        outbox_id="preview-sample",
        target_alert_id=0,
        exchange=exch,
        symbol=sym,
        target_kind=chosen_kind,
        target_price=str(t_val),
        quote_price=str(q_val),
        triggered_at=now,
        is_simulated=True,
        company_name=comp_name,
        comparison_direction=direction,
        channel="email",
        destination=",".join(recipients) if recipients else "alerts@plutuswealth.local",
        saved_cmp=saved_cmp,
        sector=sector,
        trigger_cycle=1,
    )
    email_subject, email_body = format_alert_email(msg)
    email_html = format_alert_email_html(msg)
    smtp_ok, missing_smtp = is_smtp_complete()
    live_ready, gate_reason = check_live_notification_gate()
    return {
        "exchange": exch,
        "symbol": sym,
        "company": comp_name,
        "sector": sector,
        "saved_cmp": float(saved_cmp) if saved_cmp else None,
        "target_kind": chosen_kind,
        "target": float(t_val),
        "price": float(q_val),
        "breach_abs": round(breach_abs, 2),
        "breach_pct": round(breach_pct, 2),
        "comparison_direction": direction,
        "triggered_at": now.isoformat(),
        "email_subject": email_subject,
        "email_body": email_body,
        "email_html": email_html,
        "recipients": recipients,
        "smtp_configured": smtp_ok,
        "missing_smtp": missing_smtp,
        "live_delivery_ready": live_ready,
        "delivery_gate_reason": gate_reason,
    }


@router.post("/notifications/send-sample")
def send_sample_alert_email(
    session: DbSession,
    symbol: str | None = Query(default=None),
    target_kind: str = Query(default="base"),
    recipient: str | None = Query(default=None),
) -> dict[str, Any]:
    preview = generate_test_alert_preview(session, symbol=symbol, target_kind=target_kind)
    recipients: list[str] = []
    if recipient and "@" in recipient:
        recipients = [addr.strip() for addr in recipient.split(",") if addr.strip() and "@" in addr.strip()]
    else:
        recipients = list(preview.get("recipients") or [])

    if not recipients:
        raise HTTPException(
            status_code=422,
            detail="No recipient email configured. Add your email in Settings -> Approved Email Recipients or ALERT_EMAIL_RECIPIENTS in .env.",
        )

    now = datetime.now(timezone.utc)
    msg = NotificationMessage(
        outbox_id="sample-email",
        target_alert_id=0,
        exchange=str(preview["exchange"]),
        symbol=str(preview["symbol"]),
        target_kind="bull" if str(preview["target_kind"]) == "bull" else "base",
        target_price=str(preview["target"]),
        quote_price=str(preview["price"]),
        triggered_at=now,
        is_simulated=False,
        company_name=str(preview["company"]),
        comparison_direction="below" if str(preview["comparison_direction"]) == "below" else "above",
        channel="email",
        destination=",".join(recipients),
        saved_cmp=str(preview["saved_cmp"]) if preview.get("saved_cmp") is not None else None,
        sector=str(preview["sector"]) if preview.get("sector") else None,
        trigger_cycle=1,
    )
    idempotency_key = f"sample-{int(now.timestamp())}"

    if "PYTEST_CURRENT_TEST" in os.environ:
        receipt = DryRunNotifier().send(msg, idempotency_key)
        return {
            "status": "dry_run",
            "recipients": recipients,
            "subject": preview["email_subject"],
            "provider_message_id": receipt.provider_message_id,
        }

    smtp_ok, missing = is_smtp_complete()
    if not smtp_ok:
        raise HTTPException(
            status_code=422,
            detail=f"SMTP settings are incomplete in .env (missing: {', '.join(missing)}).",
        )

    try:
        receipt = SmtpEmailNotifier().send(msg, idempotency_key)
    except (PermanentSendError, RetryableSendError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "status": "sent",
        "recipients": recipients,
        "subject": preview["email_subject"],
        "provider_message_id": receipt.provider_message_id,
    }


@router.get("/notifications/preferences")
def read_notification_preferences(session: DbSession) -> dict[str, Any]:
    pref = get_or_create_preferences(session)
    session.commit()
    smtp_ok, _ = is_smtp_complete()
    return {
        "id": pref.id,
        "dashboard_enabled": pref.dashboard_enabled,
        "email_enabled": pref.email_enabled,
        "email_recipients": collect_alert_email_recipients(session, pref),
        "notify_on_base": pref.notify_on_base,
        "notify_on_bull": pref.notify_on_bull,
        "notify_on_stale": pref.notify_on_stale,
        "smtp_configured": smtp_ok,
        "updated_at": normalize_utc(pref.updated_at),
    }


@router.put("/notifications/preferences")
def update_notification_preferences(
    payload: NotificationPreferenceUpdate,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    pref = get_or_create_preferences(session)
    now = datetime.now(timezone.utc)
    fields = payload.model_fields_set
    if "dashboard_enabled" in fields and payload.dashboard_enabled is not None:
        pref.dashboard_enabled = payload.dashboard_enabled
    if "email_enabled" in fields and payload.email_enabled is not None:
        pref.email_enabled = payload.email_enabled
    if "email_recipients" in fields and payload.email_recipients is not None:
        raw_list = (
            payload.email_recipients.split(",")
            if isinstance(payload.email_recipients, str)
            else payload.email_recipients
        )
        cleaned = [addr.strip() for addr in raw_list if addr.strip()]
        for addr in cleaned:
            if "@" not in addr or "." not in addr.split("@")[-1]:
                raise HTTPException(status_code=422, detail=f"Invalid email address: {addr}")
        pref.email_recipients = cleaned
    if "notify_on_base" in fields and payload.notify_on_base is not None:
        pref.notify_on_base = payload.notify_on_base
    if "notify_on_bull" in fields and payload.notify_on_bull is not None:
        pref.notify_on_bull = payload.notify_on_bull
    if "notify_on_stale" in fields and payload.notify_on_stale is not None:
        pref.notify_on_stale = payload.notify_on_stale
    pref.updated_at = now
    session.add(
        AuditLog(
            action="notification_preferences_updated",
            actor=user.name,
            user_id=user.id,
            entity_type="notification_preferences",
            entity_id=pref.id,
            details={
                "dashboard_enabled": pref.dashboard_enabled,
                "email_enabled": pref.email_enabled,
                "email_recipients": pref.email_recipients,
            },
            created_at=now,
        )
    )
    session.commit()
    smtp_ok, _ = is_smtp_complete()
    return {
        "id": pref.id,
        "dashboard_enabled": pref.dashboard_enabled,
        "email_enabled": pref.email_enabled,
        "email_recipients": pref.email_recipients or [],
        "notify_on_base": pref.notify_on_base,
        "notify_on_bull": pref.notify_on_bull,
        "notify_on_stale": pref.notify_on_stale,
        "smtp_configured": smtp_ok,
        "updated_at": normalize_utc(pref.updated_at),
    }


@router.get(
    "/companies/{exchange}/{symbol}/history", response_model=PriceHistoryRead
)
def company_price_history(
    exchange: str,
    symbol: str,
    session: DbSession,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    identity = (exchange.upper(), symbol.upper())
    if session.get(Company, identity) is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    quotes = session.scalars(
        select(PriceQuote)
        .where(
            PriceQuote.exchange == identity[0],
            PriceQuote.symbol == identity[1],
        )
        .order_by(
            func.coalesce(PriceQuote.quoted_at, PriceQuote.received_at).desc(),
            PriceQuote.id.desc(),
        )
        .limit(limit)
    )
    return {
        "exchange": identity[0],
        "symbol": identity[1],
        "items": [serialize_quote(quote) for quote in quotes],
    }


@router.put(
    "/companies/{exchange}/{symbol}/targets",
    response_model=CompanyRead,
)
def update_company_targets(
    exchange: str, symbol: str, update: TargetUpdate, session: DbSession, user: CurrentUser
) -> CompanyRead:
    identity = (exchange.upper(), symbol.upper())
    company = session.get(Company, identity)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    if company.version != update.expected_version:
        raise HTTPException(
            status_code=409,
            detail=f"Targets changed since they were loaded (current version {company.version}). Refresh and try again.",
        )
    fields = update.model_fields_set - {"expected_version", "actor"}
    if not fields:
        raise HTTPException(status_code=422, detail="Provide at least one target or configuration field to update.")
    for field in ("base_target", "bull_target"):
        if field in fields:
            value = getattr(update, field)
            if value is not None and (
                not value.is_finite()
                or value <= 0
                or value >= Decimal("1000000000000")
                or value.as_tuple().exponent < -2
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"{field} must be finite, positive, and have at most 12 integer digits and two decimal places.",
                )
    old_targets = (company.base_target, company.bull_target)
    for field in fields:
        val = getattr(update, field)
        if field in ("base_target", "bull_target") or val is not None:
            setattr(company, field, val)
    new_targets = (company.base_target, company.bull_target)
    now = datetime.now(timezone.utc)
    if old_targets != new_targets:
        company.version += 1
        session.add(
            TargetRevision(
                exchange=company.exchange,
                symbol=company.symbol,
                version=company.version,
                previous_base_target=old_targets[0],
                previous_bull_target=old_targets[1],
                base_target=company.base_target,
                bull_target=company.bull_target,
                base_direction=company.base_direction,
                bull_direction=company.bull_direction,
                source="manual",
                changed_by=user.name,
                changed_by_user_id=user.id,
                changed_at=now,
            )
        )
        record_activity(
            session,
            user,
            "target_prices_changed",
            {
                "exchange": company.exchange,
                "symbol": company.symbol,
                "previous_base_target": float(old_targets[0]) if old_targets[0] is not None else None,
                "previous_bull_target": float(old_targets[1]) if old_targets[1] is not None else None,
                "base_target": float(company.base_target) if company.base_target is not None else None,
                "bull_target": float(company.bull_target) if company.bull_target is not None else None,
            },
        )
    session.add(
        AuditLog(
            action="company_targets_updated",
            actor=user.name,
            user_id=user.id,
            entity_type="company",
            entity_id=f"{company.exchange}:{company.symbol}",
            details={
                "previous_base_target": float(old_targets[0]) if old_targets[0] is not None else None,
                "previous_bull_target": float(old_targets[1]) if old_targets[1] is not None else None,
                "base_target": float(company.base_target) if company.base_target is not None else None,
                "bull_target": float(company.bull_target) if company.bull_target is not None else None,
                "base_direction": company.base_direction,
                "bull_direction": company.bull_direction,
                "base_trigger_mode": company.base_trigger_mode,
                "bull_trigger_mode": company.bull_trigger_mode,
                "rearm_policy": company.rearm_policy,
                "version": company.version,
            },
            created_at=now,
        )
    )
    try:
        session.commit()
    except StaleDataError as error:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Targets changed while saving. Refresh and try again.",
        ) from error
    session.refresh(company)
    quote_by_company, states_by_key = _load_quotes_and_states(session)
    return _serialize_company(session, company, quote_by_company, states_by_key)


@router.put(
    "/companies/{exchange}/{symbol}/target-config",
    response_model=CompanyRead,
)
def update_company_target_config(
    exchange: str, symbol: str, config: TargetConfigUpdate, session: DbSession, user: CurrentUser
) -> CompanyRead:
    identity = (exchange.upper(), symbol.upper())
    company = session.get(Company, identity)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    fields = config.model_fields_set - {"actor"}
    for field in fields:
        val = getattr(config, field)
        if val is not None:
            setattr(company, field, val)
    now = datetime.now(timezone.utc)
    session.add(
        AuditLog(
            action="target_config_updated",
            actor=user.name,
            user_id=user.id,
            entity_type="company",
            entity_id=f"{company.exchange}:{company.symbol}",
            details={
                "base_direction": company.base_direction,
                "bull_direction": company.bull_direction,
                "base_trigger_mode": company.base_trigger_mode,
                "bull_trigger_mode": company.bull_trigger_mode,
                "rearm_policy": company.rearm_policy,
                "cooldown_minutes": company.cooldown_minutes,
                "monitoring_active": company.monitoring_active,
            },
            created_at=now,
        )
    )
    record_activity(session, user, "target_configuration_changed", {"exchange": company.exchange, "symbol": company.symbol})
    session.commit()
    session.refresh(company)
    quote_by_company, states_by_key = _load_quotes_and_states(session)
    return _serialize_company(session, company, quote_by_company, states_by_key)


@router.post("/imports/inspect")
async def inspect_import(
    file: Annotated[UploadFile, File()],
    header_row: Annotated[int, Form()] = 1,
) -> dict:
    filename, content = await read_upload(file)
    return inspect_upload(content, filename, header_row)


@router.post("/imports/preview")
async def preview_import(
    session: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
    header_row: Annotated[int, Form()] = 1,
    sheet_name: Annotated[str | None, Form()] = None,
    exchange_column: Annotated[str | None, Form()] = None,
    symbol_column: Annotated[str | None, Form()] = None,
    name_column: Annotated[str | None, Form()] = None,
    base_target_column: Annotated[str | None, Form()] = None,
    bull_target_column: Annotated[str | None, Form()] = None,
    cmp_column: Annotated[str | None, Form()] = None,
    sector_column: Annotated[str | None, Form()] = None,
) -> dict:
    filename, content = await read_upload(file)
    if PurePath(filename).suffix.lower() == ".xlsx" and not sheet_name:
        raise HTTPException(status_code=422, detail="Select a worksheet to preview.")
    preview = create_preview(
        session,
        filename=filename,
        content=content,
        sheet_name=sheet_name,
        header_row=header_row,
        mapping={
            "exchange_column": exchange_column,
            "symbol_column": symbol_column,
            "name_column": name_column,
            "base_target_column": base_target_column,
            "bull_target_column": bull_target_column,
            "cmp_column": cmp_column,
            "sector_column": sector_column,
        },
        uploaded_by=user.name,
        uploader_user_id=user.id,
        row_approval_status="approved",
    )
    record_activity(session, user, "import_uploaded", {"batch_id": preview["id"], "filename": filename})
    session.commit()
    return preview


@router.get("/imports")
def list_imports(session: DbSession, user: CurrentUser) -> list[dict[str, Any]]:
    query = select(ImportBatch).options(selectinload(ImportBatch.rows))
    batches = list(session.scalars(query.order_by(ImportBatch.created_at.desc()).limit(100)))
    return [
        {
            "id": b.id,
            "filename": b.filename,
            "status": b.status,
            "version_number": b.version_number,
            "uploaded_by": b.uploaded_by,
            "uploader_user_id": b.uploader_user_id,
            "approved_by": b.approved_by,
            "approver_user_id": b.approver_user_id,
            "rejector_user_id": b.rejector_user_id,
            "approved_at": normalize_utc(b.approved_at) if b.approved_at else None,
            "created_at": normalize_utc(b.created_at),
            "sheet_name": b.sheet_name,
            "row_count": len(b.rows),
            "result": b.result,
            "changes_snapshot": b.changes_snapshot,
        }
        for b in batches
    ]


@router.get("/imports/{batch_id}")
def read_import(batch_id: str, session: DbSession, user: CurrentUser) -> dict:
    batch = get_batch(session, batch_id)
    companies = {
        (company.exchange, company.symbol): company
        for company in session.scalars(select(Company))
    }
    return serialize_batch(batch, companies)


@router.put("/imports/{batch_id}/rows/{row_number}")
def edit_import_row(
    batch_id: str,
    row_number: int,
    payload: ImportRowUpdate,
    session: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    return update_preview_row(session, batch_id, row_number, payload)


@router.post("/imports/{batch_id}/reject")
def reject_import(batch_id: str, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    batch = get_batch(session, batch_id)
    if batch.status == "confirmed":
        raise HTTPException(status_code=409, detail="Confirmed imports cannot be rejected.")
    batch.status = "rejected"
    batch.rejector_user_id = user.id
    session.add(
        AuditLog(
            action="import_rejected",
            actor=user.name,
            user_id=user.id,
            entity_type="import_batch",
            entity_id=batch.id,
            details={"filename": batch.filename},
            created_at=datetime.now(timezone.utc),
        )
    )
    record_activity(session, user, "import_rejected", {"batch_id": batch.id})
    session.commit()
    return {"id": batch.id, "status": "rejected"}


@router.post("/imports/{batch_id}/restore")
def restore_import(batch_id: str, session: DbSession, user: CurrentUser) -> dict[str, Any]:
    return restore_import_version(session, batch_id, actor=user.name, user_id=user.id)


@router.post("/imports/{batch_id}/confirm")
def confirm_import(
    batch_id: str, confirmation: ImportConfirm, session: DbSession, user: CurrentUser
) -> dict:
    batch = session.scalar(
        select(ImportBatch)
        .options(selectinload(ImportBatch.rows))
        .where(ImportBatch.id == batch_id)
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Import preview not found.")
    if batch.status == "confirmed":
        return batch.result or {"id": batch.id, "status": "confirmed"}
    if batch.status == "rejected":
        raise HTTPException(status_code=409, detail="Rejected imports cannot be confirmed.")

    approved_rows = [row for row in batch.rows if row.approval_status != "rejected"]
    if not approved_rows:
        raise HTTPException(status_code=409, detail="At least one row must be approved to confirm import.")
    invalid_rows = [row.row_number for row in approved_rows if row.errors]
    if invalid_rows:
        raise HTTPException(
            status_code=409,
            detail=f"Resolve validation errors in rows: {', '.join(map(str, invalid_rows))}.",
        )
    if batch.formula_review_required and not confirmation.review_cached_targets:
        raise HTTPException(
            status_code=409,
            detail="Review the cached formula target values before confirming this import.",
        )

    for row in approved_rows:
        if row.approval_status == "pending":
            row.approval_status = "approved"

    identities = [(row.exchange, row.symbol) for row in approved_rows]
    companies = {
        identity: company
        for identity in identities
        if identity[0] is not None and identity[1] is not None
        if (company := session.get(Company, identity)) is not None
    }
    existing_identities = set(companies)
    for row in approved_rows:
        identity = (row.exchange or "", row.symbol or "")
        existing = companies.get(identity)
        if existing and row.expected_version != existing.version:
            raise HTTPException(
                status_code=409,
                detail=f"{identity[0]}:{identity[1]} changed after preview. Create a fresh preview before confirming.",
            )

    max_ver = session.scalar(select(func.max(ImportBatch.version_number))) or 0
    next_version_number = max_ver + 1

    created = 0
    updated = 0
    unchanged = 0
    rejected_count = len(batch.rows) - len(approved_rows)
    changes_snapshot: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    try:
        for row in approved_rows:
            identity = (row.exchange or "", row.symbol or "")
            company = companies.get(identity)
            is_new = company is None
            if company is None:
                company = Company(
                    exchange=identity[0],
                    symbol=identity[1],
                    name=row.name or "",
                    is_approved=True,
                    monitoring_active=True,
                )
                session.add(company)
                session.flush()
                companies[identity] = company
                created += 1
            else:
                company.is_approved = True
                if row.name:
                    company.name = row.name

            if row.update_sector:
                company.sector = row.sector
            if row.update_cmp:
                company.cmp = row.cmp

            old_targets = (company.base_target, company.bull_target)
            if row.update_base_target and row.base_target is not None:
                company.base_target = row.base_target
            if row.update_bull_target and row.bull_target is not None:
                company.bull_target = row.bull_target

            if confirmation.default_base_direction:
                company.base_direction = confirmation.default_base_direction
            elif row.base_direction:
                company.base_direction = row.base_direction

            if confirmation.default_bull_direction:
                company.bull_direction = confirmation.default_bull_direction
            elif row.bull_direction:
                company.bull_direction = row.bull_direction

            company.source_metadata = {
                "import_id": batch.id,
                "version_number": next_version_number,
                "filename": batch.filename,
                "sheet_name": batch.sheet_name,
                "row_number": row.row_number,
                "cell_refs": row.cell_refs or {},
                "imported_at": now.isoformat(),
                "approved_by": user.name,
            }
            register_verified_ticker(company.exchange, company.symbol)

            changes_snapshot.append(
                {
                    "exchange": company.exchange,
                    "symbol": company.symbol,
                    "name": company.name,
                    "row_number": row.row_number,
                    "cell_refs": row.cell_refs or {},
                    "previous_base_target": float(old_targets[0]) if old_targets[0] is not None and not is_new else None,
                    "previous_bull_target": float(old_targets[1]) if old_targets[1] is not None and not is_new else None,
                    "new_base_target": float(company.base_target) if company.base_target is not None else None,
                    "new_bull_target": float(company.bull_target) if company.bull_target is not None else None,
                    "base_direction": company.base_direction,
                    "bull_direction": company.bull_direction,
                }
            )

            if old_targets != (company.base_target, company.bull_target):
                company.version += 1
                if identity in existing_identities:
                    updated += 1
                session.add(
                    TargetRevision(
                        exchange=company.exchange,
                        symbol=company.symbol,
                        version=company.version,
                        previous_base_target=old_targets[0] if not is_new else None,
                        previous_bull_target=old_targets[1] if not is_new else None,
                        base_target=company.base_target,
                        bull_target=company.bull_target,
                        base_direction=company.base_direction,
                        bull_direction=company.bull_direction,
                        source="import",
                        import_id=batch.id,
                        changed_by=user.name,
                        changed_by_user_id=user.id,
                        cell_refs=row.cell_refs,
                        changed_at=now,
                    )
                )
            elif identity in existing_identities:
                unchanged += 1
        session.flush()
        batch.status = "confirmed"
        batch.version_number = next_version_number
        batch.approved_by = user.name
        batch.approver_user_id = user.id
        batch.approved_at = now
        batch.changes_snapshot = changes_snapshot
        batch.result = {
            "id": batch.id,
            "status": "confirmed",
            "version_number": next_version_number,
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "rejected": rejected_count,
        }
        session.add(
            AuditLog(
                action="import_confirmed",
                actor=user.name,
                user_id=user.id,
                entity_type="import_batch",
                entity_id=batch.id,
                details={
                    "filename": batch.filename,
                    "version_number": next_version_number,
                    "created": created,
                    "updated": updated,
                    "unchanged": unchanged,
                    "rejected": rejected_count,
                },
                created_at=now,
            )
        )
        record_activity(session, user, "import_approved", {"batch_id": batch.id, "version_number": next_version_number})
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A company identity changed during confirmation. Reload and preview the file again.",
        ) from error
    except StaleDataError as error:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A company target changed during confirmation. Reload and preview the file again.",
        ) from error
    except Exception:
        session.rollback()
        raise
    return batch.result


@router.get("/audit-logs", dependencies=[Depends(require_admin)])
def list_audit_logs(
    session: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    logs = session.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    )
    return [
        {
            "id": log.id,
            "action": log.action,
            "actor": log.actor,
            "user_id": log.user_id,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": normalize_utc(log.created_at),
        }
        for log in logs
    ]


@router.post("/workspace/reset", dependencies=[Depends(require_admin)])
@router.delete("/workspace/reset", dependencies=[Depends(require_admin)])
def reset_workspace_data(session: DbSession, user: AdminUser) -> dict[str, Any]:
    """Clear all companies, import batches, quotes, alerts, target revisions, and audit logs."""
    deleted_companies = session.scalar(select(func.count()).select_from(Company)) or 0
    session.execute(delete(NotificationOutbox))
    session.execute(delete(TargetAlert))
    session.execute(delete(TargetAlertState))
    session.execute(delete(TargetRevision))
    session.execute(delete(PriceQuote))
    session.execute(delete(ImportRow))
    session.execute(delete(ImportBatch))
    session.execute(delete(Company))
    session.execute(delete(AuditLog))
    session.execute(delete(WorkerHeartbeat))
    session.commit()
    record_activity(session, user, "workspace_reset", {})
    session.commit()
    return {
        "status": "cleared",
        "deleted_companies": int(deleted_companies),
    }


def _serialize_single_outbox_alert(
    outbox: NotificationOutbox,
    alert: TargetAlert,
    revision: TargetRevision,
    quote: PriceQuote,
    company: Company,
) -> dict[str, Any]:
    target = (
        alert.target_price_snapshot
        if alert.target_price_snapshot is not None
        else (revision.base_target if alert.target_kind == "base" else revision.bull_target)
    )
    quote_data = serialize_quote(quote)
    price_dir = "Downward" if alert.comparison_direction == "below" else "Upward"
    breach_abs = float(alert.price - target) if target is not None else None
    breach_pct = (
        float(((alert.price - target) / target) * Decimal("100"))
        if target is not None and target != 0
        else None
    )
    preview_msg = NotificationMessage(
        outbox_id=outbox.id,
        target_alert_id=alert.id,
        exchange=company.exchange,
        symbol=company.symbol,
        target_kind=alert.target_kind,
        target_price=str(target) if target is not None else "0.00",
        quote_price=str(alert.price),
        triggered_at=alert.triggered_at,
        is_simulated=alert.is_simulated,
        company_name=company.name,
        comparison_direction=alert.comparison_direction or ("below" if alert.target_kind == "base" else "above"),
        channel=outbox.channel,
        destination=outbox.destination,
        saved_cmp=str(company.cmp) if company.cmp is not None else None,
        sector=company.sector,
        trigger_cycle=alert.trigger_cycle or 1,
    )
    email_subject, email_body = format_alert_email(preview_msg)
    email_html = format_alert_email_html(preview_msg)
    return {
        "outbox_id": outbox.id,
        "channel": outbox.channel,
        "delivery_mode": outbox.delivery_mode,
        "status": outbox.status,
        "created_at": normalize_utc(outbox.created_at).isoformat(),
        "alert": {
            "id": alert.id,
            "exchange": company.exchange,
            "symbol": company.symbol,
            "company": company.name,
            "sector": company.sector,
            "saved_cmp": float(company.cmp) if company.cmp is not None else None,
            "target_kind": alert.target_kind,
            "target": float(target) if target is not None else None,
            "price": float(alert.price),
            "breach_abs": round(breach_abs, 2) if breach_abs is not None else None,
            "breach_pct": round(breach_pct, 2) if breach_pct is not None else None,
            "comparison_direction": alert.comparison_direction,
            "price_direction": price_dir,
            "trigger_mode": alert.trigger_mode,
            "lifecycle_status": alert.lifecycle_status,
            "trigger_cycle": alert.trigger_cycle,
            "triggered_at": normalize_utc(alert.triggered_at).isoformat(),
            "target_revision_id": alert.target_revision_id,
            "is_simulated": alert.is_simulated,
            "event_type": "fixture" if alert.is_simulated else "live",
            "quote_status": quote.status,
            "quote_is_fresh": quote_data["is_fresh"],
            "provider": quote.provider,
            "delivery_status": outbox.status,
            "delivery_mode": outbox.delivery_mode,
            "channel": outbox.channel,
            "delivery_label": (
                "Dry run (no network)"
                if outbox.delivery_mode == "dry_run"
                else f"{outbox.channel.title()}: {outbox.status}"
            ),
            "attempts": outbox.attempts,
            "max_attempts": outbox.max_attempts,
            "last_error": outbox.last_error,
            "sent_at": normalize_utc(outbox.sent_at).isoformat() if outbox.sent_at else None,
            "provider_message_id": outbox.provider_message_id,
            "email_subject": email_subject,
            "email_body": email_body,
            "email_html": email_html,
            "recipients": [
                addr.strip() for addr in (outbox.destination or "").split(",") if addr.strip() and "@" in addr
            ],
        },
    }


def _poll_stream_delta(
    session: Session,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Query newly recorded PriceQuote rows and newly queued NotificationOutbox rows since the last cursor."""
    events: list[dict[str, Any]] = []
    last_quote_id: int = state.get("last_quote_id", 0)
    seen_outbox_ids: set[str] = state.setdefault("seen_outbox_ids", set())
    last_outbox_created_at: datetime | None = state.get("last_outbox_created_at")

    new_quotes = list(
        session.scalars(
            select(PriceQuote)
            .where(PriceQuote.id > last_quote_id)
            .order_by(PriceQuote.id.asc())
            .limit(100)
        )
    )
    if new_quotes:
        state["last_quote_id"] = new_quotes[-1].id
        # Group by latest quote per company so distance metrics are computed once per symbol
        latest_by_company: dict[tuple[str, str], PriceQuote] = {}
        for q in new_quotes:
            latest_by_company[(q.exchange, q.symbol)] = q

        for (exch, sym), quote in latest_by_company.items():
            company = session.get(Company, (exch, sym))
            if company is None:
                continue
            quote_data = serialize_quote(quote)
            state_rows = list(
                session.scalars(
                    select(TargetAlertState).where(
                        TargetAlertState.exchange == exch,
                        TargetAlertState.symbol == sym,
                        TargetAlertState.is_simulated.is_(False),
                    )
                )
            )
            states_map = {st.target_kind: st for st in state_rows}
            company_dict = CompanyRead.model_validate(company).model_dump()
            enrich_company_dict(
                company,
                company_dict,
                quote_data,
                states_map.get("base"),
                states_map.get("bull"),
            )
            events.append(
                {
                    "type": "price_tick",
                    "exchange": exch,
                    "symbol": sym,
                    "company_name": company.name,
                    "quote": {
                        **quote_data,
                        "quoted_at": normalize_utc(quote.quoted_at).isoformat()
                        if quote.quoted_at is not None
                        else None,
                        "received_at": normalize_utc(quote.received_at).isoformat(),
                    },
                    "base_target": float(company.base_target) if company.base_target is not None else None,
                    "bull_target": float(company.bull_target) if company.bull_target is not None else None,
                    "base_direction": company.base_direction,
                    "bull_direction": company.bull_direction,
                    "distance_to_base_abs": company_dict.get("distance_to_base_abs"),
                    "distance_to_base_pct": company_dict.get("distance_to_base_pct"),
                    "distance_to_bull_abs": company_dict.get("distance_to_bull_abs"),
                    "distance_to_bull_pct": company_dict.get("distance_to_bull_pct"),
                    "nearest_target_pct": company_dict.get("nearest_target_pct"),
                    "alert_status": company_dict.get("alert_status"),
                    "base_alert_state": company_dict.get("base_alert_state"),
                    "bull_alert_state": company_dict.get("bull_alert_state"),
                }
            )

    outbox_stmt = (
        select(NotificationOutbox, TargetAlert, TargetRevision, PriceQuote, Company)
        .join(TargetAlert, TargetAlert.id == NotificationOutbox.target_alert_id)
        .join(TargetRevision, TargetRevision.id == TargetAlert.target_revision_id)
        .join(PriceQuote, PriceQuote.id == TargetAlert.quote_id)
        .join(
            Company,
            (Company.exchange == TargetRevision.exchange)
            & (Company.symbol == TargetRevision.symbol),
        )
    )
    if last_outbox_created_at is not None:
        outbox_stmt = outbox_stmt.where(
            NotificationOutbox.created_at >= last_outbox_created_at - timedelta(seconds=2)
        )
    outbox_rows = session.execute(
        outbox_stmt.order_by(NotificationOutbox.created_at.asc()).limit(50)
    ).all()

    for outbox, alert, revision, quote, company in outbox_rows:
        if outbox.id in seen_outbox_ids:
            continue
        seen_outbox_ids.add(outbox.id)
        state["last_outbox_created_at"] = normalize_utc(outbox.created_at)
        payload = _serialize_single_outbox_alert(outbox, alert, revision, quote, company)
        events.append({"type": "outbox_alert", **payload})

    return events


def _init_stream_cursor(
    session: Session,
    *,
    since_quote_id: int | None = None,
    include_existing_outbox: bool = False,
) -> dict[str, Any]:
    if since_quote_id is not None:
        max_quote_id = since_quote_id
    else:
        max_quote_id = session.scalar(select(func.max(PriceQuote.id))) or 0

    seen_outbox_ids: set[str] = set()
    last_outbox_created_at: datetime | None = None
    if not include_existing_outbox:
        existing_outbox = session.scalars(select(NotificationOutbox.id)).all()
        seen_outbox_ids.update(existing_outbox)
        latest_created = session.scalar(select(func.max(NotificationOutbox.created_at)))
        if latest_created is not None:
            last_outbox_created_at = normalize_utc(latest_created)

    return {
        "last_quote_id": max_quote_id,
        "seen_outbox_ids": seen_outbox_ids,
        "last_outbox_created_at": last_outbox_created_at,
    }


@router.get("/stream/live")
async def stream_live_events(
    request: Request,
    since_quote_id: int | None = Query(default=None, ge=0),
    include_existing_outbox: bool = Query(default=False),
    poll_interval_ms: int = Query(default=750, ge=100, le=10000),
    max_events: int | None = Query(default=None, ge=1, le=500),
) -> StreamingResponse:
    """Server-Sent Events (SSE) endpoint streaming real-time price ticks, Distance-from-Target updates,
    and new `notification_outbox` alert events to the React dashboard.
    """
    override_db = request.app.dependency_overrides.get(get_db)

    def _run_with_session(fn):
        if override_db is not None:
            gen = override_db()
            sess = next(gen)
            try:
                return fn(sess)
            finally:
                try:
                    next(gen)
                except StopIteration:
                    pass
        with SessionLocal() as sess:
            return fn(sess)

    cursor = _run_with_session(
        lambda s: _init_stream_cursor(
            s,
            since_quote_id=since_quote_id,
            include_existing_outbox=include_existing_outbox,
        ),
    )

    async def event_generator() -> AsyncIterator[str]:
        emitted = 0
        connected_event = {
            "type": "connected",
            "cursor_quote_id": cursor["last_quote_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        yield f"event: connected\ndata: {json.dumps(connected_event, default=str)}\n\n"
        emitted += 1
        if max_events is not None and emitted >= max_events:
            return

        while True:
            if await request.is_disconnected():
                break
            delta_events = _run_with_session(
                lambda s: _poll_stream_delta(s, cursor),
            )
            for ev in delta_events:
                ev_type = ev.get("type", "message")
                yield f"event: {ev_type}\ndata: {json.dumps(ev, default=str)}\n\n"
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return

            if not delta_events:
                heartbeat_ev = {
                    "type": "heartbeat",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat_ev)}\n\n"
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return

            await asyncio.sleep(poll_interval_ms / 1000.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@public_router.websocket("/ws/live")
async def websocket_live_events(
    websocket: WebSocket,
    since_quote_id: int | None = Query(default=None),
    include_existing_outbox: bool = Query(default=False),
    poll_interval_ms: int = Query(default=750),
) -> None:
    """WebSocket endpoint streaming real-time price ticks, Distance-from-Target metrics,
    and `notification_outbox` alert events to the React dashboard.
    """
    from .models import User, UserSession
    from .security import SESSION_COOKIE, _digest, _now

    override_db = websocket.app.dependency_overrides.get(get_db)
    override_auth = websocket.app.dependency_overrides.get(get_current_user)

    def _run_with_session(fn):
        if override_db is not None:
            gen = override_db()
            sess = next(gen)
            try:
                return fn(sess)
            finally:
                try:
                    next(gen)
                except StopIteration:
                    pass
        with SessionLocal() as sess:
            return fn(sess)

    await websocket.accept()
    if override_auth is None:
        raw_token = websocket.cookies.get(SESSION_COOKIE)
        if not raw_token:
            await websocket.close(code=4401)
            return

        def _verify_ws_session(sess: Session) -> bool:
            s_row = sess.scalar(
                select(UserSession).where(UserSession.token_hash == _digest(raw_token))
            )
            if s_row is None or s_row.expires_at.replace(tzinfo=timezone.utc) <= _now():
                return False
            u = sess.get(User, s_row.user_id)
            return bool(u is not None and u.is_active)

        if not _run_with_session(_verify_ws_session):
            await websocket.close(code=4401)
            return

    cursor = _run_with_session(
        lambda s: _init_stream_cursor(
            s,
            since_quote_id=since_quote_id,
            include_existing_outbox=include_existing_outbox,
        ),
    )
    await websocket.send_json(
        {
            "type": "connected",
            "cursor_quote_id": cursor["last_quote_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    try:
        while True:
            delta_events = _run_with_session(
                lambda s: _poll_stream_delta(s, cursor),
            )
            for ev in delta_events:
                await websocket.send_json(json.loads(json.dumps(ev, default=str)))
            await asyncio.sleep(max(0.1, poll_interval_ms / 1000.0))
    except WebSocketDisconnect:
        return

