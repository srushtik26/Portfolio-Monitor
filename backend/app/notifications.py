from email.message import EmailMessage
import logging
import os
from pathlib import Path
import smtplib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (
    Company,
    NotificationOutbox,
    NotificationPreference,
    TargetAlert,
    TargetRevision,
)

logger = logging.getLogger("portfolio.notifications")
INDIA_TZ = ZoneInfo("Asia/Kolkata")
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 15
MAX_BACKOFF_SECONDS = 15 * 60
CLAIM_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class NotificationMessage:
    outbox_id: str
    target_alert_id: int
    exchange: str
    symbol: str
    target_kind: str
    target_price: str
    quote_price: str
    triggered_at: datetime
    is_simulated: bool
    company_name: str = ""
    comparison_direction: str = "above"
    channel: str = "dry-run"
    destination: str | None = None


@dataclass(frozen=True)
class SendReceipt:
    provider_message_id: str | None


class NotificationNotifier(Protocol):
    supports_idempotency: bool

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt: ...


def format_alert_email(message: NotificationMessage) -> tuple[str, str]:
    company_label = message.company_name or message.symbol
    target_type = "Base" if message.target_kind == "base" else "Bull"
    direction_label = "Downward" if message.comparison_direction == "below" else "Upward"
    ts_utc = (
        message.triggered_at.replace(tzinfo=timezone.utc)
        if message.triggered_at.tzinfo is None
        else message.triggered_at.astimezone(timezone.utc)
    )
    ts_ist = ts_utc.astimezone(INDIA_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    try:
        target_fmt = f"₹{float(message.target_price):,.2f}"
    except (ValueError, TypeError):
        target_fmt = f"₹{message.target_price}"
    try:
        quote_fmt = f"₹{float(message.quote_price):,.2f}"
    except (ValueError, TypeError):
        quote_fmt = f"₹{message.quote_price}"

    subject = f"Price Target Alert – {company_label}"
    body = (
        f"{company_label} has reached its configured {target_type} Target Price.\n\n"
        f"Company: {company_label}\n"
        f"Symbol: {message.symbol}\n"
        f"Exchange: {message.exchange}\n"
        f"Target Type: {target_type}\n"
        f"Target Price: {target_fmt}\n"
        f"Current Price: {quote_fmt}\n"
        f"Trigger Time: {ts_ist}\n"
        f"Price Direction: {direction_label}\n"
    )
    return subject, body


class DryRunNotifier:
    """No-network sink: records notification processing without contacting a destination."""

    supports_idempotency = True

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt:
        logger.info(
            "DRY RUN only: event=%s %s:%s %s target=%s quote=%s simulated=%s idempotency=%s",
            message.target_alert_id,
            message.exchange,
            message.symbol,
            message.target_kind,
            message.target_price,
            message.quote_price,
            message.is_simulated,
            idempotency_key,
        )
        return SendReceipt(provider_message_id=f"dry-run:{idempotency_key}")


class DashboardNotifier:
    """Delivers persistent in-app dashboard alert notifications."""

    supports_idempotency = True

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt:
        logger.info(
            "DASHBOARD notification delivered: event=%s %s:%s (%s) target=%s quote=%s idempotency=%s",
            message.target_alert_id,
            message.exchange,
            message.symbol,
            message.target_kind,
            message.target_price,
            message.quote_price,
            idempotency_key,
        )
        return SendReceipt(provider_message_id=f"dashboard:{idempotency_key}")


class SmtpEmailNotifier:
    """Delivers formatted price target alert emails via SMTP or a configured mail spool."""

    supports_idempotency = True

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        use_tls: bool | None = None,
        spool_dir: str | Path | None = None,
    ) -> None:
        self.host = host if host is not None else os.environ.get("SMTP_HOST", "").strip()
        self.port = port if port is not None else int(os.environ.get("SMTP_PORT", "587"))
        self.username = username if username is not None else os.environ.get("SMTP_USER", "").strip()
        self.password = password if password is not None else os.environ.get("SMTP_PASSWORD", "")
        self.sender = (
            sender
            if sender is not None
            else os.environ.get("SMTP_FROM", "alerts@plutuswealth.local").strip()
        )
        self.use_tls = (
            use_tls
            if use_tls is not None
            else os.environ.get("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
        )
        spool_env = os.environ.get("SMTP_SPOOL_DIR", "").strip()
        self.spool_dir = Path(spool_dir) if spool_dir else (Path(spool_env) if spool_env else None)

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt:
        recipients = [
            addr.strip()
            for addr in (message.destination or "").split(",")
            if addr.strip() and "@" in addr.strip()
        ]
        if not recipients:
            raise PermanentSendError("No valid email recipient address is configured for this alert.")

        subject, body = format_alert_email(message)
        email_msg = EmailMessage()
        email_msg["Subject"] = subject
        email_msg["From"] = self.sender
        email_msg["To"] = ", ".join(recipients)
        email_msg["Message-ID"] = f"<{idempotency_key}@portfolio-monitor>"
        email_msg["X-Idempotency-Key"] = idempotency_key
        email_msg.set_content(body)

        if self.spool_dir is not None:
            try:
                self.spool_dir.mkdir(parents=True, exist_ok=True)
                spool_file = self.spool_dir / f"{idempotency_key}.eml"
                spool_file.write_bytes(email_msg.as_bytes())
                return SendReceipt(provider_message_id=f"smtp-spool:{idempotency_key}")
            except OSError as error:
                raise RetryableSendError(f"Failed writing email to spool directory: {error}") from error

        if not self.host:
            raise RetryableSendError(
                "SMTP_HOST is not configured. Configure SMTP_HOST/SMTP_PORT or SMTP_SPOOL_DIR to deliver email."
            )

        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(email_msg)
            return SendReceipt(provider_message_id=f"smtp:{idempotency_key}")
        except smtplib.SMTPRecipientsRefused as error:
            raise PermanentSendError(f"SMTP recipients refused: {error}") from error
        except smtplib.SMTPAuthenticationError as error:
            raise PermanentSendError(f"SMTP authentication failed: {error}") from error
        except (smtplib.SMTPException, OSError) as error:
            raise RetryableSendError(f"SMTP delivery error: {error}") from error


class MultiChannelNotifier:
    """Dispatches outbox items to Dashboard, Email (SMTP), or DryRun notifiers based on channel & mode."""

    supports_idempotency = True

    def __init__(
        self,
        *,
        dry_run_notifier: NotificationNotifier | None = None,
        dashboard_notifier: NotificationNotifier | None = None,
        email_notifier: NotificationNotifier | None = None,
    ) -> None:
        self.dry_run = dry_run_notifier or DryRunNotifier()
        self.dashboard = dashboard_notifier or DashboardNotifier()
        self.email = email_notifier or SmtpEmailNotifier()

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt:
        if message.is_simulated or message.channel == "dry-run":
            return self.dry_run.send(message, idempotency_key)
        if message.channel == "dashboard":
            return self.dashboard.send(message, idempotency_key)
        if message.channel == "email":
            return self.email.send(message, idempotency_key)
        raise PermanentSendError(f"Unsupported notification channel: {message.channel}")


class RetryableSendError(Exception):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentSendError(Exception):
    pass


@dataclass(frozen=True)
class DrainResult:
    sent: int
    retried: int
    failed: int


def retry_delay(attempt: int, retry_after_seconds: float | None = None) -> timedelta:
    exponent = max(0, attempt - 1)
    seconds = min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)
    if retry_after_seconds is not None:
        seconds = max(seconds, min(max(0.0, retry_after_seconds), MAX_BACKOFF_SECONDS))
    return timedelta(seconds=seconds)


def _recover_interrupted(session: Session, now: datetime) -> None:
    expired = session.scalars(
        select(NotificationOutbox).where(
            NotificationOutbox.status == "sending",
            NotificationOutbox.claimed_at <= now - CLAIM_LEASE,
        )
    )
    for row in expired:
        row.status = "failed" if row.attempts >= row.max_attempts else "retry"
        row.next_attempt_at = now
        row.claimed_at = None
        row.claim_token = None
        row.last_error = "Previous send attempt was interrupted; delivery outcome may be unknown."


def _claim_one(session: Session, now: datetime) -> NotificationOutbox | None:
    _recover_interrupted(session, now)
    exhausted = session.scalars(
        select(NotificationOutbox).where(
            NotificationOutbox.status.in_(("queued", "retry")),
            NotificationOutbox.attempts >= NotificationOutbox.max_attempts,
        )
    )
    for row in exhausted:
        row.status = "failed"
        row.last_error = row.last_error or "Maximum notification attempts reached."
        row.claimed_at = None
        row.claim_token = None
    session.flush()
    candidate = session.scalar(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status.in_(("queued", "retry")),
            NotificationOutbox.attempts < NotificationOutbox.max_attempts,
            NotificationOutbox.next_attempt_at <= now,
        )
        .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
        .limit(1)
    )
    if candidate is None:
        session.commit()
        return None

    token = str(uuid.uuid4())
    claimed = session.execute(
        update(NotificationOutbox)
        .where(
            NotificationOutbox.id == candidate.id,
            NotificationOutbox.status == candidate.status,
            NotificationOutbox.attempts == candidate.attempts,
            NotificationOutbox.next_attempt_at <= now,
        )
        .values(
            status="sending",
            attempts=NotificationOutbox.attempts + 1,
            claimed_at=now,
            claim_token=token,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    if claimed.rowcount != 1:
        return None
    session.expire(candidate)
    return session.get(NotificationOutbox, candidate.id)


def _notification_message(
    session: Session, outbox: NotificationOutbox
) -> NotificationMessage:
    alert = session.get(TargetAlert, outbox.target_alert_id)
    if alert is None:
        raise ValueError(f"Outbox row {outbox.id} references a missing target alert.")
    revision = session.get(TargetRevision, alert.target_revision_id)
    if revision is None:
        raise ValueError(f"Target alert {alert.id} references a missing revision.")
    target_value = (
        alert.target_price_snapshot
        if alert.target_price_snapshot is not None
        else (revision.base_target if alert.target_kind == "base" else revision.bull_target)
    )
    if target_value is None:
        raise ValueError(f"Target alert {alert.id} has no corresponding target value.")
    company = session.get(Company, (revision.exchange, revision.symbol))
    return NotificationMessage(
        outbox_id=outbox.id,
        target_alert_id=alert.id,
        exchange=revision.exchange,
        symbol=revision.symbol,
        target_kind=alert.target_kind,
        target_price=str(target_value),
        quote_price=str(alert.price),
        triggered_at=alert.triggered_at,
        is_simulated=alert.is_simulated,
        company_name=company.name if company else revision.symbol,
        comparison_direction=alert.comparison_direction or "above",
        channel=outbox.channel,
        destination=outbox.destination,
    )


def _complete(
    session_factory: Callable[[], Session],
    outbox_id: str,
    claim_token: str,
    *,
    now: datetime,
    receipt: SendReceipt | None = None,
    error: Exception | None = None,
) -> str:
    with session_factory() as session:
        row = session.get(NotificationOutbox, outbox_id)
        if row is None or row.status != "sending" or row.claim_token != claim_token:
            logger.warning("Notification claim %s was lost before completion.", outbox_id)
            return "lost"
        row.claimed_at = None
        row.claim_token = None
        if error is None:
            if receipt is None:
                raise ValueError("A successful send requires a receipt.")
            row.status = "sent"
            row.sent_at = now
            row.provider_message_id = receipt.provider_message_id
            row.last_error = None
            outcome = "sent"
        else:
            row.last_error = str(error)[:1000]
            row.sent_at = None
            if isinstance(error, PermanentSendError) or row.attempts >= row.max_attempts:
                row.status = "failed"
                outcome = "failed"
            else:
                row.status = "retry"
                row.next_attempt_at = now + retry_delay(
                    row.attempts,
                    error.retry_after_seconds
                    if isinstance(error, RetryableSendError)
                    else None,
                )
                outcome = "retry"
        session.commit()
        return outcome


def drain_outbox(
    notifier: NotificationNotifier | None = None,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    now: datetime | None = None,
    limit: int = 100,
) -> DrainResult:
    if limit < 1:
        raise ValueError("Outbox drain limit must be at least one.")
    active_notifier = notifier or MultiChannelNotifier()
    current_time = now or datetime.now(timezone.utc)
    sent = retried = failed = 0
    for _ in range(limit):
        with session_factory() as session:
            outbox = _claim_one(session, current_time)
            if outbox is None:
                break
            if outbox.delivery_mode != "dry_run":
                if not active_notifier.supports_idempotency:
                    outcome = _complete(
                        session_factory,
                        outbox.id,
                        outbox.claim_token or "",
                        now=current_time,
                        error=PermanentSendError(
                            "Live notifications require a notifier with provider-supported idempotency."
                        ),
                    )
                    failed += outcome == "failed"
                    continue
                if isinstance(active_notifier, DryRunNotifier):
                    outcome = _complete(
                        session_factory,
                        outbox.id,
                        outbox.claim_token or "",
                        now=current_time,
                        error=PermanentSendError(
                            "No live channel or destination is configured; no destination was contacted."
                        ),
                    )
                    failed += outcome == "failed"
                    continue
            token = outbox.claim_token
            if token is None:
                raise RuntimeError(f"Claimed outbox row {outbox.id} has no claim token.")
            message = _notification_message(session, outbox)
            idempotency_key = outbox.idempotency_key
            outbox_id = outbox.id
        try:
            receipt = active_notifier.send(message, idempotency_key)
            outcome = _complete(
                session_factory,
                outbox_id,
                token,
                now=current_time,
                receipt=receipt,
            )
        except (RetryableSendError, PermanentSendError) as error:
            logger.warning("Notification %s did not send: %s", outbox_id, error)
            outcome = _complete(
                session_factory,
                outbox_id,
                token,
                now=current_time,
                error=error,
            )
        except Exception as error:
            logger.exception("Unexpected notifier failure for outbox %s.", outbox_id)
            outcome = _complete(
                session_factory,
                outbox_id,
                token,
                now=current_time,
                error=RetryableSendError(f"Unexpected notifier error: {error}"),
            )
        if outcome == "sent":
            sent += 1
        elif outcome == "retry":
            retried += 1
        elif outcome == "failed":
            failed += 1
    return DrainResult(sent=sent, retried=retried, failed=failed)


def get_or_create_preferences(session: Session) -> NotificationPreference:
    pref = session.get(NotificationPreference, "default")
    if pref is None:
        env_recipients = [
            addr.strip()
            for addr in os.environ.get("ALERT_EMAIL_RECIPIENTS", "").split(",")
            if addr.strip() and "@" in addr.strip()
        ]
        pref = NotificationPreference(
            id="default",
            dashboard_enabled=True,
            email_enabled=bool(env_recipients),
            email_recipients=env_recipients,
            notify_on_base=True,
            notify_on_bull=True,
            notify_on_stale=True,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pref)
        session.flush()
    return pref


def create_dry_run_outbox(
    session: Session,
    target_alert: TargetAlert,
    *,
    now: datetime | None = None,
) -> NotificationOutbox:
    outbox_id = str(uuid.uuid4())
    created_at = now or datetime.now(timezone.utc)
    row = NotificationOutbox(
        id=outbox_id,
        target_alert_id=target_alert.id,
        channel="dry-run",
        destination=None,
        delivery_mode="dry_run",
        status="queued",
        attempts=0,
        max_attempts=MAX_ATTEMPTS,
        created_at=created_at,
        next_attempt_at=created_at,
        idempotency_key=f"portfolio-alert-{outbox_id}",
    )
    session.add(row)
    return row


def queue_alert_notifications(
    session: Session,
    target_alert: TargetAlert,
    *,
    now: datetime | None = None,
) -> list[NotificationOutbox]:
    """Queue notifications for a triggered alert.

    Simulated alerts always use the dry-run channel.
    Live alerts check NotificationPreference (or NOTIFICATION_MODE=live):
    if live channels are configured in NotificationPreference or environment, queues
    dashboard and/or email notifications; otherwise defaults to dry-run for backward compatibility
    when no live notification preference has been configured.
    """
    created_at = now or datetime.now(timezone.utc)
    if target_alert.is_simulated:
        return [create_dry_run_outbox(session, target_alert, now=created_at)]

    pref = session.get(NotificationPreference, "default")
    live_mode_env = os.environ.get("NOTIFICATION_MODE", "").strip().lower() == "live"
    if pref is None and not live_mode_env:
        return [create_dry_run_outbox(session, target_alert, now=created_at)]

    if pref is None:
        pref = get_or_create_preferences(session)

    if target_alert.target_kind == "base" and not pref.notify_on_base:
        return []
    if target_alert.target_kind == "bull" and not pref.notify_on_bull:
        return []

    queued: list[NotificationOutbox] = []
    if pref.dashboard_enabled:
        dash_id = str(uuid.uuid4())
        dash_row = NotificationOutbox(
            id=dash_id,
            target_alert_id=target_alert.id,
            channel="dashboard",
            destination="dashboard",
            delivery_mode="live",
            status="queued",
            attempts=0,
            max_attempts=MAX_ATTEMPTS,
            created_at=created_at,
            next_attempt_at=created_at,
            idempotency_key=f"portfolio-alert-dashboard-{target_alert.id}-{target_alert.trigger_cycle}",
        )
        session.add(dash_row)
        queued.append(dash_row)

    if pref.email_enabled and pref.email_recipients:
        email_id = str(uuid.uuid4())
        email_row = NotificationOutbox(
            id=email_id,
            target_alert_id=target_alert.id,
            channel="email",
            destination=",".join(pref.email_recipients),
            delivery_mode="live",
            status="queued",
            attempts=0,
            max_attempts=MAX_ATTEMPTS,
            created_at=created_at,
            next_attempt_at=created_at,
            idempotency_key=f"portfolio-alert-email-{target_alert.id}-{target_alert.trigger_cycle}",
        )
        session.add(email_row)
        queued.append(email_row)

    if not queued:
        return [create_dry_run_outbox(session, target_alert, now=created_at)]
    return queued
