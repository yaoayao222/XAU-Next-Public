from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from xau_next.platform.db.models import Base


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String)
    occurred_at: Mapped[str] = mapped_column(String)
    resource_kind: Mapped[str | None] = mapped_column(String)
    resource_id: Mapped[str | None] = mapped_column(String)
    job_id: Mapped[str | None] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_event_revision"),
        Index("ix_event_time", "occurred_at"),
        {"sqlite_autoincrement": True},
    )


class EventRetention(Base):
    __tablename__ = "event_retention"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    floor: Mapped[int] = mapped_column(Integer)
