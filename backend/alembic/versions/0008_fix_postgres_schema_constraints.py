"""Fix PostgreSQL schema mismatches for price_quotes and notification_outbox.destination.

Revision ID: 0008_fix_postgres_schema
Revises: 0007_team_auth
Create Date: 2026-11-20 12:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_fix_postgres_schema"
down_revision = "0007_team_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("price_quotes", schema=None) as batch_op:
        if dialect != "sqlite":
            batch_op.drop_constraint("ck_price_quotes_status", type_="check")
        batch_op.alter_column(
            "quoted_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=24),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_price_quotes_status",
            "status IN ('fresh', 'stale', 'out_of_order', 'simulated', 'freshness_unknown')",
        )

    with op.batch_alter_table("notification_outbox", schema=None) as batch_op:
        batch_op.alter_column(
            "destination",
            existing_type=sa.String(length=320),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("notification_outbox", schema=None) as batch_op:
        batch_op.alter_column(
            "destination",
            existing_type=sa.Text(),
            type_=sa.String(length=320),
            existing_nullable=True,
        )

    with op.batch_alter_table("price_quotes", schema=None) as batch_op:
        if dialect != "sqlite":
            batch_op.drop_constraint("ck_price_quotes_status", type_="check")
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=24),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "quoted_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_price_quotes_status",
            "status IN ('fresh', 'stale', 'out_of_order', 'simulated')",
        )
