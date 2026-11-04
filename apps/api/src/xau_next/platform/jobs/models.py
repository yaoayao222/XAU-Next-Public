from sqlalchemy import Boolean, CheckConstraint, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from xau_next.platform.db.models import Base


class JobRow(Base):
    __tablename__ = "jobs"
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    input_json: Mapped[str] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(String(64))
    dedup_key: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    progress: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_json: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String)
    lease_token: Mapped[str | None] = mapped_column(String)
    lease_expires_at: Mapped[str | None] = mapped_column(String)
    heartbeat_at: Mapped[str | None] = mapped_column(String)
    cancel_requested: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("kind", "dedup_key", name="uq_job_dedup"),
        CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelling','cancelled')", name="ck_job_state"
        ),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="ck_job_attempt"),
        CheckConstraint("revision >= 1", name="ck_job_revision"),
        Index("ix_job_queue", "state", "created_at", "job_id"),
        Index("ix_job_lease", "state", "lease_expires_at"),
    )
