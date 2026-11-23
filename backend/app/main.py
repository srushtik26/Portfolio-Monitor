from datetime import datetime, timezone
import os
from pathlib import Path
import time

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

import sys

from .database import ensure_sqlite_schema, get_db
from .auth_routes import router as auth_router
from .bootstrap_admin import bootstrap_admin_from_env
from .models import WorkerHeartbeat
from .notifications import check_live_notification_gate, is_smtp_complete
from .routes import public_router, router

if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
    ensure_sqlite_schema()
    bootstrap_admin_from_env()


def _parse_cors_origins() -> list[str]:
    default_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    raw = os.environ.get("CORS_ORIGINS", "").strip()
    if not raw:
        return default_origins
    configured = [origin.strip() for origin in raw.split(",") if origin.strip()]
    merged = list(dict.fromkeys([*configured, *default_origins]))
    return merged


app = FastAPI(
    title="Northstar Portfolio Monitor API",
    version="2.0.0",
    description="Production price-monitoring, target-comparison, and multi-channel alert service.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "Authorization", "X-CSRF-Token"],
)


@app.middleware("http")
async def add_security_and_timing_headers(request: Request, call_next) -> Response:
    started = time.perf_counter()
    response: Response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health/ready")
@app.get("/healthz")
def health_readiness_check(session: Session = Depends(get_db)) -> dict[str, object]:
    from .models import Company, NotificationOutbox
    from sqlalchemy import func, select

    db_ok = False
    db_dialect = "unknown"
    monitored_count = 0
    failed_emails = 0
    heartbeat: WorkerHeartbeat | None = None
    try:
        session.execute(text("SELECT 1"))
        db_ok = True
        db_dialect = session.bind.dialect.name if session.bind is not None else "unknown"
        monitored_count = int(
            session.scalar(
                select(func.count()).select_from(Company).where(Company.monitoring_active.is_(True))
            )
            or 0
        )
        failed_emails = int(
            session.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.status == "failed")
            )
            or 0
        )
        heartbeat = session.get(WorkerHeartbeat, "price")
    except Exception:
        db_ok = False
        heartbeat = None
    smtp_ok, _ = is_smtp_complete()
    live_ready, gate_reason = check_live_notification_gate()
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "2.0.0",
        "database": "connected" if db_ok else "unreachable",
        "database_engine": db_dialect,
        "worker_state": heartbeat.connection_status if heartbeat else "stopped",
        "provider": heartbeat.provider if heartbeat else None,
        "monitored_symbols_count": monitored_count,
        "failed_notifications_count": failed_emails,
        "smtp_configured": smtp_ok,
        "live_notifications_ready": live_ready,
        "notification_gate_reason": gate_reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/health/worker")
def health_worker_check(session: Session = Depends(get_db)) -> dict[str, object]:
    """Public secret-free worker & market-feed health summary distinguishing healthy/degraded/disconnected/stale/unavailable.

    Public health endpoints never expose Angel-backed symbol quotes, fallback details, or credentials.
    """
    from datetime import timedelta
    from .models import NotificationOutbox
    from .prices import normalize_utc
    from .provider_coordinator import (
        YFINANCE_DELAYED_LABEL,
        configured_backup_provider,
        configured_primary_provider,
        get_shared_coordinator,
    )
    from sqlalchemy import func, select

    now = datetime.now(timezone.utc)
    coord_health = get_shared_coordinator().get_health_snapshot(include_angel_details=False)
    try:
        heartbeat = session.get(WorkerHeartbeat, "price")
        failed_emails = int(
            session.scalar(
                select(func.count())
                .select_from(NotificationOutbox)
                .where(NotificationOutbox.status == "failed")
            )
            or 0
        )
    except Exception:
        return {
            "status": "unavailable",
            "worker_running": False,
            "provider": None,
            "primary_provider": configured_primary_provider(),
            "primary_label": YFINANCE_DELAYED_LABEL,
            "data_delay_label": "delayed_candle_data",
            "backup_provider": configured_backup_provider(),
            "backup_status": "restricted",
            "exchange_calendar": None,
            "connection_status": "disconnected",
            "last_poll_at": None,
            "last_valid_tick_at": None,
            "subscribed_symbols": 0,
            "failed_requests_count": 0,
            "stale_symbols_count": 0,
            "queued_requests_total": int(coord_health.get("queued_requests_total", 0)),
            "failed_notifications_count": 0,
            "timestamp": now.isoformat(),
        }

    if heartbeat is None:
        return {
            "status": "unavailable",
            "worker_running": False,
            "provider": None,
            "primary_provider": configured_primary_provider(),
            "primary_label": YFINANCE_DELAYED_LABEL,
            "data_delay_label": "delayed_candle_data",
            "backup_provider": configured_backup_provider(),
            "backup_status": "restricted",
            "exchange_calendar": None,
            "connection_status": "disconnected",
            "last_poll_at": None,
            "last_valid_tick_at": None,
            "subscribed_symbols": 0,
            "failed_requests_count": 0,
            "stale_symbols_count": 0,
            "queued_requests_total": int(coord_health.get("queued_requests_total", 0)),
            "failed_notifications_count": failed_emails,
            "timestamp": now.isoformat(),
        }

    last_poll = normalize_utc(heartbeat.last_poll_at) if heartbeat.last_poll_at else None
    last_quote = (
        normalize_utc(heartbeat.last_successful_quote_at)
        if heartbeat.last_successful_quote_at
        else None
    )
    heartbeat_fresh = last_poll is not None and (now - last_poll) <= timedelta(minutes=3)

    if not heartbeat_fresh:
        status = "disconnected"
    elif heartbeat.last_error:
        status = "degraded"
    elif (heartbeat.stale_symbols_count or 0) > 0 and (heartbeat.monitored_symbols_count or 0) > 0:
        status = "stale"
    elif failed_emails > 0:
        status = "degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "worker_running": heartbeat_fresh,
        "provider": heartbeat.provider,
        "primary_provider": configured_primary_provider(),
        "primary_label": YFINANCE_DELAYED_LABEL,
        "data_delay_label": "delayed_candle_data",
        "backup_provider": configured_backup_provider(),
        "backup_status": "restricted",
        "exchange_calendar": heartbeat.exchange_calendar,
        "connection_status": heartbeat.connection_status if heartbeat_fresh else "disconnected",
        "last_poll_at": last_poll.isoformat() if last_poll else None,
        "last_valid_tick_at": last_quote.isoformat() if last_quote else None,
        "subscribed_symbols": int(heartbeat.monitored_symbols_count or 0),
        "failed_requests_count": int(heartbeat.failed_requests_count or 0),
        "stale_symbols_count": int(heartbeat.stale_symbols_count or 0),
        "queued_requests_total": int(coord_health.get("queued_requests_total", 0)),
        "failed_notifications_count": failed_emails,
        "timestamp": now.isoformat(),
    }


app.include_router(auth_router)
app.include_router(public_router)
app.include_router(router)

# Serve production frontend bundle from ../dist when built locally
_DIST_DIR = Path(__file__).resolve().parents[2] / "dist"
if (_DIST_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(_DIST_DIR / "assets")), name="static-assets")


@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa_index(full_path: str) -> Response:
    if full_path.startswith("api/") or full_path == "healthz":
        return Response(status_code=404)
    index_file = _DIST_DIR / "index.html"
    if index_file.is_file():
        candidate = (_DIST_DIR / full_path).resolve()
        if full_path and candidate.is_file() and _DIST_DIR in candidate.parents:
            return FileResponse(str(candidate))
        return FileResponse(str(index_file))
    return Response(
        content='{"service":"Northstar Portfolio Monitor API","status":"running"}',
        media_type="application/json",
    )

