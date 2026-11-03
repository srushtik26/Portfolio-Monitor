"""Create companies keyed by exchange and symbol.

Revision ID: 0001_create_companies
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_create_companies"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("exchange", sa.String(length=3), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("sector", sa.String(length=100), nullable=True),
        sa.Column("base_target", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("bull_target", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.CheckConstraint("exchange IN ('NSE', 'BSE')", name="ck_companies_exchange"),
        sa.PrimaryKeyConstraint("exchange", "symbol"),
    )


def downgrade() -> None:
    op.drop_table("companies")
