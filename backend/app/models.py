from decimal import Decimal
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_companies_exchange"),
        CheckConstraint(
            "base_direction IN ('below', 'above', 'pending')",
            name="ck_companies_base_direction",
        ),
        CheckConstraint(
            "bull_direction IN ('below', 'above', 'pending')",
            name="ck_companies_bull_direction",
        ),
        CheckConstraint(
            "base_trigger_mode IN ('at_or_beyond', 'crosses')",
            name="ck_companies_base_trigger_mode",
        ),
        CheckConstraint(
            "bull_trigger_mode IN ('at_or_beyond', 'crosses')",
            name="ck_companies_bull_trigger_mode",
        ),
        CheckConstraint(
            "rearm_policy IN ('once_per_revision', 'rearm_on_reset', 'rearm_after_recovery', 'cooldown')",
            name="ck_companies_rearm_policy",
        ),
    )

    exchange: Mapped[str] = mapped_column(String(3), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cmp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    bull_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    monitoring_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    base_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="above")
    bull_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="above")
    base_trigger_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="at_or_beyond")
    bull_trigger_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="at_or_beyond")
    rearm_policy: Mapped[str] = mapped_column(String(24), nullable=False, default="once_per_revision")
    cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    source_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __mapper_args__ = {"version_id_col": version}


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="preview")
    formula_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(100), nullable=False, default="Srushti Kadam")
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    header_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    changes_snapshot: Mapped[list[dict] | None] = mapped_column(JSON, nullable=True)

    rows: Mapped[list["ImportRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportRow.row_number"
    )


class ImportRow(Base):
    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("import_batches.id"), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(3), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sector: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cmp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    bull_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    update_cmp: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    update_sector: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    update_base_target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    update_bull_target: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    errors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    base_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bull_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    cell_refs: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="approved")

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


class TargetRevision(Base):
    __tablename__ = "target_revisions"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", "version", name="uq_target_revisions_company_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(3), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    bull_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    import_id: Mapped[str | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    previous_base_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    previous_bull_target: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    base_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    bull_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    changed_by: Mapped[str] = mapped_column(String(100), nullable=False, default="Srushti Kadam")
    cell_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PriceQuote(Base):
    __tablename__ = "price_quotes"
    __table_args__ = (
        CheckConstraint("price > 0", name="ck_price_quotes_positive_price"),
        CheckConstraint(
            "exchange IN ('NSE', 'BSE')", name="ck_price_quotes_exchange"
        ),
        CheckConstraint(
            "status IN ('fresh', 'stale', 'out_of_order', 'simulated')",
            name="ck_price_quotes_status",
        ),
        ForeignKeyConstraint(
            ["exchange", "symbol"],
            ["companies.exchange", "companies.symbol"],
            name="fk_price_quotes_company",
        ),
        Index("ix_price_quotes_company_time", "exchange", "symbol", "quoted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(3), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    quote_type: Mapped[str] = mapped_column(String(24), nullable=False, default="last_trade")
    freshness_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class TargetAlertState(Base):
    __tablename__ = "target_alert_states"
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('base', 'bull')", name="ck_target_alert_states_kind"
        ),
        CheckConstraint(
            "state IN ('armed', 'triggered', 'acknowledged', 'resolved', 'pending_configuration')",
            name="ck_target_alert_states_state",
        ),
        ForeignKeyConstraint(
            ["exchange", "symbol"],
            ["companies.exchange", "companies.symbol"],
            name="fk_target_alert_states_company",
        ),
    )

    exchange: Mapped[str] = mapped_column(String(3), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=False)

    state: Mapped[str] = mapped_column(String(24), nullable=False, default="armed")
    target_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("target_revisions.id"), nullable=True
    )
    last_evaluated_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_condition_met: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_triggered_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    last_alert_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TargetAlert(Base):
    __tablename__ = "target_alerts"
    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('base', 'bull')", name="ck_target_alerts_target_kind"
        ),
        CheckConstraint(
            "comparison_direction IN ('below', 'above')",
            name="ck_target_alerts_direction",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'acknowledged', 'resolved', 'reset')",
            name="ck_target_alerts_lifecycle",
        ),
        UniqueConstraint(
            "target_revision_id",
            "target_kind",
            "is_simulated",
            "trigger_cycle",
            name="uq_target_alerts_revision_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_revision_id: Mapped[int] = mapped_column(
        ForeignKey("target_revisions.id"), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    quote_id: Mapped[int] = mapped_column(ForeignKey("price_quotes.id"), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trigger_cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    comparison_direction: Mapped[str] = mapped_column(String(16), nullable=False, default="above")
    trigger_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="at_or_beyond")
    target_price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'retry', 'sending', 'sent', 'failed')",
            name="ck_notification_outbox_status",
        ),
        CheckConstraint(
            "delivery_mode IN ('dry_run', 'live')",
            name="ck_notification_outbox_delivery_mode",
        ),
        UniqueConstraint("target_alert_id", "channel", name="uq_notification_outbox_alert"),
        UniqueConstraint("idempotency_key", name="uq_notification_outbox_idempotency"),
        Index("ix_notification_outbox_due", "status", "next_attempt_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_alert_id: Mapped[int] = mapped_column(
        ForeignKey("target_alerts.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    destination: Mapped[str | None] = mapped_column(String(320), nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    dashboard_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_recipients: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    notify_on_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_on_bull: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_on_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    exchange_calendar: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    connection_status: Mapped[str] = mapped_column(String(24), nullable=False, default="disconnected")
    websocket_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unavailable")
    last_successful_quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_requests_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconnect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monitored_symbols_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_symbols_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False, default="Srushti Kadam")
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
