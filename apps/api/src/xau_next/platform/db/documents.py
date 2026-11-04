from __future__ import annotations

import json

from sqlalchemy import and_, or_, select, update
from xau_next.shared.errors import DomainError

from .core import Database, ReadScope, UnitOfWork, canonical_json, normalize_time, poison, session_for
from .models import Document, DocumentRevision

OWNERS = frozenset({"market", "analysis", "plans", "portfolio", "review", "settings"})
_UNCHANGED = object()


class RevisionConflict(DomainError):
    def __init__(self, current_revision: int | None):
        super().__init__("REVISION_CONFLICT", "内容已变化，请刷新后重试", 409)
        self.current_revision = current_revision


class DuplicateDocument(DomainError):
    def __init__(self):
        super().__init__("ALREADY_EXISTS", "该记录已经存在", 409)


class DocumentStore:
    def __init__(self, db: Database, owner: str):
        if owner not in OWNERS:
            raise ValueError("未知数据 owner")
        self._db, self._owner = db, owner
        self._codec = db.payload_codec()

    def _payload(self, scope: ReadScope, row) -> dict:
        return {
            **json.loads(self._codec.decode_text(scope, row.payload)),
            "id": row.id,
            "revision": row.revision,
            "recorded_at": row.recorded_at,
        }

    def get(self, scope: ReadScope, kind: str, id: str) -> dict | None:
        row = session_for(scope, self._db).get(Document, (self._owner, kind, id))
        return self._payload(scope, row) if row else None

    def list(
        self,
        scope: ReadScope,
        kind: str,
        *,
        parent_id: str | None = None,
        parent_is_null: bool = False,
        state: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        available_before: str | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("每页数量必须为 1 到 1000")
        if parent_is_null and parent_id is not None:
            raise ValueError("父标识和空父标识筛选不能同时使用")
        session = session_for(scope, self._db)
        query = select(Document).where(Document.owner == self._owner, Document.kind == kind)
        if parent_id is not None:
            query = query.where(Document.parent_id == parent_id)
        elif parent_is_null:
            query = query.where(Document.parent_id.is_(None))
        if state is not None:
            query = query.where(Document.state == state)
        if available_before is not None:
            query = query.where(Document.available_at <= normalize_time(available_before))
        if cursor:
            anchor = session.get(Document, (self._owner, kind, cursor))
            if anchor is None:
                raise ValueError("分页游标不存在")
            query = query.where(
                or_(
                    Document.recorded_at < anchor.recorded_at,
                    and_(Document.recorded_at == anchor.recorded_at, Document.id < anchor.id),
                )
            )
        return [
            self._payload(scope, row)
            for row in session.scalars(
                query.order_by(Document.recorded_at.desc(), Document.id.desc()).limit(limit)
            )
        ]

    def insert(
        self,
        scope: UnitOfWork,
        kind: str,
        id: str,
        payload: dict,
        *,
        parent_id: str | None = None,
        state: str | None = None,
        available_at: str | None = None,
    ) -> dict:
        session = session_for(scope, self._db, write=True)
        if session.get(Document, (self._owner, kind, id)) is not None:
            poison(scope)
            raise DuplicateDocument()
        now = self._db.now()
        data = self._codec.encode_text(scope, canonical_json(payload))
        row = Document(
            owner=self._owner,
            kind=kind,
            id=id,
            payload=data,
            revision=1,
            parent_id=parent_id,
            state=state,
            recorded_at=now,
            available_at=normalize_time(available_at) if available_at else None,
        )
        session.add(row)
        session.flush()
        session.add(
            DocumentRevision(owner=self._owner, kind=kind, id=id, revision=1, payload=data, recorded_at=now)
        )
        session.flush()
        return self._payload(scope, row)

    def update(
        self,
        scope: UnitOfWork,
        kind: str,
        id: str,
        payload: dict,
        expected_revision: int,
        *,
        state: str | None = None,
        parent_id: str | None | object = _UNCHANGED,
    ) -> dict:
        session = session_for(scope, self._db, write=True)
        row = session.get(Document, (self._owner, kind, id))
        if row is None or row.revision != expected_revision:
            poison(scope)
            raise RevisionConflict(row.revision if row else None)
        if parent_id is not _UNCHANGED and parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("父标识必须为字符串或空值")
        data, now = self._codec.encode_text(scope, canonical_json(payload)), self._db.now()
        values = {"payload": data, "revision": expected_revision + 1, "recorded_at": now}
        if state is not None:
            values["state"] = state
        if parent_id is not _UNCHANGED:
            values["parent_id"] = parent_id
        changed = session.execute(
            update(Document)
            .where(
                Document.owner == self._owner,
                Document.kind == kind,
                Document.id == id,
                Document.revision == expected_revision,
            )
            .values(**values)
        )
        if changed.rowcount != 1:
            poison(scope)
            raise RevisionConflict(None)
        session.add(
            DocumentRevision(
                owner=self._owner,
                kind=kind,
                id=id,
                revision=expected_revision + 1,
                payload=data,
                recorded_at=now,
            )
        )
        session.flush()
        session.refresh(row)
        return self._payload(scope, row)

    def history(self, scope: ReadScope, kind: str, id: str) -> list[dict]:
        query = (
            select(DocumentRevision)
            .where(
                DocumentRevision.owner == self._owner,
                DocumentRevision.kind == kind,
                DocumentRevision.id == id,
            )
            .order_by(DocumentRevision.revision)
        )
        return [self._payload(scope, row) for row in session_for(scope, self._db).scalars(query)]
