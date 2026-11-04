"""AC02–09/19: public HTTP, actual SQLite, actual JSON-lines synthetic bridge."""

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
from xau_next.bootstrap import create_app

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "isolated-integration-token"


class Clock:
    value = "2026-10-12T02:00:00Z"

    def __call__(self):
        return self.value


def post(client, path, body, key="command-1"):
    return client.post(path, json=body, headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": key})


def get(client, path):
    return client.get(path, headers={"Authorization": "Bearer " + TOKEN})


def refresh(client, app, key="market-1"):
    r = post(
        client,
        "/v1/market/refresh",
        {"include_history": False, "history_from": None, "history_to": None},
        key,
    )
    assert r.status_code == 202, r.text
    assert app.state.workflows.process_once()
    job = get(client, "/v1/jobs/" + r.json()["job_id"]).json()
    assert job["state"] == "succeeded", job
    return get(client, "/v1/workspace").json()["market"]


def request(client, market, key="req-1", *, forecast=None, base=None):
    r = post(
        client,
        "/v1/analysis/requests",
        {
            "kind": "update" if forecast else "new",
            "market_snapshot_id": market["snapshot_id"],
            "forecast_id": forecast,
            "base_version_id": base,
            "horizon_minutes": [180, 360, 720],
        },
        key,
    )
    assert r.status_code == 201, r.text
    return r.json()


def answer(request):
    a = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    for k in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        a[k] = request[k]
    a["evidence_refs"] = [request["market_snapshot_id"]]
    for h in a["horizons"]:
        h["phases"][0]["start_at"] = request["market_cutoff"]
    if request["kind"] == "update":
        a["plans"][0].update(
            valid_from=request["market_cutoff"],
            entry_deadline="2026-10-12T04:30:00Z",
            exit_deadline="2026-10-12T05:00:00Z",
        )
    return "  " + json.dumps(a, ensure_ascii=False, indent=2) + "\n"


def submit(client, app, request, raw, key="import-1"):
    body = {
        "request_id": request["request_id"],
        "raw_text": raw,
        "source": "manual_paste",
        "provider": None,
        "model_id": None,
    }
    r = post(client, "/v1/analysis/imports", body, key)
    assert r.status_code == 202, r.text
    app.state.workflows.process_once()
    return get(client, "/v1/jobs/" + r.json()["job_id"]).json(), r.json(), body


def test_ac02_to_09_public_flow_and_restart(tmp_path):
    clock = Clock()
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        assert client.get("/v1/workspace").status_code == 401
        assert client.get("/v1/health").status_code == 200
        market = refresh(client, app)
        assert market["source_kind"] == "synthetic" and market["quote"]["bid"] == "99.98"
        req = request(client, market)
        assert get(client, "/v1/forecasts").json()["items"] == []
        assert req["request_id"] in req["packet_text"]
        clock.value = "2026-10-12T02:07:00Z"
        raw = answer(req)
        job, accepted, body = submit(client, app, req, raw)
        assert job["state"] == "succeeded", json.dumps(job, ensure_ascii=False)
        replay = post(client, "/v1/analysis/imports", body, "import-1")
        assert replay.json() == accepted
        changed = copy.deepcopy(body)
        changed["raw_text"] += " "
        assert post(client, "/v1/analysis/imports", changed, "import-1").status_code == 409
        again, _, _ = submit(client, app, req, raw, "import-other-key")
        assert again["state"] == "succeeded"
        forecast = get(client, "/v1/forecasts/" + req["forecast_id"]).json()
        version = forecast["current_version"]
        first_id = version["version_id"]
        assert version["raw_text"] == raw
        assert version["available_at"] == "2026-10-12T02:07:00Z"
        assert len(version["plans"]) == 1
        assert len(get(client, f"/v1/forecasts/{req['forecast_id']}/versions").json()["items"]) == 1
        workspace = get(client, "/v1/workspace").json()
        assert (
            workspace["current_forecast"]["current_version_id"] == first_id and len(workspace["plans"]) == 1
        )
        # Two requests share one base; only one may commit.
        clock.value = "2026-10-12T04:00:00Z"
        newer = refresh(client, app, "market-2")
        req2 = request(client, newer, "req-2", forecast=req["forecast_id"], base=first_id)
        req3 = request(client, newer, "req-3", forecast=req["forecast_id"], base=first_id)
        clock.value = "2026-10-12T04:01:00Z"
        ok, _, _ = submit(client, app, req2, answer(req2), "update-1")
        conflict, _, _ = submit(client, app, req3, answer(req3), "update-2")
        assert ok["state"] == "succeeded", ok
        assert conflict["state"] == "failed" and conflict["error"]["code"] == "VERSION_CONFLICT"
        assert (
            get(client, "/v1/forecasts/" + req["forecast_id"]).json()["forecast"]["window_end"]
            == req["window_end"]
        )
    restarted = create_app(
        data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False
    )
    with TestClient(restarted) as client:
        forecast = get(client, "/v1/forecasts/" + req["forecast_id"]).json()
        assert forecast["current_version"]["sequence"] == 2
        assert (
            get(client, f"/v1/forecasts/{req['forecast_id']}/versions/{first_id}").json()["raw_text"] == raw
        )
        assert len(get(client, f"/v1/forecasts/{req['forecast_id']}/versions").json()["items"]) == 2
        assert get(client, "/v1/workspace").status_code == 200


def test_ac05_failure_attempt_and_ac09_expired_lease_recovery(tmp_path):
    clock = Clock()
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        market = refresh(client, app)
        req = request(client, market)
        bad, _, _ = submit(client, app, req, '{"invalid":true}')
        assert bad["state"] == "failed" and bad["error"]["code"] == "SCHEMA_INVALID"
        assert get(client, "/v1/forecasts").json()["items"] == []
        attempts = app.state.workflows.analysis.list_import_attempts(req["request_id"])
        assert len(attempts) == 1 and attempts[0].to_dict()["raw_text"] == '{"invalid":true}'
        response = post(
            client,
            "/v1/market/refresh",
            {"include_history": False, "history_from": None, "history_to": None},
            "crash-queued",
        )
        lease = app.state.workflows.jobs.claim("crashed-process")
        assert lease.job_id == response.json()["job_id"]
    clock.value = "2026-10-12T02:01:00Z"
    restarted = create_app(
        data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False
    )
    with TestClient(restarted) as client:
        assert restarted.state.workflows.process_once()
        assert get(client, "/v1/jobs/" + lease.job_id).json()["state"] == "succeeded"
        assert not restarted.state.workflows.process_once()
