from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from xau_next.platform.db import Database
from xau_next.platform.events import EventStore
from xau_next.platform.idempotency import IdempotencyConflict, IdempotencyStore
from xau_next.platform.jobs import JobStore, LeaseLost


@pytest.fixture
def platform(tmp_path):
    time = ["2026-10-12T02:00:00Z"]
    db = Database(tmp_path, clock=lambda: time[0])
    db.initialize()
    yield db, JobStore(db), time
    db.close()


def problem(retryable=True):
    return {
        "type": "about:blank",
        "title": "暂时不可用",
        "status": 503,
        "code": "TEMPORARY",
        "detail": "数据源暂时不可用",
        "trace_id": "test-trace",
        "field_errors": [],
        "retryable": retryable,
    }


def enqueue(db, jobs, key="one"):
    with db.write() as tx:
        return jobs.enqueue(tx, "answer_import", {"request_id": "r1"}, dedup_key=key)


def test_ac06_concurrent_idempotency_includes_business_job_events(platform):
    db, jobs, _ = platform
    idem = IdempotencyStore(db)

    def operation(tx):
        db.documents("analysis").insert(tx, "attempt", "one", {"raw": "answer"})
        return 202, jobs.enqueue(tx, "answer_import", {"request_id": "r1"})

    def call(_):
        return idem.execute("POST", "/v1/import", "same-key", {"raw": "answer"}, operation)

    with ThreadPoolExecutor() as pool:
        results = list(pool.map(call, range(2)))
    assert results[0].body == results[1].body
    assert sorted(r.replayed for r in results) == [False, True]
    with pytest.raises(IdempotencyConflict):
        idem.execute("POST", "/v1/import", "same-key", {"raw": "different"}, operation)
    with db.read() as scope:
        assert len(jobs.list(scope)) == 1
        assert EventStore(db).cursor(scope) == 1


def test_ac09_expired_worker_cannot_commit_any_business_result(platform):
    db, jobs, time = platform
    job = enqueue(db, jobs)
    lease_old = jobs.claim("old", lease_seconds=30)
    assert jobs.claim("other") is None
    time[0] = "2026-10-12T02:00:30Z"
    lease_new = jobs.claim("new")
    assert lease_new.attempt == 2 and lease_new.token != lease_old.token
    with pytest.raises(LeaseLost), db.write() as tx:
        db.documents("analysis").insert(tx, "version", "stale", {})
        jobs.finish(tx, lease_old, {"kind": "forecast", "id": "f1"})
    with db.read() as scope:
        assert db.documents("analysis").get(scope, "version", "stale") is None
    with db.write() as tx:
        db.documents("analysis").insert(tx, "version", "v1", {})
        jobs.finish(tx, lease_new, {"kind": "forecast", "id": "f1"})
    # Simulate response loss: querying and re-enqueueing the same work returns the committed job.
    assert enqueue(db, jobs)["job_id"] == job["job_id"]
    assert jobs.claim("after-response-loss") is None
    with db.read() as scope:
        assert jobs.get(scope, job["job_id"])["state"] == "succeeded"


def test_ac09_uncommitted_compute_recovers_after_restart(platform, tmp_path):
    db, jobs, time = platform
    job = enqueue(db, jobs)
    first = jobs.claim("worker1")
    with pytest.raises(RuntimeError, match="simulated"), db.write() as tx:
        db.documents("analysis").insert(tx, "version", "not-committed", {})
        jobs.finish(tx, first, {"kind": "forecast", "id": "f1"})
        raise RuntimeError("simulated before commit crash")
    db.close()
    recovered = Database(tmp_path, clock=lambda: time[0])
    recovered.initialize()
    jobs2 = JobStore(recovered)
    assert jobs2.claim("worker2") is None
    time[0] = "2026-10-12T02:00:31Z"
    lease = jobs2.claim("worker2")
    assert lease.job_id == job["job_id"]
    with recovered.read() as scope:
        assert recovered.documents("analysis").get(scope, "version", "not-committed") is None
    with recovered.write() as tx:
        jobs2.finish(tx, lease, {"kind": "forecast", "id": "f1"})
    recovered.close()


def test_ac09_cancellation_retry_and_max_attempts(platform):
    db, jobs, _ = platform
    queued = enqueue(db, jobs, "queued")
    assert jobs.cancel(queued["job_id"])["state"] == "cancelled"
    running = enqueue(db, jobs, "running")
    lease = jobs.claim("worker")
    assert jobs.cancel(running["job_id"])["state"] == "cancelling"
    assert jobs.heartbeat(lease)["state"] == "cancelling"
    with pytest.raises(LeaseLost), db.write() as tx:
        jobs.finish(tx, lease, {"kind": "forecast", "id": "f"})
    assert jobs.acknowledge_cancel(lease)["state"] == "cancelled"
    retry_job = enqueue(db, jobs, "retry")
    for attempt in range(1, 4):
        lease = jobs.claim("worker")
        assert lease.attempt == attempt
        assert jobs.fail(lease, problem())["state"] == "failed"
        if attempt < 3:
            assert jobs.retry(retry_job["job_id"])["state"] == "queued"
    with pytest.raises(Exception, match="不能自动重试"):
        jobs.retry(retry_job["job_id"])
    assert jobs.claim("worker") is None


def test_ac09_lease_expiry_without_reclaim_still_rejects_commit(platform):
    db, jobs, time = platform
    enqueue(db, jobs)
    lease = jobs.claim("one")
    time[0] = "2026-10-12T02:00:30Z"
    with pytest.raises(LeaseLost), db.write() as tx:
        jobs.finish(tx, lease, None)


def test_ac06_idempotency_failure_does_not_leave_partial_result(platform):
    db, _, _ = platform
    idem = IdempotencyStore(db)

    def fail(tx):
        db.documents("analysis").insert(tx, "attempt", "one", {})
        raise RuntimeError("failure before idempotency result")

    with pytest.raises(RuntimeError):
        idem.execute("POST", "/test", "k", {}, fail)

    def success(tx):
        return 201, db.documents("analysis").insert(tx, "attempt", "one", {})

    assert not idem.execute("POST", "/test", "k", {}, success).replayed


def test_ac09_failure_attempt_and_job_state_commit_atomically(platform):
    db, jobs, _ = platform
    job = enqueue(db, jobs)
    lease = jobs.claim("worker")
    with pytest.raises(RuntimeError, match="interrupted"), db.write() as tx:
        db.documents("analysis").insert(tx, "failed_attempt", "a", {"raw_id": "raw"})
        jobs.fail(lease, problem(False), scope=tx)
        raise RuntimeError("interrupted")
    with db.read() as scope:
        assert db.documents("analysis").get(scope, "failed_attempt", "a") is None
        assert jobs.get(scope, job["job_id"])["state"] == "running"
    with db.write() as tx:
        db.documents("analysis").insert(tx, "failed_attempt", "a", {"raw_id": "raw"})
        jobs.fail(lease, problem(False), scope=tx)
    with db.read() as scope:
        assert jobs.get(scope, job["job_id"])["state"] == "failed"


def test_concurrent_same_refresh_coalesces_without_duplicate_events(platform):
    db, jobs, _ = platform
    barrier = Barrier(8)

    def call(index):
        # Object ordering must not change the canonical request identity.
        body = {"include_history": False, "history_from": None, "history_to": None}
        if index % 2:
            body = dict(reversed(list(body.items())))
        barrier.wait(timeout=5)
        with db.write() as tx:
            return jobs.enqueue(tx, "market_refresh", body, coalesce_active=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))
    assert len({job["job_id"] for job in results}) == 1
    assert all(job["revision"] == 1 for job in results)
    with db.read() as scope:
        assert len(jobs.list(scope)) == 1
        assert EventStore(db).cursor(scope) == 1


def test_running_refresh_is_reused_without_resetting_lease_or_progress(platform):
    db, jobs, _ = platform
    with db.write() as tx:
        job = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    lease = jobs.claim("reader", kind="market_refresh")
    running = jobs.heartbeat(lease, progress=0.5, message="采集中")
    with db.write() as tx:
        reused = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    assert reused == running
    assert jobs.claim("other-reader", kind="market_refresh") is None
    with db.write() as tx:
        assert jobs.finish(tx, lease, None)["job_id"] == job["job_id"]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_refresh_does_not_coalesce(platform, terminal):
    db, jobs, _ = platform
    with db.write() as tx:
        original = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    lease = jobs.claim("reader", kind="market_refresh")
    if terminal == "succeeded":
        with db.write() as tx:
            jobs.finish(tx, lease, None)
    elif terminal == "failed":
        jobs.fail(lease, problem())
    else:
        jobs.cancel(original["job_id"])
        jobs.acknowledge_cancel(lease)
    with db.write() as tx:
        new = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    assert new["job_id"] != original["job_id"]
    assert new["state"] == "queued"
    with db.read() as scope:
        assert jobs.get(scope, original["job_id"])["state"] == terminal


def test_cancelling_refresh_does_not_capture_new_refresh(platform):
    db, jobs, _ = platform
    with db.write() as tx:
        original = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    lease = jobs.claim("reader", kind="market_refresh")
    assert jobs.cancel(original["job_id"])["state"] == "cancelling"
    with db.write() as tx:
        new = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
        reused = jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
    assert new["job_id"] != original["job_id"]
    assert reused == new
    jobs.acknowledge_cancel(lease)
    assert jobs.claim("reader", kind="market_refresh").job_id == new["job_id"]


def test_history_range_and_job_kind_are_part_of_active_coalescing(platform):
    db, jobs, _ = platform
    requests = [
        ("market_refresh", {"include_history": False, "history_from": None, "history_to": None}),
        (
            "market_refresh",
            {
                "include_history": True,
                "history_from": "2026-10-11T00:00:00Z",
                "history_to": "2026-10-12T00:00:00Z",
            },
        ),
        (
            "market_refresh",
            {
                "include_history": True,
                "history_from": "2026-10-10T00:00:00Z",
                "history_to": "2026-10-12T00:00:00Z",
            },
        ),
        ("review_refresh", {"include_history": False, "history_from": None, "history_to": None}),
    ]
    with db.write() as tx:
        jobs_created = [jobs.enqueue(tx, kind, body, coalesce_active=True) for kind, body in requests]
    assert len({job["job_id"] for job in jobs_created}) == len(requests)
    with db.write() as tx:
        for (kind, body), original in zip(requests, jobs_created):
            assert jobs.enqueue(tx, kind, body, coalesce_active=True)["job_id"] == original["job_id"]


def test_active_coalescing_is_opt_in_and_rolls_back_with_transaction(platform):
    db, jobs, _ = platform
    with pytest.raises(RuntimeError, match="rollback"), db.write() as tx:
        jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)
        raise RuntimeError("rollback")
    with db.read() as scope:
        assert jobs.latest(scope, kind="market_refresh") is None
        assert EventStore(db).cursor(scope) == 0
    with db.write() as tx:
        first = jobs.enqueue(tx, "market_refresh", {})
        second = jobs.enqueue(tx, "market_refresh", {})
    assert first["job_id"] != second["job_id"]


def test_dedup_key_retains_permanent_replay_with_active_coalescing(platform):
    db, jobs, _ = platform
    with db.write() as tx:
        job = jobs.enqueue(tx, "market_refresh", {}, dedup_key="explicit", coalesce_active=True)
    jobs.cancel(job["job_id"])
    with db.write() as tx:
        assert (
            jobs.enqueue(tx, "market_refresh", {}, dedup_key="explicit", coalesce_active=True)["job_id"]
            == job["job_id"]
        )
    with pytest.raises(IdempotencyConflict), db.write() as tx:
        jobs.enqueue(tx, "market_refresh", {"changed": True}, dedup_key="explicit", coalesce_active=True)


def test_excluded_kind_is_not_claimed_or_recovered_after_lease_expiry(platform):
    db, jobs, time = platform
    with db.write() as tx:
        market = jobs.enqueue(tx, "market_refresh", {})
        review = jobs.enqueue(tx, "review_refresh", {})
    general = jobs.claim("general", exclude_kinds=("market_refresh",))
    assert general.job_id == review["job_id"]
    assert jobs.claim("general", exclude_kinds=("market_refresh",)) is None
    old = jobs.claim("reader", kind="market_refresh", lease_seconds=30)
    with db.write() as tx:
        jobs.finish(tx, general, None)
    time[0] = "2026-10-12T02:00:30Z"
    assert jobs.claim("general", exclude_kinds=("market_refresh",)) is None
    with db.read() as scope:
        assert jobs.get(scope, market["job_id"])["revision"] == 2
    # A new periodic enqueue retains the recoverable job rather than duplicating it.
    with db.write() as tx:
        assert jobs.enqueue(tx, "market_refresh", {}, coalesce_active=True)["job_id"] == market["job_id"]
    recovered = jobs.claim("reader-restarted", kind="market_refresh")
    assert recovered.job_id == old.job_id
    assert recovered.attempt == 2 and recovered.token != old.token
    with pytest.raises(LeaseLost), db.write() as tx:
        jobs.finish(tx, old, None)


def test_expired_cancelled_kind_only_cleaned_by_its_dedicated_worker(platform):
    db, jobs, time = platform
    with db.write() as tx:
        job = jobs.enqueue(tx, "market_refresh", {})
    jobs.claim("reader", kind="market_refresh", lease_seconds=1)
    jobs.cancel(job["job_id"])
    time[0] = "2026-10-12T02:00:01Z"
    assert jobs.claim("general", exclude_kinds=("market_refresh",)) is None
    with db.read() as scope:
        assert jobs.get(scope, job["job_id"])["state"] == "cancelling"
    assert jobs.claim("reader-restarted", kind="market_refresh") is None
    with db.read() as scope:
        assert jobs.get(scope, job["job_id"])["state"] == "cancelled"


def test_latest_is_kind_scoped_and_breaks_created_time_ties_by_job_id(platform, monkeypatch):
    db, jobs, time = platform
    ids = iter(("job-z", "job-a", "other-newer", "job-0"))
    monkeypatch.setattr("xau_next.platform.jobs.public.new_id", lambda: next(ids))
    with db.read() as scope:
        assert jobs.latest(scope, kind="market_refresh") is None
    with db.write() as tx:
        highest_id = jobs.enqueue(tx, "market_refresh", {})
        lower_id = jobs.enqueue(tx, "market_refresh", {})
    with db.read() as scope:
        assert jobs.latest(scope, kind="market_refresh")["job_id"] == highest_id["job_id"]
    time[0] = "2026-10-12T02:01:00Z"
    jobs.cancel(lower_id["job_id"])
    with db.write() as tx:
        jobs.enqueue(tx, "review_refresh", {})
    with db.read() as scope:
        assert jobs.latest(scope, kind="market_refresh")["job_id"] == highest_id["job_id"]
    time[0] = "2026-10-12T02:02:00Z"
    with db.write() as tx:
        newest = jobs.enqueue(tx, "market_refresh", {})
    with db.read() as scope:
        assert jobs.latest(scope, kind="market_refresh") == newest
