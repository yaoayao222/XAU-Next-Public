"""Add content-addressed payload storage without rewriting historical documents."""

import sqlalchemy as sa
from alembic import op

revision = "0003_payload_blobs"
down_revision = "0002_jobs_events"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payload_blobs",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("codec", sa.String(), nullable=False),
        sa.Column("uncompressed_size", sa.Integer(), nullable=False),
        sa.Column("compressed", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("length(sha256) = 64", name="ck_payload_sha256_length"),
        sa.CheckConstraint("uncompressed_size >= 0", name="ck_payload_uncompressed_size"),
    )


def downgrade():
    raise RuntimeError("Use a verified backup in an independent DATA_ROOT to roll back")
