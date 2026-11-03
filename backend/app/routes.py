from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import PurePath
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import Session, selectinload

from .database import get_db
from .imports import create_preview, get_batch, inspect_upload, read_upload, serialize_batch
from .models import (
    Company,
    ImportBatch,
    NotificationOutbox,
    PriceQuote,
    TargetAlert,
    TargetRevision,
    WorkerHeartbeat,
)
from .prices import normalize_utc, serialize_quote
from .schemas import CompanyRead, ImportConfirm, PriceHistoryRead, TargetUpdate

router = APIRouter(prefix="/api")
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/companies", response_model=list[CompanyRead])
def list_companies(session: DbSession) -> list[CompanyRead]:
    companies = list(
        session.scalars(select(Company).order_by(Company.exchange, Company.symbol))
    )
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
    results = []
    for company in companies:
        company_data = CompanyRead.model_validate(company).model_dump()
        company_data["latest_quote"] = quote_by_company.get(
            (company.exchange, company.symbol)
        )
        results.append(CompanyRead.model_validate(company_data))
    return results


@router.get("/price-feed/status")
def price_feed_status() -> dict[str, object]:
    return {
        "provider": None,
        "exchange_calendar": None,
        "live_mode": "not_configured",
        "alert_rules_enabled": False,
        "reason": "Select and configure a live provider and exchange calendar before enabling alerts.",
        "trigger": "price >= target",
        "already_above": "alert on first eligible quote",
    }


@router.get("/worker/status")
def worker_status(session: DbSession) -> dict[str, object]:
    heartbeat = session.get(WorkerHeartbeat, "price")
    now = datetime.now(timezone.utc)
    last_poll = normalize_utc(heartbeat.last_poll_at) if heartbeat and heartbeat.last_poll_at else None
    heartbeat_fresh = last_poll is not None and now - last_poll <= timedelta(minutes=3)
    return {
        "state": "error" if heartbeat and heartbeat.last_error else "running" if heartbeat_fresh else "stopped",
        "provider": heartbeat.provider if heartbeat else None,
        "exchange_calendar": heartbeat.exchange_calendar if heartbeat else None,
        "last_started_at": normalize_utc(heartbeat.started_at) if heartbeat else None,
        "last_poll_at": last_poll,
        "last_error": heartbeat.last_error if heartbeat else None,
        "live_provider_configured": False,
        "authoritative_calendar_configured": False,
        "live_alerts_enabled": False,
        "notification_mode": "dry_run_only",
        "note": "Fixture worker data is simulated. Laptop sleep or offline time stops polling.",
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
    events: list[dict[str, object]] = []
    for alert, revision, quote, company, notification in rows:
        target = (
            revision.base_target
            if alert.target_kind == "base"
            else revision.bull_target
        )
        quote_data = serialize_quote(quote)
        events.append(
            {
                "id": alert.id,
                "exchange": company.exchange,
                "symbol": company.symbol,
                "company": company.name,
                "target_kind": alert.target_kind,
                "target": float(target) if target is not None else None,
                "price": float(alert.price),
                "triggered_at": normalize_utc(alert.triggered_at),
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
                    else notification.status if notification else "Not queued"
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
            }
        )
    return events


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
) -> Company:
    identity = (exchange.upper(), symbol.upper())
    company = session.get(Company, identity)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found.")
    if company.version != update.expected_version:
        raise HTTPException(
            status_code=409,
            detail=f"Targets changed since they were loaded (current version {company.version}). Refresh and try again.",
        )
    fields = update.model_fields_set - {"expected_version"}
    if not fields:
        raise HTTPException(status_code=422, detail="Provide at least one target to update.")
    for field in fields:
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
        setattr(company, field, getattr(update, field))
    new_targets = (company.base_target, company.bull_target)
    if old_targets != new_targets:
        company.version += 1
        session.add(
            TargetRevision(
                exchange=company.exchange,
                symbol=company.symbol,
                version=company.version,
                base_target=company.base_target,
                bull_target=company.bull_target,
                source="manual",
                changed_at=datetime.now(timezone.utc),
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
    return company


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


@router.get("/imports/{batch_id}")
def read_import(batch_id: str, session: DbSession) -> dict:
    batch = get_batch(session, batch_id)
    companies = {
        (company.exchange, company.symbol): company
        for company in session.scalars(select(Company))
    }
    return serialize_batch(batch, companies)


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
    invalid_rows = [row.row_number for row in batch.rows if row.errors]
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

    identities = [(row.exchange, row.symbol) for row in batch.rows]
    companies = {
        identity: company
        for identity in identities
        if identity[0] is not None and identity[1] is not None
        if (company := session.get(Company, identity)) is not None
    }
    existing_identities = set(companies)
    for row in batch.rows:
        identity = (row.exchange or "", row.symbol or "")
        existing = companies.get(identity)
        if existing and row.expected_version != existing.version:
            raise HTTPException(
                status_code=409,
                detail=f"{identity[0]}:{identity[1]} changed after preview. Create a fresh preview before confirming.",
            )

    created = 0
    updated = 0
    unchanged = 0
    now = datetime.now(timezone.utc)
    try:
        for row in batch.rows:
            identity = (row.exchange or "", row.symbol or "")
            company = companies.get(identity)
            if company is None:
                company = Company(
                    exchange=identity[0],
                    symbol=identity[1],
                    name=row.name or "",
                )
                session.add(company)
                session.flush()
                companies[identity] = company
                created += 1
            if row.update_sector:
                company.sector = row.sector
            if row.update_cmp:
                company.cmp = row.cmp

            old_targets = (company.base_target, company.bull_target)
            if row.update_base_target and row.base_target is not None:
                company.base_target = row.base_target
            if row.update_bull_target and row.bull_target is not None:
                company.bull_target = row.bull_target
            if old_targets != (company.base_target, company.bull_target):
                company.version += 1
                if identity in existing_identities:
                    updated += 1
                session.add(
                    TargetRevision(
                        exchange=company.exchange,
                        symbol=company.symbol,
                        version=company.version,
                        base_target=company.base_target,
                        bull_target=company.bull_target,
                        source="import",
                        import_id=batch.id,
                        changed_at=now,
                    )
                )
            elif identity in existing_identities:
                unchanged += 1
        session.flush()
        batch.status = "confirmed"
        batch.result = {
            "id": batch.id,
            "status": "confirmed",
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
        }
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
