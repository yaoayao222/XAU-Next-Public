"""Durable deduplicated delivery. Business validity is rechecked by the caller before send."""

import hashlib
import json
import logging

from sqlalchemy import select
from xau_next.platform.db.core import canonical_json, normalize_time, session_for
from xau_next.shared.values import parse_utc

from .models import NotificationAttempt, NotificationRow
from .windows import WindowsNotifier


def wire(row):
    payload = json.loads(row.payload)
    return {"notification_id": row.id, **{k: payload[k] for k in ("kind", "resource_id", "title", "body")},
            "created_at": row.created_at, "expires_at": row.expires_at, "state": row.state,
            "submitted_at": row.submitted_at, "detail": row.detail}


class NotificationStore:
    def __init__(self, db, *, sink=None, allow_desktop=False):
        self.db, self.sink, self.allow_desktop = db, sink or WindowsNotifier(), allow_desktop

    def contains(self, scope, key):
        return session_for(scope, self.db).get(NotificationRow, hashlib.sha256(key.encode("utf-8")).hexdigest()) is not None

    def enqueue(self, uow, key, scope_key, payload, *, expires_at):
        session = session_for(uow, self.db, write=True)
        id = hashlib.sha256(key.encode("utf-8")).hexdigest()
        existing = session.get(NotificationRow, id)
        if existing:
            return wire(existing), False
        row = NotificationRow(id=id, dedup_key=key, scope_key=scope_key, payload=canonical_json(payload),
                              created_at=self.db.now(), expires_at=normalize_time(expires_at), state="queued")
        session.add(row)
        session.flush()
        session.add(NotificationAttempt(notification_id=id, at=self.db.now(), state="queued", detail=None))
        return wire(row), True

    def list(self, scope, *, scope_key, limit=30):
        rows = session_for(scope, self.db).scalars(select(NotificationRow).where(
            NotificationRow.scope_key == scope_key).order_by(NotificationRow.created_at.desc(), NotificationRow.id).limit(limit))
        return [wire(row) for row in rows]

    def recover(self):
        with self.db.write() as uow:
            session = session_for(uow, self.db, write=True)
            for row in session.scalars(select(NotificationRow).where(NotificationRow.state == "sending")):
                self._finish(session, row, "unknown", "上次发送被中断，是否显示未知；不自动重发。")

    def _finish(self, session, row, state, detail):
        row.state, row.detail = state, detail
        if state == "submitted":
            row.submitted_at = self.db.now()
        session.add(NotificationAttempt(notification_id=row.id, at=self.db.now(), state=state, detail=detail))

    def deliver_one(self, validate):
        with self.db.write() as uow:
            session = session_for(uow, self.db, write=True)
            row = session.scalar(select(NotificationRow).where(NotificationRow.state == "queued").order_by(
                NotificationRow.created_at, NotificationRow.id).limit(1))
            if row is None:
                return None
            if parse_utc(row.expires_at) <= parse_utc(self.db.now()):
                self._finish(session, row, "suppressed", "通知已过期，不追发。")
                return wire(row)
            self._finish(session, row, "sending", None)
            envelope = {**wire(row), "scope_key": row.scope_key, "context": json.loads(row.payload).get("context", {})}
        # Neither a slow native call nor a business snapshot read holds the writer.
        try:
            valid, reason = validate(envelope)
            if not valid:
                state, detail = "suppressed", reason
            elif not self.allow_desktop:
                state, detail = "suppressed", "合成或隔离环境未启用桌面投递；通知内容已保存。"
            else:
                state, detail = self.sink.send(envelope)
        except Exception as error:
            logging.getLogger(__name__).exception("Local notification delivery failed")
            state, detail = "unknown", "通知处理异常，投递结果未知：" + str(error)[:500]
        with self.db.write() as uow:
            session = session_for(uow, self.db, write=True)
            row = session.get(NotificationRow, envelope["notification_id"])
            self._finish(session, row, state, detail)
            return wire(row)
