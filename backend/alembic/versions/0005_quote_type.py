"""Identify quote vs candle-close timestamps.

Revision ID: 0005_quote_type
Revises: 0004_notification_outbox
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_quote_type"
down_revision: Union[str, None] = "0004_notification_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "price_quotes",
        sa.Column(
            "quote_type",
            sa.String(length=24),
            nullable=False,
            server_default="provider_quote",
        ),
    )
    op.execute(
        "UPDATE price_quotes SET quote_type = "
        "CASE WHEN is_simulated THEN 'fixture' ELSE 'last_trade' END"
    )
    op.add_column(
        "price_quotes",
        sa.Column(
            "freshness_seconds",
            sa.Integer(),
            nullable=False,
            server_default="900",
        ),
    )


def downgrade() -> None:
    op.drop_column("price_quotes", "freshness_seconds")
    op.drop_column("price_quotes", "quote_type")
