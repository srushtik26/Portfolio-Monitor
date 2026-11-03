"""Add transactional notification outbox and worker status.

Revision ID: 0004_notification_outbox
Revises: 0003_price_feed_and_alerts
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_notification_outbox"
down_revision: Union[str, None] = "0003_price_feed_and_alerts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("target_alerts") as batch_op:
        batch_op.drop_constraint("uq_target_alerts_revision_target", type_="unique")
        batch_op.add_column(
            sa.Column(
                "is_simulated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_unique_constraint(
            "uq_target_alerts_revision_target",
            ["target_revision_id", "target_kind", "is_simulated"],
        )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("target_alert_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination", sa.String(length=320), nullable=True),
        sa.Column("delivery_mode", sa.String(length=12), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'retry', 'sending', 'sent', 'failed')",
            name="ck_notification_outbox_status",
        ),
        sa.CheckConstraint(
            "delivery_mode IN ('dry_run', 'live')",
            name="ck_notification_outbox_delivery_mode",
        ),
        sa.ForeignKeyConstraint(["target_alert_id"], ["target_alerts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_alert_id", name="uq_notification_outbox_alert"),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_outbox_idempotency"),
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("exchange_calendar", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_notification_outbox_due", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    with op.batch_alter_table("target_alerts") as batch_op:
        batch_op.drop_constraint("uq_target_alerts_revision_target", type_="unique")
        batch_op.drop_column("is_simulated")
        batch_op.create_unique_constraint(
            "uq_target_alerts_revision_target",
            ["target_revision_id", "target_kind"],
        )
