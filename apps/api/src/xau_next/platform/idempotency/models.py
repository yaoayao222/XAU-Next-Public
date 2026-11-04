from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from xau_next.platform.db.models import Base


class IdempotencyRow(Base):
    __tablename__ = "idempotency_requests"
    method: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    __table_args__ = (Index("ix_idempotency_time", "created_at"),)
