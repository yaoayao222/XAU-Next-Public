from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from xau_next.platform.db.models import Base


class NotificationRow(Base):
    __tablename__ = "local_notifications"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String, unique=True)
    scope_key: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    submitted_at: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_local_notification_queue", "state", "created_at"),
                      Index("ix_local_notification_scope", "scope_key", "created_at"))


class NotificationAttempt(Base):
    __tablename__ = "local_notification_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notification_id: Mapped[str] = mapped_column(ForeignKey("local_notifications.id"))
    at: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text)
