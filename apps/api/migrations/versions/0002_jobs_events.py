"""Persistent jobs, idempotency results and ordered event replay."""

import sqlalchemy as sa
from alembic import op

revision = "0002_jobs_events"
down_revision = "0001_persistence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("dedup_key", sa.String()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Float()),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_json", sa.Text()),
        sa.Column("lease_owner", sa.String()),
        sa.Column("lease_token", sa.String()),
        sa.Column("lease_expires_at", sa.String()),
        sa.Column("heartbeat_at", sa.String()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.UniqueConstraint("kind", "dedup_key", name="uq_job_dedup"),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelling','cancelled')", name="ck_job_state"
        ),
        sa.CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="ck_job_attempt"),
        sa.CheckConstraint("revision >= 1", name="ck_job_revision"),
    )
    op.create_index("ix_job_queue", "jobs", ["state", "created_at", "job_id"])
    op.create_index("ix_job_lease", "jobs", ["state", "lease_expires_at"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.String(), nullable=False),
        sa.Column("resource_kind", sa.String()),
        sa.Column("resource_id", sa.String()),
        sa.Column("job_id", sa.String()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_event_revision"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_event_time", "events", ["occurred_at"])
    op.create_table(
        "event_retention",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("floor", sa.Integer(), nullable=False),
    )
    op.create_table(
        "idempotency_requests",
        sa.Column("method", sa.String(), primary_key=True),
        sa.Column("path", sa.String(), primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_idempotency_time", "idempotency_requests", ["created_at"])


def downgrade():
    raise RuntimeError("Use a verified backup in an independent DATA_ROOT to roll back")
