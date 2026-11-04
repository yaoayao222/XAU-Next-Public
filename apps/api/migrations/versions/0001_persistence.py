"""Owner-bound documents, revision history and immutable attachment metadata."""

import sqlalchemy as sa
from alembic import op

revision = "0001_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "documents",
        sa.Column("owner", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_id", sa.String()),
        sa.Column("state", sa.String()),
        sa.Column("available_at", sa.String()),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "owner IN ('market','analysis','plans','portfolio','review','settings')", name="ck_document_owner"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_document_revision"),
    )
    for name, columns in (
        ("time", ["recorded_at", "id"]),
        ("parent", ["parent_id", "recorded_at", "id"]),
        ("state", ["state", "recorded_at", "id"]),
        ("available", ["available_at"]),
    ):
        op.create_index("ix_document_" + name, "documents", ["owner", "kind", *columns])
    op.create_table(
        "document_revisions",
        sa.Column("owner", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner", "kind", "id"], ["documents.owner", "documents.kind", "documents.id"]
        ),
    )
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.CheckConstraint("size >= 0", name="ck_attachment_size"),
    )


def downgrade():
    raise RuntimeError("Use a verified backup in an independent DATA_ROOT to roll back")
