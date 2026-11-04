import json

from fastapi.testclient import TestClient
from test_flow import ROOT, TOKEN, Clock, get, post, refresh
from xau_next.bootstrap import create_app


def minute_answer(req):
    raw = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        raw[key] = req[key]
    raw["horizons"] = [
        {
            **raw["horizons"][0],
            "horizon_minutes": 1,
            "end_at": req["window_end"],
            "phases": [
                {
                    "start_at": req["market_cutoff"],
                    "end_at": req["window_end"],
                    "direction": "uncertain",
                    "note": "合成一分钟复盘验收",
                }
            ],
        }
    ]
    raw["plans"][0].update(
        activation={"kind": "immediate"},
        valid_from=req["market_cutoff"],
        entry_deadline="2026-10-12T02:00:30Z",
        exit_deadline=req["window_end"],
        max_holding_minutes=1,
    )
    raw["evidence_refs"] = [req["market_snapshot_id"]]
    return json.dumps(raw, ensure_ascii=False)


def test_ac09_14_17_due_processpool_review_and_experience_after_restart(tmp_path):
    clock = Clock()
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        market = refresh(client, app)
        response = post(
            client,
            "/v1/analysis/requests",
            {
                "kind": "new",
                "market_snapshot_id": market["snapshot_id"],
                "forecast_id": None,
                "base_version_id": None,
                "horizon_minutes": [1],
            },
            "minute-request",
        )
        assert response.status_code == 201, response.text
        req = response.json()
        clock.value = "2026-10-12T02:00:01Z"
        imported = post(
            client,
            "/v1/analysis/imports",
            {
                "request_id": req["request_id"],
                "raw_text": minute_answer(req),
                "source": "manual_file",
                "provider": "synthetic acceptance",
                "model_id": None,
            },
            "minute-import",
        )
        assert app.state.workflows.process_once()
        job = get(client, "/v1/jobs/" + imported.json()["job_id"]).json()
        assert job["state"] == "succeeded", job
    # Neither browser nor in-memory timer survives; due objects are recovered from SQLite.
    clock.value = "2026-10-12T02:04:00Z"
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        assert app.state.workflows.enqueue_due_reviews() == 1
        assert app.state.workflows.process_once()
        with app.state.workflows.db.read() as scope:
            jobs = app.state.workflows.jobs.list(scope)
        job = next(j for j in jobs if j["kind"] == "review_refresh")
        assert job["state"] == "succeeded", json.dumps(job, ensure_ascii=False)
        reviews = get(client, "/v1/reviews").json()["items"]
        assert len(reviews) == 2, reviews
        prediction = next(r for r in reviews if r["target_kind"] == "forecast")
        assert prediction["outcome"] == "EVALUATED", prediction
        assert prediction["inputs"]["forecast_version_id"]
        assert all(r["available_at"] == "2026-10-12T02:04:00Z" for r in reviews)
        assert app.state.workflows.enqueue_due_reviews() == 0
        assert not app.state.workflows.process_once()  # completed windows are not enqueued again
        market = refresh(client, app, "after-review")
        request = post(
            client,
            "/v1/analysis/requests",
            {
                "kind": "new",
                "market_snapshot_id": market["snapshot_id"],
                "forecast_id": None,
                "base_version_id": None,
                "horizon_minutes": [180],
            },
            "with-experience",
        ).json()
        assert "experience/2" in request["packet_text"]
        assert prediction["review_id"] in request["packet_text"]
