"""Immutable, hash-verified attachments registered inside a UnitOfWork."""

from .store import AttachmentRef, AttachmentStore, PreparedAttachment

__all__ = ["AttachmentRef", "AttachmentStore", "PreparedAttachment"]
