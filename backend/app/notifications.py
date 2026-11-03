import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import NotificationOutbox, TargetAlert, TargetRevision

logger = logging.getLogger("portfolio.notifications")
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


@dataclass(frozen=True)
class SendReceipt:
    provider_message_id: str | None


class NotificationNotifier(Protocol):
    supports_idempotency: bool

    def send(self, message: NotificationMessage, idempotency_key: str) -> SendReceipt: ...


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
    target_value = revision.base_target if alert.target_kind == "base" else revision.bull_target
    if target_value is None:
        raise ValueError(f"Target alert {alert.id} has no corresponding target value.")
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
    notifier: NotificationNotifier,
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    now: datetime | None = None,
    limit: int = 100,
) -> DrainResult:
    if limit < 1:
        raise ValueError("Outbox drain limit must be at least one.")
    current_time = now or datetime.now(timezone.utc)
    sent = retried = failed = 0
    for _ in range(limit):
        with session_factory() as session:
            outbox = _claim_one(session, current_time)
            if outbox is None:
                break
            if outbox.delivery_mode != "dry_run":
                reason = (
                    "Live notifications require a notifier with provider-supported idempotency."
                    if not notifier.supports_idempotency
                    else "No live channel or destination is configured; no destination was contacted."
                )
                outcome = _complete(
                    session_factory,
                    outbox.id,
                    outbox.claim_token or "",
                    now=current_time,
                    error=PermanentSendError(reason),
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
            receipt = notifier.send(message, idempotency_key)
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
