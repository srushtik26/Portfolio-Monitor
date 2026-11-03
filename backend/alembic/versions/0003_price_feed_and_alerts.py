"""Add quote history and once-per-target-revision alerts.

Revision ID: 0003_price_feed_and_alerts
Revises: 0002_imports_and_target_history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_price_feed_and_alerts"
down_revision: Union[str, None] = "0002_imports_and_target_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("target_revisions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_target_revisions_company_version",
            ["exchange", "symbol", "version"],
        )

    op.create_table(
        "price_quotes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(length=3), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint("price > 0", name="ck_price_quotes_positive_price"),
        sa.CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_price_quotes_exchange"),
        sa.CheckConstraint(
            "status IN ('fresh', 'stale', 'out_of_order', 'simulated')",
            name="ck_price_quotes_status",
        ),
        sa.ForeignKeyConstraint(
            ["exchange", "symbol"],
            ["companies.exchange", "companies.symbol"],
            name="fk_price_quotes_company",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_price_quotes_company_time",
        "price_quotes",
        ["exchange", "symbol", "quoted_at"],
    )
    op.create_table(
        "target_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_revision_id", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(length=8), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(18, 6), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('base', 'bull')", name="ck_target_alerts_target_kind"
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"], ["target_revisions.id"]
        ),
        sa.ForeignKeyConstraint(["quote_id"], ["price_quotes.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_revision_id",
            "target_kind",
            name="uq_target_alerts_revision_target",
        ),
    )


def downgrade() -> None:
    op.drop_table("target_alerts")
    op.drop_index("ix_price_quotes_company_time", table_name="price_quotes")
    op.drop_table("price_quotes")
    with op.batch_alter_table("target_revisions") as batch_op:
        batch_op.drop_constraint("uq_target_revisions_company_version", type_="unique")
