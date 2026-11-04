"""SYNTHETIC: durable per-item reviews, recovery and no unnecessary history reads."""
import json
from datetime import timedelta

from fastapi.testclient import TestClient
from test_flow import Clock, TOKEN, get, post, refresh
from test_reviews import minute_answer
from xau_next.bootstrap import create_app
from xau_next.shared.values import parse_utc


def seed(tmp_path):
    clock = Clock()
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    client = TestClient(app)
    client.__enter__()
    market = refresh(client, app)
    req = post(client, "/v1/analysis/requests", {
        "kind": "new", "market_snapshot_id": market["snapshot_id"], "forecast_id": None,
        "base_version_id": None, "horizon_minutes": [1]}, "seed").json()
    clock.value = "2026-10-12T02:00:01Z"
    post(client, "/v1/analysis/imports", {"request_id": req["request_id"],
        "raw_text": minute_answer(req), "source": "manual_file", "provider": "SYNTHETIC", "model_id": None}, "import")
    assert app.state.workflows.process_once()
    clock.value = "2026-10-12T02:04:00Z"
    return app, client, clock


def run(client, wf, clock, key):
    response = post(client, "/v1/reviews/refresh", {"forecast_id": None, "through": clock()}, key)
    assert response.status_code == 202, response.text
    assert wf.process_once()
    return get(client, "/v1/jobs/" + response.json()["job_id"]).json()


def test_completed_items_skip_history_reads_and_duplicate_revisions(tmp_path, monkeypatch):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        assert run(client, wf, clock, "first")["state"] == "succeeded"
        before = get(client, "/v1/reviews").json()["items"]
        monkeypatch.setattr(wf.market, "capture", lambda *a, **k: (_ for _ in ()).throw(AssertionError("completed item reread")))
        job = run(client, wf, clock, "second")
        assert job["state"] == "succeeded", job
        assert "处理 0 项" in job["message"]
        assert get(client, "/v1/reviews").json()["items"] == before
    finally:
        client.__exit__(None, None, None)


def test_detail_diagnosis_uses_saved_evidence_without_capture_or_revision(tmp_path, monkeypatch):
    app,client,clock=seed(tmp_path)
    try:
        wf=app.state.workflows
        assert run(client,wf,clock,'detail-seed')['state']=='succeeded'
        rows=get(client,'/v1/reviews').json()['items']
        original=next(r for r in rows if r['target_kind']=='forecast')
        monkeypatch.setattr(wf.market,'capture',lambda *a,**k: (_ for _ in ()).throw(AssertionError('detail recaptured market')))
        detail=get(client,f"/v1/reviews/{original['review_id']}?revision={original['revision']}").json()
        assert detail['diagnostics']['method_version']=='review-diagnostics/1'
        assert {k:v for k,v in detail.items() if k!='diagnostics'}==original
        assert wf.review.get_review(original['review_id']).to_dict()==original
        assert get(client,'/v1/reviews').json()['items']==rows
    finally:
        client.__exit__(None,None,None)


def test_queued_full_scan_prevents_duplicate_automatic_forecast_jobs(tmp_path):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        response = post(client, "/v1/reviews/refresh", {"forecast_id": None, "through": clock()}, "one-full-scan")
        assert response.status_code == 202
        assert wf.enqueue_due_reviews() == 0
        with wf.db.read() as scope:
            assert wf.jobs.active_review(scope)['job_id'] == response.json()['job_id']
    finally:
        client.__exit__(None, None, None)


def test_specific_forecast_review_does_not_traverse_unrelated_trades(tmp_path, monkeypatch):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        forecast = wf.analysis.list_forecasts().to_dict()['items'][0]['forecast_id']
        monkeypatch.setattr(wf.portfolio, 'list_trades', lambda **kw: (_ for _ in ()).throw(AssertionError('unrelated trade scan')))
        response = post(client, '/v1/reviews/refresh', {'forecast_id': forecast, 'through': clock()}, 'forecast-only')
        assert response.status_code == 202
        assert wf.process_once()
        assert get(client, '/v1/jobs/' + response.json()['job_id']).json()['state'] == 'succeeded'
    finally:
        client.__exit__(None, None, None)


def test_checkpoint_survives_later_failure_and_recovery_skips_finished_item(tmp_path, monkeypatch):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        original = wf._compute
        seen = []
        def fail_second(function, *args, **kwargs):
            seen.append(function.__name__)
            if len(seen) == 2:
                raise RuntimeError("SYNTHETIC interruption after first checkpoint")
            return original(function, *args, **kwargs)
        monkeypatch.setattr(wf, "_compute", fail_second)
        assert run(client, wf, clock, "interrupted")["state"] == "failed"
        saved = get(client, "/v1/reviews").json()["items"]
        assert len(saved) == 1 and saved[0]["target_kind"] == "forecast"
        resumed = []
        def resume(function, *args, **kwargs):
            resumed.append(function.__name__)
            return original(function, *args, **kwargs)
        monkeypatch.setattr(wf, "_compute", resume)
        assert run(client, wf, clock, "recovered")["state"] == "succeeded"
        assert resumed == ["compute_reference"]
        assert next(r for r in get(client, "/v1/reviews").json()["items"]
                    if r["target_kind"] == "forecast") == saved[0]
    finally:
        client.__exit__(None, None, None)


def test_total_runtime_over_lease_keeps_per_item_renewal(tmp_path, monkeypatch):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        original = wf._compute
        def delayed(function, *args, **kwargs):
            # Two independently committed items take 40 logical seconds in total.
            clock.value = (parse_utc(clock()) + timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
            return original(function, *args, **kwargs)
        monkeypatch.setattr(wf, "_compute", delayed)
        job = run(client, wf, clock, "long-total")
        assert job["state"] == "succeeded", job
        assert len(get(client, "/v1/reviews").json()["items"]) == 2
    finally:
        client.__exit__(None, None, None)


def test_missing_history_does_not_loop_after_time_restart_or_manual_rescan(tmp_path, monkeypatch):
    app, client, clock = seed(tmp_path)
    try:
        wf = app.state.workflows
        request = wf.market._bridge.request
        def missing_history(operation, params):
            result = request(operation, params)
            if operation == "history":
                result["result"]["bars"] = []
            return result
        monkeypatch.setattr(wf.market._bridge, "request", missing_history)
        assert run(client, wf, clock, "missing-first")["state"] == "succeeded"
        before = get(client, "/v1/reviews").json()["items"]
        original = next(r for r in before if r["target_kind"] == "forecast")
        assert original["outcome"] == "DATA_INCOMPLETE"
        clock.value = "2026-10-12T03:04:00Z"
        assert wf.enqueue_due_reviews() == 0
        monkeypatch.setattr(wf.market, "capture", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unchanged missing evidence reread")))
        assert "处理 0 项" in run(client, wf, clock, "missing-again")["message"]
        assert get(client, "/v1/reviews").json()["items"] == before
    finally:
        client.__exit__(None, None, None)
    # The policy is persisted, not a process-local cooldown.
    reopened = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(reopened) as again:
        wf = reopened.state.workflows
        assert wf.enqueue_due_reviews() == 0
        bounds = wf._review_bounds(original["window_start"], original["window_end"])
        captured = wf.market.capture({"include_history": True, "history_from": bounds[0],
                                     "history_to": bounds[1]}, wf.settings.get_snapshot())
        with wf.db.write() as tx:
            wf.market.commit_capture(captured, tx)
        assert wf.enqueue_due_reviews() == 1  # Same historical window now has genuinely changed facts.
        assert wf.process_once()
        improved = next(r for r in get(again, "/v1/reviews").json()["items"] if r["target_kind"] == "forecast")
        assert improved["outcome"] == "EVALUATED" and improved["revision"] == original["revision"] + 1
        # HTTP adds a dated read-only diagnostic; the saved revision remains byte-for-byte business data.
        assert wf.review.get_review(original["review_id"], revision=original["revision"]).to_dict() == {
            k:v for k,v in original.items() if k!='diagnostics'}
        assert wf.enqueue_due_reviews() == 0
