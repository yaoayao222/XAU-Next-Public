from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete
from xau_next.platform.db import Database, UnitOfWork
from xau_next.platform.db.core import canonical_json, normalize_time, session_for
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

from .models import IdempotencyRow


class IdempotencyConflict(DomainError):
    def __init__(self):
        super().__init__("IDEMPOTENCY_CONFLICT", "同一幂等键对应的请求内容已经改变", 409)


@dataclass(frozen=True)
class IdempotentResult:
    status: int
    body: dict
    replayed: bool


def input_hash(body) -> str:
    payload = body if isinstance(body, bytes) else canonical_json(body).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class IdempotencyStore:
    def __init__(self, db: Database):
        self._db = db

    def execute(
        self, method: str, path: str, key: str, body, operation: Callable[[UnitOfWork], tuple[int, dict]]
    ) -> IdempotentResult:
        if not isinstance(key, str) or not key or len(key) > 120:
            raise ValueError("必须提供有效幂等键")
        method, digest = method.upper(), input_hash(body)
        with self._db.write() as scope:
            session = session_for(scope, self._db, write=True)
            existing = session.get(IdempotencyRow, (method, path, key))
            if existing:
                if existing.input_hash != digest:
                    raise IdempotencyConflict()
                return IdempotentResult(existing.status, json.loads(existing.response_json), True)
            status, response = operation(scope)
            data = canonical_json(response)
            session.add(
                IdempotencyRow(
                    method=method,
                    path=path,
                    key=key,
                    input_hash=digest,
                    status=status,
                    response_json=data,
                    created_at=self._db.now(),
                )
            )
            return IdempotentResult(status, json.loads(data), False)

    def prune(self, before: str | None = None) -> int:
        boundary = normalize_time(
            (parse_utc(self._db.now()) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        )
        cutoff = min(normalize_time(before), boundary) if before else boundary
        with self._db.write() as scope:
            return (
                session_for(scope, self._db, write=True)
                .execute(delete(IdempotencyRow).where(IdempotencyRow.created_at < cutoff))
                .rowcount
            )
