"""Byte-preserving payload encoding and explicitly invoked offline compaction."""

from __future__ import annotations

import hashlib
import re
import zlib
from collections.abc import Callable

from sqlalchemy import func, select, tuple_, update
from xau_next.shared.errors import DomainError

from .core import Database, ReadScope, UnitOfWork, poison, session_for
from .models import Document, DocumentRevision, PayloadBlob

PAYLOAD_PREFIX = "@xau-payload-v1:"
PAYLOAD_THRESHOLD_BYTES = 4096
_MARKER_FAMILY = "@xau-payload-"
_CODEC = "zlib"


class PayloadIntegrityError(DomainError):
    def __init__(self, reason: str):
        super().__init__("PAYLOAD_CORRUPT", "持久化内容校验失败，已停止读取。", 500)
        self.reason = reason


def _corrupt(scope: ReadScope, reason: str):
    poison(scope)
    raise PayloadIntegrityError(reason)


class PayloadCodec:
    """Store exact UTF-8 text; callers keep their own JSON and revision semantics."""

    def __init__(self, db: Database):
        self._db = db

    def encode_text(self, scope: UnitOfWork, text: str) -> str:
        """Encode raw text in the caller's write transaction, without JSON reserialization."""
        session = session_for(scope, self._db, write=True)
        raw = text.encode("utf-8")
        if len(raw) < PAYLOAD_THRESHOLD_BYTES:
            return text
        digest = hashlib.sha256(raw).hexdigest()
        existing = session.get(PayloadBlob, digest)
        if existing is None:
            session.add(
                PayloadBlob(
                    sha256=digest,
                    codec=_CODEC,
                    uncompressed_size=len(raw),
                    compressed=zlib.compress(raw, level=6),
                )
            )
            session.flush()
        elif self._decode_blob(scope, existing, digest) != raw:
            _corrupt(scope, "digest_collision")
        return PAYLOAD_PREFIX + digest

    def decode_text(self, scope: ReadScope, stored: str) -> str:
        """Return legacy inline text or verify and decode a versioned blob reference."""
        session = session_for(scope, self._db)
        if not stored.startswith(_MARKER_FAMILY):
            return stored
        if not stored.startswith(PAYLOAD_PREFIX):
            _corrupt(scope, "unsupported_marker")
        digest = stored[len(PAYLOAD_PREFIX) :]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            _corrupt(scope, "invalid_marker")
        blob = session.get(PayloadBlob, digest)
        if blob is None:
            _corrupt(scope, "missing_blob")
        raw = self._decode_blob(scope, blob, digest)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            _corrupt(scope, "invalid_utf8")

    @staticmethod
    def _decode_blob(scope: ReadScope, blob: PayloadBlob, digest: str) -> bytes:
        if blob.codec != _CODEC:
            _corrupt(scope, "unsupported_codec")
        size = blob.uncompressed_size
        if type(size) is not int or size < 0:
            _corrupt(scope, "invalid_length")
        try:
            decoder = zlib.decompressobj()
            # Stop one byte beyond the stored length, including malformed oversized streams.
            raw = decoder.decompress(blob.compressed, size + 1)
        except (zlib.error, OverflowError, TypeError):
            _corrupt(scope, "invalid_compressed_data")
        if len(raw) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            _corrupt(scope, "length_or_stream_mismatch")
        if hashlib.sha256(raw).hexdigest() != digest:
            _corrupt(scope, "sha256_mismatch")
        return raw


def _blob_totals(db: Database) -> dict:
    with db.read() as scope:
        count, size, compressed = (
            session_for(scope, db)
            .execute(
                select(
                    func.count(PayloadBlob.sha256),
                    func.coalesce(func.sum(PayloadBlob.uncompressed_size), 0),
                    func.coalesce(func.sum(func.length(PayloadBlob.compressed)), 0),
                )
            )
            .one()
        )
    return {"count": count, "uncompressed_bytes": size, "compressed_bytes": compressed}


def compact_payloads(
    db: Database, *, batch_size: int = 100, progress: Callable[[dict], None] | None = None
) -> dict:
    """Explicit offline maintenance. Each batch commits independently and can be resumed.

    Only payload columns change; original text bytes, keys, timestamps, revisions and
    events are preserved. Existing markers are verified. This never runs at startup,
    removes blobs or vacuums the database. Use a stopped application or isolated copy.
    """
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("压缩批次必须为 1 到 1000")
    before = _blob_totals(db)
    stats = {
        "documents_scanned": 0,
        "documents_compacted": 0,
        "document_revisions_scanned": 0,
        "document_revisions_compacted": 0,
        "existing_markers_verified": 0,
        "inline_bytes_replaced": 0,
        "marker_bytes_written": 0,
        "committed_batches": 0,
    }
    codec = PayloadCodec(db)
    for model in (Document, DocumentRevision):
        key_columns = list(model.__table__.primary_key.columns)
        anchor = None
        while True:
            increments = {key: 0 for key in stats}
            with db.write() as scope:
                session = session_for(scope, db, write=True)
                query = select(*key_columns, model.payload).order_by(*key_columns).limit(batch_size)
                if anchor is not None:
                    query = query.where(tuple_(*key_columns) > anchor)
                rows = session.execute(query).all()
                if not rows:
                    break
                for row in rows:
                    original = row[-1]
                    increments[model.__tablename__ + "_scanned"] += 1
                    if original.startswith(_MARKER_FAMILY):
                        codec.decode_text(scope, original)
                        increments["existing_markers_verified"] += 1
                        continue
                    encoded = codec.encode_text(scope, original)
                    if encoded == original:
                        continue
                    session.execute(
                        update(model)
                        .where(*(column == value for column, value in zip(key_columns, row[:-1])))
                        .values(payload=encoded)
                        .execution_options(synchronize_session=False)
                    )
                    increments[model.__tablename__ + "_compacted"] += 1
                    increments["inline_bytes_replaced"] += len(original.encode("utf-8"))
                    increments["marker_bytes_written"] += len(encoded)
                anchor = tuple(rows[-1][:-1])
            increments["committed_batches"] = 1
            for name, value in increments.items():
                stats[name] += value
            if progress is not None:
                progress(dict(stats))
    after = _blob_totals(db)
    return {
        **stats,
        "format": "xau-next-payload-compaction/1",
        "threshold_bytes": PAYLOAD_THRESHOLD_BYTES,
        "blobs_before": before,
        "blobs_after": after,
        "blobs_created": after["count"] - before["count"],
        "business_revisions_changed": 0,
    }
