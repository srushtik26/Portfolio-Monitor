"""Add team authentication, sessions, activity, and ownership references.

Revision ID: 0007_team_auth
Revises: 0006_production_monitoring_upgrade
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_team_auth"
down_revision: Union[str, None] = "0006_production_monitoring_upgrade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("normalized_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('ADMIN', 'EMPLOYEE')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_users_normalized_name", "users", ["normalized_name"], unique=True)
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True)
    op.create_table(
        "activity_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_activity_logs_timestamp", "activity_logs", ["timestamp"])
    op.create_table(
        "login_rate_limits",
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("fingerprint"),
    )

    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.add_column(sa.Column("uploader_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("approver_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("rejector_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("stored_file_path", sa.String(length=500), nullable=True))
        batch_op.create_foreign_key("fk_import_batches_uploader", "users", ["uploader_user_id"], ["id"], ondelete="SET NULL")
        batch_op.create_foreign_key("fk_import_batches_approver", "users", ["approver_user_id"], ["id"], ondelete="SET NULL")
        batch_op.create_foreign_key("fk_import_batches_rejector", "users", ["rejector_user_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("target_revisions") as batch_op:
        batch_op.add_column(sa.Column("changed_by_user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_target_revisions_changed_by", "users", ["changed_by_user_id"], ["id"], ondelete="SET NULL")

    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_audit_logs_user", "users", ["user_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_constraint("fk_audit_logs_user", type_="foreignkey")
        batch_op.drop_column("user_id")
    with op.batch_alter_table("target_revisions") as batch_op:
        batch_op.drop_constraint("fk_target_revisions_changed_by", type_="foreignkey")
        batch_op.drop_column("changed_by_user_id")
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.drop_constraint("fk_import_batches_rejector", type_="foreignkey")
        batch_op.drop_constraint("fk_import_batches_approver", type_="foreignkey")
        batch_op.drop_constraint("fk_import_batches_uploader", type_="foreignkey")
        batch_op.drop_column("stored_file_path")
        batch_op.drop_column("rejector_user_id")
        batch_op.drop_column("approver_user_id")
        batch_op.drop_column("uploader_user_id")

    op.drop_table("login_rate_limits")
    op.drop_index("ix_activity_logs_timestamp", table_name="activity_logs")
    op.drop_table("activity_logs")
    op.drop_index("uq_user_sessions_token_hash", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("uq_users_normalized_name", table_name="users")
    op.drop_table("users")