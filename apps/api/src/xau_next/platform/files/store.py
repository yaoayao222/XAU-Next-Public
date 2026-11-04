from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from sqlalchemy import select
from xau_next.platform.db import Database, ReadScope, UnitOfWork
from xau_next.platform.db.core import session_for
from xau_next.platform.db.models import Attachment
from xau_next.shared.values import new_id


@dataclass(frozen=True)
class AttachmentRef:
    id: str
    sha256: str
    relative_path: str
    size: int
    media_type: str


@dataclass(frozen=True)
class PreparedAttachment(AttachmentRef):
    mtime_ns: int
    inode: int


class AttachmentStore:
    def __init__(self, db: Database):
        self._db = db

    def prepare(self, content: bytes, *, media_type: str = "application/octet-stream") -> PreparedAttachment:
        return self._prepare(content, media_type, new_id(), reuse=False)

    def prepare_shared(self, content: bytes, *, media_type: str) -> PreparedAttachment:
        """Opt-in immutable content identity; existing callers retain random IDs."""
        identity = hashlib.sha256(media_type.encode() + b"\0" + content).hexdigest()
        return self._prepare(content, media_type, "shared-" + identity, reuse=True)

    def _prepare(self, content: bytes, media_type: str, id: str, *, reuse: bool) -> PreparedAttachment:
        digest = hashlib.sha256(content).hexdigest()
        relative = f"attachments/{digest[:2]}/{digest}"
        target = self._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if reuse and target.exists():
            self._verify(target.read_bytes(), digest, len(content))
            stat = target.stat()
            return PreparedAttachment(id, digest, relative, len(content), media_type,
                                      stat.st_mtime_ns, stat.st_ino)
        temporary = self._db.data_root / "staging" / f"{new_id()}.tmp"
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists():
                self._verify(target.read_bytes(), digest, len(content))
                temporary.unlink()
            else:
                os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        stat = target.stat()
        return PreparedAttachment(
            id, digest, relative, len(content), media_type, stat.st_mtime_ns, stat.st_ino
        )

    def register(self, scope: UnitOfWork, prepared: PreparedAttachment) -> AttachmentRef:
        session = session_for(scope, self._db, write=True)
        stat = self._path(prepared.relative_path).stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (
            prepared.size,
            prepared.mtime_ns,
            prepared.inode,
        ):
            raise ValueError("附件在准备后已变化，必须重新准备")
        existing = session.get(Attachment, prepared.id)
        if existing:
            if (existing.sha256, existing.relative_path, existing.size, existing.media_type) != (
                prepared.sha256,
                prepared.relative_path,
                prepared.size,
                prepared.media_type,
            ):
                raise ValueError("附件标识已被其它内容占用")
        else:
            session.add(
                Attachment(
                    id=prepared.id,
                    sha256=prepared.sha256,
                    relative_path=prepared.relative_path,
                    size=prepared.size,
                    media_type=prepared.media_type,
                    recorded_at=self._db.now(),
                )
            )
            session.flush()
        return AttachmentRef(
            prepared.id, prepared.sha256, prepared.relative_path, prepared.size, prepared.media_type
        )

    def get(self, scope: ReadScope, id: str) -> AttachmentRef | None:
        row = session_for(scope, self._db).get(Attachment, id)
        return AttachmentRef(row.id, row.sha256, row.relative_path, row.size, row.media_type) if row else None

    def read(self, scope: ReadScope, id: str) -> bytes:
        ref = self.get(scope, id)
        if ref is None:
            raise KeyError("附件尚未登记或不存在")
        content = self._path(ref.relative_path).read_bytes()
        self._verify(content, ref.sha256, ref.size)
        return content

    def read_many(self, scope: ReadScope, ids) -> dict[str, bytes]:
        """Bound SQL lookups while still verifying every referenced file."""
        ids = list(dict.fromkeys(ids))
        session = session_for(scope, self._db)
        result = {}
        for start in range(0, len(ids), 200):
            rows = session.scalars(select(Attachment).where(Attachment.id.in_(ids[start:start + 200])))
            for row in rows:
                content = self._path(row.relative_path).read_bytes()
                self._verify(content, row.sha256, row.size)
                result[row.id] = content
        if len(result) != len(ids):
            raise KeyError("附件尚未登记或不存在")
        return result

    def _path(self, relative: str):
        path = (self._db.data_root / relative).resolve()
        if not path.is_relative_to(self._db.data_root / "attachments"):
            raise ValueError("附件路径超出数据目录")
        return path

    @staticmethod
    def _verify(content: bytes, sha256: str, size: int) -> None:
        if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
            raise ValueError("附件长度或 SHA256 校验失败")
