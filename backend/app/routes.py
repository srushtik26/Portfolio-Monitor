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
from sqlalchemy import func, select
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
    NotificationOutbox,
    NotificationPreference,
    PriceQuote,
    TargetAlert,
    TargetAlertState,
    TargetRevision,
    WorkerHeartbeat,
)
from .notifications import MultiChannelNotifier, drain_outbox, get_or_create_preferences
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

router = APIRouter(prefix="/api")
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
                order_by=(PriceQuote.quoted_at.desc(), PriceQuote.id.desc()),
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


@router.get("/health")
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
        .order_by(PriceQuote.quoted_at.desc(), PriceQuote.id.desc())
        .limit(100)
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

    return {
        "company": company_read.model_dump(),
        "quote_history": [serialize_quote(q) for q in quotes],
        "alert_history": alerts_serialized,
        "target_revisions": revisions_serialized,
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
    heartbeat = session.get(WorkerHeartbeat, "price")
    now = datetime.now(timezone.utc)
    last_poll = normalize_utc(heartbeat.last_poll_at) if heartbeat and heartbeat.last_poll_at else None
    heartbeat_fresh = last_poll is not None and now - last_poll <= timedelta(minutes=3)
    live_provider = bool(heartbeat and heartbeat.provider and heartbeat.provider != "fixture")
    auth_calendar = bool(heartbeat and heartbeat.exchange_calendar and heartbeat.exchange_calendar != "fixture")
    pref = session.get(NotificationPreference, "default")
    notif_mode = (
        "live_channels"
        if (pref and (pref.dashboard_enabled or (pref.email_enabled and pref.email_recipients)))
        else "dry_run_only"
    )

    quote_by_company, _ = _load_quotes_and_states(session)
    companies = list(session.scalars(select(Company).where(Company.is_approved.is_(True), Company.monitoring_active.is_(True))))
    stale_symbols = []
    for c in companies:
        q = quote_by_company.get((c.exchange, c.symbol))
        if q is not None and not q.get("is_fresh") and not q.get("is_simulated"):
            stale_symbols.append(f"{c.exchange}:{c.symbol}")

    calendar = NseBseExchangeCalendar()
    market_open = calendar.is_open("NSE", now)

    return {
        "state": "error" if heartbeat and heartbeat.last_error else "running" if heartbeat_fresh else "stopped",
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
        "connection_status": heartbeat.connection_status if heartbeat else "disconnected",
        "websocket_status": heartbeat.websocket_status if heartbeat else "unavailable",
        "failed_requests_count": heartbeat.failed_requests_count if heartbeat else 0,
        "reconnect_count": heartbeat.reconnect_count if heartbeat else 0,
        "monitored_symbols_count": len(companies),
        "stale_symbols_count": len(stale_symbols),
        "stale_symbols": stale_symbols,
        "market_open": market_open,
        "live_provider_configured": live_provider,
        "authoritative_calendar_configured": auth_calendar,
        "live_alerts_enabled": live_provider and auth_calendar,
        "notification_mode": notif_mode,
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
    """Fetch live quotes on demand for one or all approved active companies and evaluate target rules."""
    use_shoonya = (
        os.environ.get("PRICE_PROVIDER", "").strip().lower() in {"shoonya", "shoonya_ws"}
        or bool(os.environ.get("SHOONYA_USER_ID"))
    )
    if use_shoonya:
        from .shoonya_feed import ShoonyaPriceProvider

        provider = ShoonyaPriceProvider()
    else:
        provider = YFinanceProvider()
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
            quote = provider.fetch_quote(company.exchange, company.symbol)
            record_quote(
                session,
                quote,
                now=datetime.now(timezone.utc),
                alerts_enabled=True,
                fixture_alerts_enabled=False,
            )
            recorded += 1
        except (ProviderError, ValueError) as exc:
            failed += 1
            errors.append(f"{company.exchange}:{company.symbol}: {exc}")

    delivery = drain_outbox(MultiChannelNotifier(), session_factory=lambda: session, now=now)
    heartbeat = session.get(WorkerHeartbeat, "price")
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            name="price",
            provider="yfinance",
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
        heartbeat.provider = "yfinance"
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
        "errors": errors,
        "notifications_sent": delivery.sent,
        "notifications_retried": delivery.retried,
        "notifications_failed": delivery.failed,
    }


@router.get("/alerts")
def list_alerts(session: DbSession) -> list[dict[str, object]]:
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
            grouped[alert.id] = {
                "id": alert.id,
                "exchange": company.exchange,
                "symbol": company.symbol,
                "company": company.name,
                "target_kind": alert.target_kind,
                "target": float(target) if target is not None else None,
                "price": float(alert.price),
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
    return list(grouped.values())


@router.post("/alerts/{alert_id}/action")
def alert_action(
    alert_id: int,
    payload: AlertActionRequest,
    session: DbSession,
) -> dict[str, Any]:
    try:
        updated = apply_alert_lifecycle_action(
            session,
            alert_id,
            payload.action,
            actor=payload.actor,
        )
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


@router.get("/notifications/preferences")
def read_notification_preferences(session: DbSession) -> dict[str, Any]:
    pref = get_or_create_preferences(session)
    session.commit()
    return {
        "id": pref.id,
        "dashboard_enabled": pref.dashboard_enabled,
        "email_enabled": pref.email_enabled,
        "email_recipients": pref.email_recipients or [],
        "notify_on_base": pref.notify_on_base,
        "notify_on_bull": pref.notify_on_bull,
        "notify_on_stale": pref.notify_on_stale,
        "smtp_configured": bool(os.environ.get("SMTP_HOST") or os.environ.get("SMTP_SPOOL_DIR")),
        "updated_at": normalize_utc(pref.updated_at),
    }


@router.put("/notifications/preferences")
def update_notification_preferences(
    payload: NotificationPreferenceUpdate,
    session: DbSession,
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
            actor=payload.actor,
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
    return {
        "id": pref.id,
        "dashboard_enabled": pref.dashboard_enabled,
        "email_enabled": pref.email_enabled,
        "email_recipients": pref.email_recipients or [],
        "notify_on_base": pref.notify_on_base,
        "notify_on_bull": pref.notify_on_bull,
        "notify_on_stale": pref.notify_on_stale,
        "smtp_configured": bool(os.environ.get("SMTP_HOST") or os.environ.get("SMTP_SPOOL_DIR")),
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
        .order_by(PriceQuote.quoted_at.desc(), PriceQuote.id.desc())
        .limit(limit)
    )
    return {
        "exchange": identity[0],
        "symbol": identity[1],
        "items": [serialize_quote(quote) for quote in quotes],
    }


@router.put(
    "/companies/{exchange}/{symbol}/targets", response_model=CompanyRead
)
def update_company_targets(
    exchange: str, symbol: str, update: TargetUpdate, session: DbSession
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
                changed_by=update.actor,
                changed_at=now,
            )
        )
    session.add(
        AuditLog(
            action="company_targets_updated",
            actor=update.actor,
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
    "/companies/{exchange}/{symbol}/target-config", response_model=CompanyRead
)
def update_company_target_config(
    exchange: str, symbol: str, config: TargetConfigUpdate, session: DbSession
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
            actor=config.actor,
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
    return create_preview(
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
    )


@router.get("/imports")
def list_imports(session: DbSession) -> list[dict[str, Any]]:
    batches = list(
        session.scalars(
            select(ImportBatch)
            .options(selectinload(ImportBatch.rows))
            .order_by(ImportBatch.created_at.desc())
            .limit(50)
        )
    )
    return [
        {
            "id": b.id,
            "filename": b.filename,
            "status": b.status,
            "version_number": b.version_number,
            "uploaded_by": b.uploaded_by,
            "approved_by": b.approved_by,
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
def read_import(batch_id: str, session: DbSession) -> dict:
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
) -> dict[str, Any]:
    return update_preview_row(session, batch_id, row_number, payload)


@router.post("/imports/{batch_id}/reject")
def reject_import(batch_id: str, session: DbSession) -> dict[str, Any]:
    batch = get_batch(session, batch_id)
    if batch.status == "confirmed":
        raise HTTPException(status_code=409, detail="Confirmed imports cannot be rejected.")
    batch.status = "rejected"
    session.add(
        AuditLog(
            action="import_rejected",
            actor="Srushti Kadam",
            entity_type="import_batch",
            entity_id=batch.id,
            details={"filename": batch.filename},
            created_at=datetime.now(timezone.utc),
        )
    )
    session.commit()
    return {"id": batch.id, "status": "rejected"}


@router.post("/imports/{batch_id}/restore")
def restore_import(batch_id: str, session: DbSession) -> dict[str, Any]:
    return restore_import_version(session, batch_id)


@router.post("/imports/{batch_id}/confirm")
def confirm_import(
    batch_id: str, confirmation: ImportConfirm, session: DbSession
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
                "approved_by": confirmation.approved_by,
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
                        changed_by=confirmation.approved_by,
                        cell_refs=row.cell_refs,
                        changed_at=now,
                    )
                )
            elif identity in existing_identities:
                unchanged += 1
        session.flush()
        batch.status = "confirmed"
        batch.version_number = next_version_number
        batch.uploaded_by = confirmation.uploaded_by
        batch.approved_by = confirmation.approved_by
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
                actor=confirmation.approved_by,
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


@router.get("/audit-logs")
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
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": normalize_utc(log.created_at),
        }
        for log in logs
    ]


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
            "target_kind": alert.target_kind,
            "target": float(target) if target is not None else None,
            "price": float(alert.price),
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
                        "quoted_at": normalize_utc(quote.quoted_at).isoformat(),
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

    cursor = await asyncio.to_thread(
        _run_with_session,
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
            delta_events = await asyncio.to_thread(
                _run_with_session,
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


@router.websocket("/ws/live")
async def websocket_live_events(
    websocket: WebSocket,
    since_quote_id: int | None = Query(default=None),
    include_existing_outbox: bool = Query(default=False),
    poll_interval_ms: int = Query(default=750),
) -> None:
    """WebSocket endpoint streaming real-time price ticks, Distance-from-Target metrics,
    and `notification_outbox` alert events to the React dashboard.
    """
    await websocket.accept()
    override_db = websocket.app.dependency_overrides.get(get_db)

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

    cursor = await asyncio.to_thread(
        _run_with_session,
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
            delta_events = await asyncio.to_thread(
                _run_with_session,
                lambda s: _poll_stream_delta(s, cursor),
            )
            for ev in delta_events:
                await websocket.send_json(json.loads(json.dumps(ev, default=str)))
            await asyncio.sleep(max(0.1, poll_interval_ms / 1000.0))
    except WebSocketDisconnect:
        return

