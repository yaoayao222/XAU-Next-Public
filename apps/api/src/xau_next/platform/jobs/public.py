from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select
from xau_next.platform.db import Database, ReadScope, UnitOfWork
from xau_next.platform.db.core import canonical_json, normalize_time, poison, session_for
from xau_next.platform.events.public import EventStore, validate_resource
from xau_next.platform.idempotency import IdempotencyConflict, input_hash
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id, parse_utc

from .models import JobRow

KINDS = frozenset({"market_refresh", "answer_import", "review_refresh", "experiment_replay", "model_call", "research_capture", "comparison_run", "comparison_score", "daily_model_call"})


class LeaseLost(DomainError):
    def __init__(self):
        super().__init__("JOB_LEASE_LOST", "任务租约已失效，当前执行者不能保存结果", 409)


@dataclass(frozen=True)
class Lease:
    job_id: str
    kind: str
    worker: str
    token: str
    input_data: dict
    attempt: int
    expires_at: str


def _wire(row: JobRow) -> dict:
    return {
        "job_id": row.job_id,
        "kind": row.kind,
        "state": row.state,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "progress": row.progress,
        "message": row.message,
        "result": json.loads(row.result_json) if row.result_json else None,
        "error": json.loads(row.error_json) if row.error_json else None,
        "revision": row.revision,
    }


def _problem(code: str, detail: str, retryable: bool = False) -> dict:
    return {
        "type": "about:blank",
        "title": "任务执行失败",
        "status": 500,
        "code": code,
        "detail": detail,
        "trace_id": new_id(),
        "field_errors": [],
        "retryable": retryable,
    }


class JobStore:
    def __init__(self, db: Database, events: EventStore | None = None):
        self._db, self._events = db, events or EventStore(db)

    def enqueue(
        self,
        scope: UnitOfWork,
        kind: str,
        input_data: dict,
        *,
        dedup_key: str | None = None,
        max_attempts: int = 3,
        coalesce_active: bool = False,
    ) -> dict:
        if kind not in KINDS or max_attempts < 1:
            raise ValueError("任务类型或最大尝试次数无效")
        session = session_for(scope, self._db, write=True)
        digest = input_hash(input_data)
        if dedup_key is not None:
            row = session.scalar(select(JobRow).where(JobRow.kind == kind, JobRow.dedup_key == dedup_key))
            if row:
                if row.input_hash != digest:
                    poison(scope)
                    raise IdempotencyConflict()
                return _wire(row)
        if coalesce_active:
            # BEGIN IMMEDIATE covers lookup and insertion, including other processes.
            # Cancelling work cannot satisfy a new request. Explicit dedup keys above
            # retain their permanent replay semantics independently of active reuse.
            row = session.scalar(
                select(JobRow)
                .where(
                    JobRow.kind == kind,
                    JobRow.input_hash == digest,
                    JobRow.state.in_(("queued", "running")),
                    JobRow.cancel_requested.is_(False),
                )
                .order_by(JobRow.created_at, JobRow.job_id)
                .limit(1)
            )
            if row:
                return _wire(row)
        now = self._db.now()
        row = JobRow(
            job_id=new_id(),
            kind=kind,
            input_json=canonical_json(input_data),
            input_hash=digest,
            dedup_key=dedup_key,
            state="queued",
            attempt=0,
            max_attempts=max_attempts,
            progress=None,
            message="等待处理",
            result_json=None,
            error_json=None,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            heartbeat_at=None,
            cancel_requested=False,
            created_at=now,
            updated_at=now,
            revision=1,
        )
        session.add(row)
        session.flush()
        self._event(scope, row)
        return _wire(row)

    def get(self, scope: ReadScope, job_id: str) -> dict | None:
        row = session_for(scope, self._db).get(JobRow, job_id)
        return _wire(row) if row else None

    def latest(self, scope: ReadScope, *, kind: str) -> dict | None:
        row = session_for(scope, self._db).scalar(
            select(JobRow)
            .where(JobRow.kind == kind)
            .order_by(JobRow.created_at.desc(), JobRow.job_id.desc())
            .limit(1)
        )
        return _wire(row) if row else None

    def active_review(self, scope: ReadScope, forecast_id: str | None = None) -> dict | None:
        """An existing full scan covers narrower scheduler work until its checkpoints commit."""
        rows = session_for(scope, self._db).scalars(select(JobRow).where(
            JobRow.kind == "review_refresh", JobRow.state.in_(("queued", "running")),
            JobRow.cancel_requested.is_(False)).order_by(JobRow.created_at))
        for row in rows:
            command = json.loads(row.input_json)
            if not command.get("actual_only") and (
                    command.get("forecast_id") is None or command.get("forecast_id") == forecast_id):
                return _wire(row)
        return None

    def list(self, scope: ReadScope, *, state: str | None = None, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("每页数量必须为 1 到 1000")
        query = select(JobRow)
        if state:
            query = query.where(JobRow.state == state)
        return [
            _wire(row)
            for row in session_for(scope, self._db).scalars(
                query.order_by(JobRow.created_at.desc(), JobRow.job_id.desc()).limit(limit)
            )
        ]

    def claim(
        self,
        worker: str,
        *,
        lease_seconds: float = 30,
        kind: str | None = None,
        exclude_kinds: tuple[str, ...] = (),
    ) -> Lease | None:
        if not worker or lease_seconds <= 0:
            raise ValueError("执行者和租约时长必须有效")
        with self._db.write() as scope:
            session = session_for(scope, self._db, write=True)
            now = self._db.now()
            if kind in {"model_call","comparison_run","daily_model_call"}:
                # Global model concurrency one, including other API processes and cancelling I/O.
                active = list(session.scalars(select(JobRow.job_id).where(JobRow.kind == kind,
                    JobRow.state.in_(("running", "cancelling")), JobRow.lease_expires_at > now).limit(4)))
                if len(active)>=(4 if kind=='daily_model_call' else 1):
                    return None
            query = select(JobRow).where(
                or_(
                    JobRow.state == "queued",
                    and_(JobRow.state.in_(["running", "cancelling"]), JobRow.lease_expires_at <= now),
                )
            )
            if kind:
                query = query.where(JobRow.kind == kind)
            else:
                # Model I/O has an explicit dedicated channel; ordinary workers cannot claim it.
                query = query.where(JobRow.kind.not_in(("model_call","comparison_run","daily_model_call")))
            if exclude_kinds:
                query = query.where(JobRow.kind.not_in(exclude_kinds))
            for row in session.scalars(query.order_by(JobRow.created_at, JobRow.job_id).limit(1000)):
                if row.cancel_requested:
                    row.state, row.message = "cancelled", "任务已取消"
                    self._clear_lease(row)
                    self._changed(scope, row)
                    continue
                if row.attempt >= row.max_attempts:
                    row.state, row.message = "failed", "任务恢复次数已用尽"
                    row.error_json = canonical_json(_problem("ATTEMPTS_EXHAUSTED", row.message))
                    self._clear_lease(row)
                    self._changed(scope, row)
                    continue
                row.state, row.message, row.progress = "running", "正在处理", None
                row.attempt += 1
                row.lease_owner, row.lease_token = worker, new_id()
                row.lease_expires_at, row.heartbeat_at = self._expiry(now, lease_seconds), now
                row.error_json = None
                self._changed(scope, row)
                return Lease(
                    row.job_id,
                    row.kind,
                    worker,
                    row.lease_token,
                    json.loads(row.input_json),
                    row.attempt,
                    row.lease_expires_at,
                )
            return None

    def heartbeat(
        self,
        lease: Lease,
        *,
        lease_seconds: float = 30,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict:
        if lease_seconds <= 0 or (progress is not None and not 0 <= progress <= 1):
            raise ValueError("租约或进度无效")
        if message is not None and not 1 <= len(message) <= 1000:
            raise ValueError("任务说明长度无效")
        with self._db.write() as scope:
            row = self._leased(scope, lease, allow_cancelling=True)
            now = self._db.now()
            row.heartbeat_at, row.lease_expires_at = now, self._expiry(now, lease_seconds)
            if progress is not None:
                row.progress = progress
            if message is not None:
                row.message = message
            self._changed(scope, row)
            return _wire(row)

    def finish(self, scope: UnitOfWork, lease: Lease, result: dict | None, *, message: str = "处理完成") -> dict:
        validate_resource(result)
        row = self._leased(scope, lease)
        row.state, row.progress, row.message = "succeeded", 1.0, message
        row.result_json = canonical_json(result) if result else None
        row.error_json = None
        self._clear_lease(row)
        self._changed(scope, row)
        return _wire(row)

    def assert_current(self, scope, lease, *, allow_cancelling=False):
        return _wire(self._leased(scope, lease, allow_cancelling=allow_cancelling))

    def inspect_lease(self, lease):
        """Cancellation polling never takes the single-writer coordinator."""
        with self._db.read() as scope:
            row = session_for(scope, self._db).get(JobRow, lease.job_id)
            if not self._is_current(row, lease, allow_cancelling=True):
                raise LeaseLost()
            return _wire(row)

    def fail(self, lease: Lease, problem: dict, *, scope: UnitOfWork | None = None) -> dict:
        # Domain and transport adapters supply the public Problem shape.
        if not isinstance(problem, dict) or not {"code", "detail", "retryable"}.issubset(problem):
            raise ValueError("任务失败必须包含规范错误原因")
        with self._db.write() if scope is None else nullcontext(scope) as tx:
            row = self._leased(tx, lease, allow_cancelling=True)
            if row.cancel_requested:
                row.state, row.message = "cancelled", "任务已取消"
            else:
                row.state, row.message = "failed", str(problem["detail"])[:1000]
                row.error_json = canonical_json(problem)
            self._clear_lease(row)
            self._changed(tx, row)
            return _wire(row)

    def retry(self, job_id: str, *, scope: UnitOfWork | None = None) -> dict:
        with self._db.write() if scope is None else nullcontext(scope) as tx:
            row = session_for(tx, self._db, write=True).get(JobRow, job_id)
            if row is None:
                raise KeyError(job_id)
            error = json.loads(row.error_json) if row.error_json else {}
            if row.state != "failed" or not error.get("retryable") or row.attempt >= row.max_attempts:
                raise DomainError("JOB_NOT_RETRYABLE", "该任务不能自动重试，请检查失败原因", 409)
            row.state, row.message, row.progress = "queued", "等待重试", None
            self._changed(tx, row)
            return _wire(row)

    def cancel(self, job_id: str, *, scope: UnitOfWork | None = None) -> dict:
        with self._db.write() if scope is None else nullcontext(scope) as tx:
            row = session_for(tx, self._db, write=True).get(JobRow, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.state in {"succeeded", "failed", "cancelled", "cancelling"}:
                return _wire(row)
            row.cancel_requested = True
            row.state = "cancelled" if row.state == "queued" else "cancelling"
            row.message = "任务已取消" if row.state == "cancelled" else "正在等待任务安全停止"
            if row.state == "cancelled":
                self._clear_lease(row)
            self._changed(tx, row)
            return _wire(row)

    def acknowledge_cancel(self, lease: Lease) -> dict:
        with self._db.write() as scope:
            row = self._leased(scope, lease, allow_cancelling=True)
            if not row.cancel_requested:
                raise DomainError("CANCEL_NOT_REQUESTED", "任务尚未请求取消", 409)
            row.state, row.message = "cancelled", "任务已取消"
            self._clear_lease(row)
            self._changed(scope, row)
            return _wire(row)

    def _leased(self, scope: UnitOfWork, lease: Lease, *, allow_cancelling: bool = False) -> JobRow:
        row = session_for(scope, self._db, write=True).get(JobRow, lease.job_id)
        if not self._is_current(row, lease, allow_cancelling=allow_cancelling):
            poison(scope)
            raise LeaseLost()
        return row

    def _is_current(self, row, lease, *, allow_cancelling):
        states = {"running", "cancelling"} if allow_cancelling else {"running"}
        return not (
            row is None
            or row.state not in states
            or row.lease_owner != lease.worker
            or row.lease_token != lease.token
            or row.lease_expires_at is None
            or row.lease_expires_at <= self._db.now()
            or (row.cancel_requested and not allow_cancelling)
        )

    def _changed(self, scope: UnitOfWork, row: JobRow) -> None:
        row.revision += 1
        row.updated_at = self._db.now()
        session_for(scope, self._db, write=True).flush()
        self._event(scope, row)

    def _event(self, scope: UnitOfWork, row: JobRow) -> None:
        self._events.append(scope, "job.changed", job_id=row.job_id, revision=row.revision)

    @staticmethod
    def _clear_lease(row: JobRow) -> None:
        row.lease_owner = row.lease_token = row.lease_expires_at = None

    @staticmethod
    def _expiry(now: str, seconds: float) -> str:
        return normalize_time(
            (parse_utc(now) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
        )
