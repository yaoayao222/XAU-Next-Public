from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


OWNER_CHECK = "owner IN ('market','analysis','plans','portfolio','review','settings')"


class Document(Base):
    __tablename__ = "documents"
    owner: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    parent_id: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    available_at: Mapped[str | None] = mapped_column(String)
    recorded_at: Mapped[str] = mapped_column(String)
    __table_args__ = (
        CheckConstraint(OWNER_CHECK, name="ck_document_owner"),
        CheckConstraint("revision >= 1", name="ck_document_revision"),
        Index("ix_document_time", "owner", "kind", "recorded_at", "id"),
        Index("ix_document_parent", "owner", "kind", "parent_id", "recorded_at", "id"),
        Index("ix_document_state", "owner", "kind", "state", "recorded_at", "id"),
        Index("ix_document_available", "owner", "kind", "available_at"),
    )


class DocumentRevision(Base):
    __tablename__ = "document_revisions"
    owner: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[str] = mapped_column(String)
    __table_args__ = (
        ForeignKeyConstraint(["owner", "kind", "id"], ["documents.owner", "documents.kind", "documents.id"]),
    )


class PayloadBlob(Base):
    __tablename__ = "payload_blobs"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    codec: Mapped[str] = mapped_column(String)
    uncompressed_size: Mapped[int] = mapped_column(Integer)
    compressed: Mapped[bytes] = mapped_column(LargeBinary)
    __table_args__ = (
        CheckConstraint("length(sha256) = 64", name="ck_payload_sha256_length"),
        CheckConstraint("uncompressed_size >= 0", name="ck_payload_uncompressed_size"),
    )


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    relative_path: Mapped[str] = mapped_column(String)
    size: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String)
    recorded_at: Mapped[str] = mapped_column(String)
    __table_args__ = (CheckConstraint("size >= 0", name="ck_attachment_size"),)
