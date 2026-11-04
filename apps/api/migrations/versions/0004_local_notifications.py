"""Durable local notification outbox; no historical business rows are rewritten."""

import sqlalchemy as sa
from alembic import op

revision = "0004_local_notifications"
down_revision = "0003_payload_blobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("local_notifications",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("dedup_key", sa.String(), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.String(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True))
    op.create_index("ix_local_notification_queue", "local_notifications", ["state", "created_at"])
    op.create_index("ix_local_notification_scope", "local_notifications", ["scope_key", "created_at"])
    op.create_table("local_notification_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("notification_id", sa.String(), sa.ForeignKey("local_notifications.id"), nullable=False),
        sa.Column("at", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True))


def downgrade():
    raise RuntimeError("Use a verified backup in an independent DATA_ROOT to roll back")
