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
    saved_cmp: str | None = None
    sector: str | None = None
    trigger_cycle: int = 1


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
    rule_symbol = "<=" if message.comparison_direction == "below" else ">="
    ts_utc = (
        message.triggered_at.replace(tzinfo=timezone.utc)
        if message.triggered_at.tzinfo is None
        else message.triggered_at.astimezone(timezone.utc)
    )
    ts_ist = ts_utc.astimezone(INDIA_TZ).strftime("%Y-%m-%d %H:%M:%S IST")
    target_num: float | None = None
    quote_num: float | None = None
    try:
        target_num = float(message.target_price)
        target_fmt = f"₹{target_num:,.2f}"
    except (ValueError, TypeError):
        target_fmt = f"₹{message.target_price}"
    try:
        quote_num = float(message.quote_price)
        quote_fmt = f"₹{quote_num:,.2f}"
    except (ValueError, TypeError):
        quote_fmt = f"₹{message.quote_price}"

    variance_line = ""
    if target_num and target_num > 0 and quote_num is not None:
        diff_abs = quote_num - target_num
        diff_pct = (diff_abs / target_num) * 100.0
        sign = "+" if diff_abs > 0 else ""
        variance_line = f"Variance vs Target: {sign}₹{diff_abs:,.2f} ({sign}{diff_pct:.2f}%)\n"

    saved_cmp_line = ""
    if message.saved_cmp is not None:
        try:
            cmp_num = float(message.saved_cmp)
            if cmp_num > 0 and quote_num is not None:
                cmp_pct = ((quote_num - cmp_num) / cmp_num) * 100.0
                cmp_sign = "+" if cmp_pct > 0 else ""
                saved_cmp_line = f"Saved Reference CMP: ₹{cmp_num:,.2f} ({cmp_sign}{cmp_pct:.2f}%)\n"
            else:
                saved_cmp_line = f"Saved Reference CMP: ₹{cmp_num:,.2f}\n"
        except (ValueError, TypeError):
            pass

    sector_line = f"Sector: {message.sector}\n" if message.sector else ""

    subject = f"Price Target Alert – {company_label} ({message.exchange}:{message.symbol}) [{target_type} Target {quote_fmt}]"
    body = (
        f"{company_label} has reached its configured {target_type} Target Price.\n\n"
        f"Company: {company_label}\n"
        f"Symbol: {message.symbol}\n"
        f"Exchange: {message.exchange}\n"
        f"{sector_line}"
        f"Target Type: {target_type}\n"
        f"Trigger Rule: Current Price {rule_symbol} {target_type} Target ({direction_label})\n"
        f"Target Price: {target_fmt}\n"
        f"Current Price: {quote_fmt}\n"
        f"{variance_line}"
        f"{saved_cmp_line}"
        f"Trigger Time: {ts_ist}\n"
        f"Price Direction: {direction_label}\n"
        f"Alert Cycle: #{message.trigger_cycle}\n\n"
        f"--\n"
        f"Northstar Portfolio Monitor · Plutus Wealth Management\n"
        f"Automated Price-Monitoring & Target-Alert System (No Order Execution)\n"
    )
    return subject, body


def format_alert_email_html(message: NotificationMessage) -> str:
    """Render a clean, institutional HTML email body for price target alerts."""
    company_label = message.company_name or message.symbol
    is_base = message.target_kind == "base"
    target_type = "Base" if is_base else "Bull"
    direction_label = "Downward" if message.comparison_direction == "below" else "Upward"
    rule_symbol = "&le;" if message.comparison_direction == "below" else "&ge;"
    badge_bg = "#0f3d2e" if is_base else "#451a23"
    badge_fg = "#34d399" if is_base else "#f87171"
    badge_border = "#10b981" if is_base else "#ef4444"

    ts_utc = (
        message.triggered_at.replace(tzinfo=timezone.utc)
        if message.triggered_at.tzinfo is None
        else message.triggered_at.astimezone(timezone.utc)
    )
    ts_ist = ts_utc.astimezone(INDIA_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")

    target_num: float | None = None
    quote_num: float | None = None
    try:
        target_num = float(message.target_price)
        target_fmt = f"₹{target_num:,.2f}"
    except (ValueError, TypeError):
        target_fmt = f"₹{message.target_price}"
    try:
        quote_num = float(message.quote_price)
        quote_fmt = f"₹{quote_num:,.2f}"
    except (ValueError, TypeError):
        quote_fmt = f"₹{message.quote_price}"

    variance_fmt = "—"
    if target_num and target_num > 0 and quote_num is not None:
        diff_abs = quote_num - target_num
        diff_pct = (diff_abs / target_num) * 100.0
        sign = "+" if diff_abs > 0 else ""
        variance_fmt = f"{sign}₹{diff_abs:,.2f} ({sign}{diff_pct:.2f}%)"

    saved_cmp_fmt = "—"
    if message.saved_cmp is not None:
        try:
            cmp_num = float(message.saved_cmp)
            saved_cmp_fmt = f"₹{cmp_num:,.2f}"
        except (ValueError, TypeError):
            pass

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:24px;background-color:#0b1120;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#e2e8f0;">
  <div style="max-width:580px;margin:0 auto;background-color:#111827;border:1px solid #1e293b;border-radius:12px;overflow:hidden;">
    <div style="padding:20px 24px;border-bottom:1px solid #1e293b;background-color:#0f172a;">
      <div style="font-size:11px;letter-spacing:0.12em;text-transform:uppercase;color:#94a3b8;font-weight:600;">
        NORTHSTAR PORTFOLIO MONITOR · TARGET ALERT
      </div>
      <h2 style="margin:8px 0 4px;font-size:20px;color:#f8fafc;">
        {company_label} <span style="color:#64748b;font-weight:500;">({message.exchange}:{message.symbol})</span>
      </h2>
      <span style="display:inline-block;margin-top:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;background:{badge_bg};color:{badge_fg};border:1px solid {badge_border};">
        {target_type.upper()} TARGET TRIGGERED ({direction_label.upper()})
      </span>
    </div>
    <div style="padding:20px 24px;">
      <p style="margin:0 0 16px;font-size:14px;line-height:1.5;color:#cbd5e1;">
        <strong>{company_label}</strong> has reached its configured <strong>{target_type} Target Price</strong> ({rule_symbol} {target_fmt}).
      </p>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#94a3b8;">Current Market Price (CMP)</td>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#f8fafc;font-weight:700;text-align:right;font-size:15px;">{quote_fmt}</td>
        </tr>
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#94a3b8;">Configured {target_type} Target</td>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:{badge_fg};font-weight:700;text-align:right;font-size:15px;">{target_fmt}</td>
        </tr>
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#94a3b8;">Variance vs Target</td>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#e2e8f0;font-weight:600;text-align:right;">{variance_fmt}</td>
        </tr>
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#94a3b8;">Saved Reference CMP</td>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#cbd5e1;text-align:right;">{saved_cmp_fmt}</td>
        </tr>
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#94a3b8;">Price Direction &amp; Rule</td>
          <td style="padding:10px 12px;border-bottom:1px solid #1e293b;color:#cbd5e1;text-align:right;">{direction_label} ({rule_symbol} Target)</td>
        </tr>
        <tr>
          <td style="padding:10px 12px;color:#94a3b8;">Trigger Timestamp</td>
          <td style="padding:10px 12px;color:#cbd5e1;text-align:right;">{ts_ist}</td>
        </tr>
      </table>
    </div>
    <div style="padding:14px 24px;background-color:#0f172a;border-top:1px solid #1e293b;font-size:11px;color:#64748b;">
      Northstar Portfolio Monitor · Price-Monitoring &amp; Target-Alert System · Zero Order Execution
    </div>
  </div>
</body>
</html>"""


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
        self.password = (password if password is not None else os.environ.get("SMTP_PASSWORD", "")).strip()
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

        # Attach rich HTML multipart alternative for real SMTP delivery
        email_msg.add_alternative(format_alert_email_html(message), subtype="html")

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


def is_smtp_complete() -> tuple[bool, list[str]]:
    """Return whether complete SMTP settings (or a test mail spool directory) are configured."""
    if os.environ.get("SMTP_SPOOL_DIR", "").strip():
        return True, []
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    return len(missing) == 0, missing


def check_live_notification_gate() -> tuple[bool, str]:
    """Require BOTH NOTIFICATION_MODE=live AND complete SMTP settings before any real send."""
    raw_mode = os.environ.get("NOTIFICATION_MODE", "dry_run").strip().lower() or "dry_run"
    if raw_mode != "live":
        return (
            False,
            f"NOTIFICATION_MODE is '{raw_mode}' (must be 'live'); staying in dry_run.",
        )
    smtp_ok, missing = is_smtp_complete()
    if not smtp_ok:
        return (
            False,
            f"NOTIFICATION_MODE=live but SMTP settings are incomplete (missing {', '.join(missing)}); staying in dry_run.",
        )
    return True, "NOTIFICATION_MODE=live and SMTP settings complete."


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
        live_ready, reason = check_live_notification_gate()
        if not live_ready:
            logger.warning("Notification send staying in dry-run: %s", reason)
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
        comparison_direction=alert.comparison_direction or ("below" if alert.target_kind == "base" else "above"),
        channel=outbox.channel,
        destination=outbox.destination,
        saved_cmp=str(company.cmp) if company and company.cmp is not None else None,
        sector=company.sector if company else None,
        trigger_cycle=alert.trigger_cycle or 1,
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
    preference: NotificationPreference | None = None,
    now: datetime | None = None,
) -> list[NotificationOutbox]:
    """Queue notifications for a triggered alert in the active SQLAlchemy transaction.

    Simulated alerts always use the dry-run channel.
    Live alerts require BOTH NOTIFICATION_MODE=live AND complete SMTP settings
    before queuing live sends; otherwise they stay in dry-run and log why.
    """
    created_at = now or datetime.now(timezone.utc)
    if target_alert.is_simulated:
        return [create_dry_run_outbox(session, target_alert, now=created_at)]

    live_ready, gate_reason = check_live_notification_gate()
    if not live_ready:
        logger.warning("Alert %s notification staying in dry-run: %s", target_alert.id, gate_reason)
        return [create_dry_run_outbox(session, target_alert, now=created_at)]

    pref = preference if preference is not None else session.get(NotificationPreference, "default")
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


def write_alert_to_outbox(
    session: Session,
    target_alert: TargetAlert,
    *,
    preference: NotificationPreference | None = None,
    now: datetime | None = None,
) -> list[NotificationOutbox]:
    """Write a newly triggered Base or Bull target alert into `notification_outbox`
    within the caller's existing SQLAlchemy session transaction.
    """
    return queue_alert_notifications(
        session,
        target_alert,
        preference=preference,
        now=now,
    )

