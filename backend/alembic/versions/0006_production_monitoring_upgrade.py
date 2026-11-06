"""Production monitoring upgrade: configurable target rules, alert states, import versioning, notifications, and audit logs.

Revision ID: 0006_production_monitoring_upgrade
Revises: 0005_quote_type
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_production_monitoring_upgrade"
down_revision: Union[str, None] = "0005_quote_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch_op:
        batch_op.add_column(sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("monitoring_active", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("base_direction", sa.String(16), nullable=False, server_default="above"))
        batch_op.add_column(sa.Column("bull_direction", sa.String(16), nullable=False, server_default="above"))
        batch_op.add_column(sa.Column("base_trigger_mode", sa.String(24), nullable=False, server_default="at_or_beyond"))
        batch_op.add_column(sa.Column("bull_trigger_mode", sa.String(24), nullable=False, server_default="at_or_beyond"))
        batch_op.add_column(sa.Column("rearm_policy", sa.String(24), nullable=False, server_default="once_per_revision"))
        batch_op.add_column(sa.Column("cooldown_minutes", sa.Integer(), nullable=False, server_default="60"))
        batch_op.add_column(sa.Column("source_metadata", sa.JSON(), nullable=True))

    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.add_column(sa.Column("version_number", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("uploaded_by", sa.String(100), nullable=False, server_default="Srushti Kadam"))
        batch_op.add_column(sa.Column("approved_by", sa.String(100), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("sheet_name", sa.String(120), nullable=True))
        batch_op.add_column(sa.Column("header_row", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("changes_snapshot", sa.JSON(), nullable=True))

    with op.batch_alter_table("import_rows") as batch_op:
        batch_op.add_column(sa.Column("base_direction", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("bull_direction", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("cell_refs", sa.JSON(), nullable=False, server_default="{}"))
        batch_op.add_column(sa.Column("approval_status", sa.String(16), nullable=False, server_default="approved"))

    with op.batch_alter_table("target_revisions") as batch_op:
        batch_op.add_column(sa.Column("previous_base_target", sa.Numeric(14, 2), nullable=True))
        batch_op.add_column(sa.Column("previous_bull_target", sa.Numeric(14, 2), nullable=True))
        batch_op.add_column(sa.Column("base_direction", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("bull_direction", sa.String(16), nullable=True))
        batch_op.add_column(sa.Column("changed_by", sa.String(100), nullable=False, server_default="Srushti Kadam"))
        batch_op.add_column(sa.Column("cell_refs", sa.JSON(), nullable=True))

    op.create_table(
        "target_alert_states",
        sa.Column("exchange", sa.String(3), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("target_kind", sa.String(8), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(24), nullable=False, server_default="armed"),
        sa.Column("target_revision_id", sa.Integer(), nullable=True),
        sa.Column("last_evaluated_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_condition_met", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_triggered_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("last_alert_id", sa.Integer(), nullable=True),
        sa.Column("trigger_cycle", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("target_kind IN ('base', 'bull')", name="ck_target_alert_states_kind"),
        sa.CheckConstraint(
            "state IN ('armed', 'triggered', 'acknowledged', 'resolved', 'pending_configuration')",
            name="ck_target_alert_states_state",
        ),
        sa.ForeignKeyConstraint(["exchange", "symbol"], ["companies.exchange", "companies.symbol"], name="fk_target_alert_states_company"),
        sa.ForeignKeyConstraint(["target_revision_id"], ["target_revisions.id"]),
        sa.PrimaryKeyConstraint("exchange", "symbol", "target_kind", "is_simulated"),
    )

    with op.batch_alter_table("target_alerts") as batch_op:
        batch_op.drop_constraint("uq_target_alerts_revision_target", type_="unique")
        batch_op.add_column(sa.Column("trigger_cycle", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("comparison_direction", sa.String(16), nullable=False, server_default="above"))
        batch_op.add_column(sa.Column("trigger_mode", sa.String(24), nullable=False, server_default="at_or_beyond"))
        batch_op.add_column(sa.Column("target_price_snapshot", sa.Numeric(14, 2), nullable=True))
        batch_op.add_column(sa.Column("lifecycle_status", sa.String(16), nullable=False, server_default="active"))
        batch_op.add_column(sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint(
            "uq_target_alerts_revision_target",
            ["target_revision_id", "target_kind", "is_simulated", "trigger_cycle"],
        )

    with op.batch_alter_table("notification_outbox") as batch_op:
        batch_op.drop_constraint("uq_notification_outbox_alert", type_="unique")
        batch_op.create_unique_constraint(
            "uq_notification_outbox_alert",
            ["target_alert_id", "channel"],
        )

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("dashboard_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_recipients", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("notify_on_base", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_on_bull", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notify_on_stale", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("worker_heartbeats") as batch_op:
        batch_op.add_column(sa.Column("connection_status", sa.String(24), nullable=False, server_default="disconnected"))
        batch_op.add_column(sa.Column("websocket_status", sa.String(24), nullable=False, server_default="unavailable"))
        batch_op.add_column(sa.Column("last_successful_quote_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("failed_requests_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("reconnect_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monitored_symbols_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("stale_symbols_count", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False, server_default="Srushti Kadam"),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("notification_preferences")
    op.drop_table("target_alert_states")

