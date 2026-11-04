from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy import delete, func, select
from xau_next.platform.db import Database, ReadScope, UnitOfWork
from xau_next.platform.db.core import canonical_json, normalize_time, session_for
from xau_next.shared.values import parse_utc

from .models import EventRetention, EventRow

RESOURCE_KINDS = frozenset(
    {
        "market_snapshot",
        "analysis_request",
        "forecast",
        "plan",
        "evaluation",
        "portfolio",
        "review",
        "settings",
        "experiment",
        "notification",
        "action_advice",
        "research_bundle",
        "analysis_comparison",
    }
)


def validate_resource(resource: dict | None) -> None:
    if resource is not None and (
        set(resource) != {"kind", "id"}
        or resource["kind"] not in RESOURCE_KINDS
        or not isinstance(resource["id"], str)
        or not resource["id"]
    ):
        raise ValueError("无效资源引用")


def _wire(row: EventRow) -> dict:
    return {
        "id": row.id,
        "type": row.type,
        "occurred_at": row.occurred_at,
        "resource": {"kind": row.resource_kind, "id": row.resource_id} if row.resource_kind else None,
        "job_id": row.job_id,
        "revision": row.revision,
    }


def format_sse(event: dict) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {canonical_json(event)}\n\n"


class EventStore:
    def __init__(self, db: Database):
        self._db = db

    def append(
        self,
        scope: UnitOfWork,
        type: str,
        *,
        resource: dict | None = None,
        job_id: str | None = None,
        revision: int = 0,
    ) -> dict:
        if type not in {"resource.changed", "job.changed"} or revision < 0:
            raise ValueError("无效事件类型或版本")
        validate_resource(resource)
        session = session_for(scope, self._db, write=True)
        row = EventRow(
            type=type,
            occurred_at=self._db.now(),
            resource_kind=resource["kind"] if resource else None,
            resource_id=resource["id"] if resource else None,
            job_id=job_id,
            revision=revision,
        )
        session.add(row)
        session.flush()
        return _wire(row)

    def cursor(self, scope: ReadScope) -> int:
        session = session_for(scope, self._db)
        maximum = session.scalar(select(func.max(EventRow.id))) or 0
        floor = session.get(EventRetention, 1)
        return max(maximum, floor.floor if floor else 0)

    def replay(self, after: int = 0, *, last_event_id: str | None = None, limit: int = 200) -> list[dict]:
        if last_event_id is not None:
            if re.fullmatch(r"[0-9]+", last_event_id) is None:
                raise ValueError("Last-Event-ID 必须为非负整数")
            after = int(last_event_id)
        if type(after) is not int or after < 0 or not 1 <= limit <= 1000:
            raise ValueError("无效事件游标或批量大小")
        with self._db.read() as scope:
            session = session_for(scope, self._db)
            floor = session.get(EventRetention, 1)
            cursor = self.cursor(scope)
            if (floor and after < floor.floor) or after > cursor:
                return [
                    {
                        "id": max(cursor, 1),
                        "type": "resync.required",
                        "occurred_at": self._db.now(),
                        "resource": None,
                        "job_id": None,
                        "revision": 0,
                    }
                ]
            rows = session.scalars(
                select(EventRow).where(EventRow.id > after).order_by(EventRow.id).limit(limit)
            )
            return [_wire(row) for row in rows]

    def prune(self, before: str | None = None) -> int:
        boundary = normalize_time(
            (parse_utc(self._db.now()) - timedelta(days=7)).isoformat().replace("+00:00", "Z")
        )
        cutoff = min(normalize_time(before), boundary) if before else boundary
        with self._db.write() as scope:
            session = session_for(scope, self._db, write=True)
            last_removed = session.scalar(select(func.max(EventRow.id)).where(EventRow.occurred_at < cutoff))
            if last_removed is None:
                return 0
            count = session.execute(delete(EventRow).where(EventRow.occurred_at < cutoff)).rowcount
            state = session.get(EventRetention, 1)
            if state is None:
                session.add(EventRetention(id=1, floor=last_removed))
            else:
                state.floor = max(state.floor, last_removed)
            return count

    format_sse = staticmethod(format_sse)
