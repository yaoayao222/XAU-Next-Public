"""Automatic read-only collection through the real job/HTTP/SQLite seams."""

import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from xau_next.bootstrap import create_app
from xau_next.domains.market.bridge import BridgeError
from xau_next.shared.values import parse_utc

TOKEN = "auto-refresh-integration-only"
COMMAND = {"include_history": False, "history_from": None, "history_to": None}


class Clock:
    elapsed = 0

    def __call__(self):
        return (
            (parse_utc("2026-10-12T02:00:00Z") + timedelta(seconds=self.elapsed))
            .isoformat()
            .replace("+00:00", "Z")
        )


def app_at(path, clock, *, worker=False):
    app = create_app(data_root=path, token=TOKEN, source="synthetic", clock=clock, start_worker=worker)
    app.state.workflows.monotonic = lambda: clock.elapsed
    return app


def client_for(app):
    return TestClient(app, headers={"Authorization": "Bearer " + TOKEN})


def workspace(client):
    result = client.get("/v1/workspace")
    assert result.status_code == 200, result.text
    return result.json()


def refresh(client, key):
    result = client.post("/v1/market/refresh", json=COMMAND, headers={"Idempotency-Key": key})
    assert result.status_code == 202, result.text
    return result.json()


def eventually(read, accepts, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if accepts(value):
            return value
        time.sleep(0.05)
    raise AssertionError("Automatic worker did not produce the expected public result")


def test_ac02_19_startup_and_periodic_collection_continue_during_slow_review(tmp_path):
    clock = Clock()
    app = app_at(tmp_path, clock, worker=True)
    wf = app.state.workflows
    entered, release = threading.Event(), threading.Event()

    def slow_review(lease):
        entered.set()
        assert release.wait(10)
        with wf.db.write() as tx:
            wf.jobs.finish(tx, lease, None)

    wf.run_reviews = slow_review
    with client_for(app) as client:
        try:
            first = eventually(lambda: workspace(client), lambda w: w["market"] is not None)
            assert first["fact_refresh"]["enabled"]
            assert first["portfolio"]["account_status"]["state"] == "available"
            assert first["portfolio"]["orders_status"]["state"] == "available"
            assert first["portfolio"]["trades_status"]["state"] == "available"
            with wf.db.write() as tx:
                wf.jobs.enqueue(tx, "review_refresh", {"forecast_id": None, "through": clock()})
            assert entered.wait(5)
            clock.elapsed += 6
            second = eventually(
                lambda: workspace(client),
                lambda w: w["market"]["snapshot_id"] != first["market"]["snapshot_id"],
            )
            assert second["portfolio"]["snapshot_id"] != first["portfolio"]["snapshot_id"]
            assert second["market"]["captured_at"] > first["market"]["captured_at"]
            assert not release.is_set()  # a pending CPU job did not stop current facts
        finally:
            release.set()
    assert not wf._refresh_thread.is_alive()
    assert not wf._thread.is_alive()


def test_ac06_19_manual_and_automatic_requests_share_inflight_capture(tmp_path):
    clock = Clock()
    app = app_at(tmp_path, clock, worker=True)
    wf = app.state.workflows
    entered, release = threading.Event(), threading.Event()
    capture = wf.market.capture
    calls = []

    def delayed_capture(*args):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return capture(*args)

    wf.market.capture = delayed_capture
    with client_for(app) as client:
        try:
            assert entered.wait(5)
            auto = workspace(client)["fact_refresh"]["latest_job"]
            one, two = refresh(client, "manual-one"), refresh(client, "manual-two")
            assert auto["job_id"] == one["job_id"] == two["job_id"]
            assert one["state"] == "running"
            release.set()
            eventually(lambda: workspace(client), lambda w: w["market"] is not None)
            assert len(calls) == 1
        finally:
            release.set()


def test_ac13_19_failure_keeps_facts_backs_off_and_manual_refresh_can_recover(tmp_path):
    clock = Clock()
    app = app_at(tmp_path, clock)
    wf = app.state.workflows
    with client_for(app) as client:
        wf.schedule_market_refresh()
        assert wf.process_once(kind="market_refresh")
        first = workspace(client)
        capture = wf.market.capture

        def unavailable(*_args):
            raise BridgeError("TERMINAL_DISCONNECTED", "只读终端暂时未连接")

        wf.market.capture = unavailable
        clock.elapsed = 6
        wf.schedule_market_refresh()
        assert wf.process_once(kind="market_refresh")
        failed = workspace(client)
        assert failed["market"]["snapshot_id"] == first["market"]["snapshot_id"]
        assert failed["portfolio"] == first["portfolio"]
        assert failed["fact_refresh"]["latest_job"]["error"]["code"] == "TERMINAL_DISCONNECTED"
        clock.elapsed = 35
        assert wf.schedule_market_refresh() is None
        wf.market.capture = capture
        manual = refresh(client, "immediate-during-backoff")
        assert wf.process_once(kind="market_refresh")
        recovered = workspace(client)
        assert recovered["market"]["snapshot_id"] != first["market"]["snapshot_id"]
        assert recovered["fact_refresh"]["latest_job"]["job_id"] == manual["job_id"]
        assert recovered["fact_refresh"]["latest_job"]["state"] == "succeeded"
        clock.elapsed = 39
        assert wf.schedule_market_refresh() is None
        clock.elapsed = 40
        assert wf.schedule_market_refresh()["state"] == "queued"


@pytest.mark.parametrize("running", [False, True])
def test_ac09_startup_recovers_existing_capture_without_duplicate_queue(tmp_path, running):
    clock = Clock()
    app = app_at(tmp_path, clock)
    wf = app.state.workflows
    with client_for(app):
        pending = wf.schedule_market_refresh()
        if running:
            assert wf.jobs.claim("interrupted", kind="market_refresh")
    clock.elapsed = 31
    restarted = app_at(tmp_path, clock, worker=True)
    with client_for(restarted) as client:
        restored = eventually(lambda: workspace(client), lambda w: w["market"] is not None)
        assert restored["fact_refresh"]["latest_job"]["job_id"] == pending["job_id"]
        with restarted.state.workflows.db.read() as tx:
            jobs = restarted.state.workflows.jobs.list(tx)
        assert len([j for j in jobs if j["kind"] == "market_refresh"]) == 1


def test_ac19_failed_startup_recovers_automatically_after_retry_interval(tmp_path):
    clock = Clock()
    app = app_at(tmp_path, clock)
    wf = app.state.workflows
    capture = wf.market.capture

    def unavailable(*_args):
        raise BridgeError("TERMINAL_DISCONNECTED", "只读终端暂时未连接")

    with client_for(app) as client:
        wf.market.capture = unavailable
        failed_id = wf.schedule_market_refresh()["job_id"]
        wf.process_once(kind="market_refresh")
        assert workspace(client)["market"] is None
        wf.market.capture = capture
        clock.elapsed = 29
        assert wf.schedule_market_refresh() is None
        clock.elapsed = 30
        retry = wf.schedule_market_refresh()
        assert retry["job_id"] != failed_id
        wf.process_once(kind="market_refresh")
        assert workspace(client)["market"] is not None
        with wf.db.read() as tx:
            assert wf.jobs.get(tx, failed_id)["state"] == "failed"
            assert wf.jobs.get(tx, retry["job_id"])["state"] == "succeeded"


def test_ac13_changed_source_settings_do_not_commit_old_capture(tmp_path):
    clock = Clock()
    app = app_at(tmp_path, clock)
    wf = app.state.workflows
    capture = wf.market.capture

    def changed_source(*args):
        prepared = capture(*args)
        settings = wf.settings.get_snapshot().to_dict()
        settings["values"]["connection"]["history_days"] = 29
        wf.settings.update_settings({"expected_revision": settings["revision"], "values": settings["values"]})
        return prepared

    wf.market.capture = changed_source
    with client_for(app) as client:
        wf.schedule_market_refresh()
        wf.process_once(kind="market_refresh")
        result = workspace(client)
        assert result["market"] is None
        assert result["fact_refresh"]["latest_job"]["error"]["code"] == "SOURCE_SETTINGS_CHANGED"
