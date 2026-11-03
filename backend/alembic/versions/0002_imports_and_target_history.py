"""Add imports, target revisions, CMP, and company versions.

Revision ID: 0002_imports_and_target_history
Revises: 0001_create_companies
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_imports_and_target_history"
down_revision: Union[str, None] = "0001_create_companies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("cmp", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "companies",
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("formula_review_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "import_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("exchange", sa.String(3), nullable=True),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("sector", sa.String(100), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("cmp", sa.Numeric(14, 2), nullable=True),
        sa.Column("base_target", sa.Numeric(14, 2), nullable=True),
        sa.Column("bull_target", sa.Numeric(14, 2), nullable=True),
        sa.Column("update_cmp", sa.Boolean(), nullable=False),
        sa.Column("update_sector", sa.Boolean(), nullable=False),
        sa.Column("update_base_target", sa.Boolean(), nullable=False),
        sa.Column("update_bull_target", sa.Boolean(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
    )
    op.create_table(
        "target_revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("exchange", sa.String(3), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("base_target", sa.Numeric(14, 2), nullable=True),
        sa.Column("bull_target", sa.Numeric(14, 2), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("import_id", sa.String(36), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["import_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("target_revisions")
    op.drop_table("import_rows")
    op.drop_table("import_batches")
    op.drop_column("companies", "version")
    op.drop_column("companies", "cmp")
