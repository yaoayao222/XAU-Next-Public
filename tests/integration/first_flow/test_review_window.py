"""Fractional forecast windows through HTTP, bridge, review worker and SQLite."""

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_flow import ROOT, TOKEN, Clock, get, post, refresh, submit
from xau_next.bootstrap import create_app
from xau_next.shared.values import parse_utc

CUTOFF = "2026-10-12T02:00:50.792Z"
IMPORTED = "2026-10-12T02:00:51.792Z"
EXIT_QUOTE = "2026-10-12T02:03:51.792Z"


def fractional_answer(request, *, turning_points):
    answer = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        answer[key] = request[key]
    points = [
        {
            "kind": kind,
            "earliest_at": at,
            "latest_at": at,
            "price_range": {"low": price, "high": price},
        }
        for kind, at, price in (
            ("local_low", "2026-10-12T02:01:00Z", "99.50"),
            ("local_high", "2026-10-12T02:03:00Z", "100.50"),
        )
    ]
    answer["horizons"] = [
        {
            **answer["horizons"][0],
            "horizon_minutes": 3,
            "end_at": request["window_end"],
            "direction": "long",
            "phases": [
                {
                    "start_at": request["market_cutoff"],
                    "end_at": request["window_end"],
                    "direction": "long",
                    "note": "合成毫秒截点窗口",
                }
            ],
            "turning_points": points if turning_points else [],
        }
    ]
    answer["plans"][0].update(
        activation={"kind": "immediate"},
        invalidation={"kind": "manual_observation", "instruction": "合成路径没有人工失效事件"},
        entry={"mode": "market", "price": None, "zone": None},
        valid_from=request["market_cutoff"],
        entry_deadline="2026-10-12T02:02:50.792Z",
        exit_deadline=request["window_end"],
        max_holding_minutes=10,
    )
    answer["evidence_refs"] = [request["market_snapshot_id"]]
    return answer


def history_path(monkeypatch, workflows, *, missing_bar):
    """Add deterministic extrema/ticks to real synthetic bridge replies at the source seam."""
    bridge_request = workflows.market._bridge.request
    requests = []

    def request(operation, params):
        response = bridge_request(operation, params)
        if operation != "history":
            return response
        requests.append(dict(params))
        history = response["result"]
        for bar in history["bars"]:
            if parse_utc(bar["open_at"]) == parse_utc("2026-10-12T02:01:00Z"):
                bar["low"] = "99.50"
            if parse_utc(bar["open_at"]) == parse_utc("2026-10-12T02:03:00Z"):
                bar["high"] = "100.50"
        if missing_bar:
            history["bars"] = [
                bar
                for bar in history["bars"]
                if parse_utc(bar["open_at"]) != parse_utc("2026-10-12T02:02:00Z")
            ]
            for key in ("status", "bar_status"):
                history[key].update(state="incomplete", reasons=["合成证据明确缺少区间内一根 M1"])
        for at, bid, ask in (
            ("2026-10-12T02:00:52.792Z", "99.98", "100.00"),
            (EXIT_QUOTE, "100.20", "100.22"),
        ):
            if parse_utc(params["from"]) <= parse_utc(at) <= parse_utc(params["to"]):
                history["ticks"].append({"event_time": at, "bid": bid, "ask": ask})
        history["ticks"].sort(key=lambda tick: parse_utc(tick["event_time"]))
        for sequence, tick in enumerate(history["ticks"]):
            tick["source_sequence"] = sequence
        return response

    monkeypatch.setattr(workflows.market._bridge, "request", request)
    return requests


def run_fractional_review(
    tmp_path, monkeypatch, *, as_of, turning_points=False, missing_bar=False, imported=IMPORTED
):
    clock = Clock()
    clock.value = CUTOFF
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        workflows = app.state.workflows
        market = refresh(client, app)
        response = post(
            client,
            "/v1/analysis/requests",
            {
                "kind": "new",
                "market_snapshot_id": market["snapshot_id"],
                "forecast_id": None,
                "base_version_id": None,
                "horizon_minutes": [3],
            },
            "fractional-request",
        )
        assert response.status_code == 201, response.text
        request = response.json()
        answer = fractional_answer(request, turning_points=turning_points)
        clock.value = imported
        job, _, _ = submit(client, app, request, json.dumps(answer, ensure_ascii=False))
        assert job["state"] == "succeeded", job
        source_requests = history_path(monkeypatch, workflows, missing_bar=missing_bar)
        clock.value = as_of
        response = post(
            client,
            "/v1/reviews/refresh",
            {"forecast_id": request["forecast_id"], "through": clock()},
            "fractional-review",
        )
        assert response.status_code == 202, response.text
        assert workflows.process_once()
        job = get(client, "/v1/jobs/" + response.json()["job_id"]).json()
        assert job["state"] == "succeeded", job
        reviews = get(client, "/v1/reviews").json()["items"]
        assert len(reviews) == 2, reviews
        prediction = next(review for review in reviews if review["target_kind"] == "forecast")
        reference = next(review for review in reviews if review["target_kind"] == "reference_plan")
        version = get(client, "/v1/forecasts/" + request["forecast_id"]).json()["current_version"]
        assert version["answer"] == answer  # acquiring support data must not round the prediction itself
        assert prediction["window_start"] == CUTOFF
        assert prediction["window_end"] == request["window_end"]
        assert prediction["method_version"] == "prediction_review/1"
        assert len(source_requests) == 1
        capture_from, capture_to = source_requests[0]["from"], source_requests[0]["to"]
        stored = workflows.market.read_window(
            prediction["inputs"]["market_snapshot_ids"][0], capture_from, capture_to
        ).to_dict()
        return prediction, reference, stored, source_requests[0]


@pytest.mark.parametrize(
    "as_of,turning_points,expected_to",
    [
        ("2026-10-12T02:06:55.929Z", True, "2026-10-12T02:06:00Z"),
        ("2026-10-12T02:04:20.123Z", False, "2026-10-12T02:04:00Z"),
    ],
)
def test_fractional_window_evaluates_with_original_boundaries_and_support_evidence(
    tmp_path, monkeypatch, as_of, turning_points, expected_to
):
    prediction, reference, stored, request = run_fractional_review(
        tmp_path, monkeypatch, as_of=as_of, turning_points=turning_points
    )
    assert prediction["outcome"] == "EVALUATED", prediction
    assert prediction["coverage"] == "partial"
    assert parse_utc(prediction["evaluated_start"]) == parse_utc("2026-10-12T02:01:00Z")
    assert parse_utc(prediction["evaluated_end"]) == parse_utc("2026-10-12T02:03:00Z")
    delta = next(metric for metric in prediction["metrics"] if metric["name"] == "delta")
    assert delta["state"] == "available" and Decimal(delta["value"]) == Decimal("0.05")
    assert request["from"] == "2026-10-12T01:58:00Z"
    assert request["to"] == expected_to
    assert parse_utc(request["to"]) <= parse_utc(as_of)
    assert stored["bar_status"]["state"] == "available" and stored["coverage"]["complete"]
    if turning_points:
        turns = [metric for metric in prediction["metrics"] if metric["name"] == "turning_point_hit"]
        assert len(turns) == 2 and all(
            metric["state"] == "available" and metric["value"] == "1" for metric in turns
        )
        # The first extremum needs pre-window bars, the last needs two post-window closed bars.
        assert any(parse_utc(bar["open_at"]) == parse_utc("2026-10-12T01:59:00Z") for bar in stored["bars"])
        assert any(parse_utc(bar["close_at"]) == parse_utc("2026-10-12T02:06:00Z") for bar in stored["bars"])
    assert any(tick["event_time"] == EXIT_QUOTE for tick in stored["ticks"])
    assert reference["outcome"] == "TIME_EXIT" and reference["coverage"] == "complete", reference
    assert parse_utc(reference["evaluated_end"]) == parse_utc(EXIT_QUOTE)
    gross = next(metric for metric in reference["metrics"] if metric["name"] == "reference_gross_pnl")
    assert gross["state"] == "available" and Decimal(gross["value"]) == Decimal("0.20")


def test_fractional_window_with_real_interior_gap_stays_data_incomplete(tmp_path, monkeypatch):
    prediction, _, stored, request = run_fractional_review(
        tmp_path, monkeypatch, as_of="2026-10-12T02:06:55.929Z", missing_bar=True
    )
    assert prediction["outcome"] == "DATA_INCOMPLETE", prediction
    assert prediction["coverage"] == "missing" and prediction["metrics"] == []
    assert stored["bar_status"]["state"] == "incomplete" and not stored["coverage"]["complete"]
    assert not any(parse_utc(bar["open_at"]) == parse_utc("2026-10-12T02:02:00Z") for bar in stored["bars"])
    assert request["from"] == "2026-10-12T01:58:00Z"
    assert request["to"] == "2026-10-12T02:06:00Z"


def test_answer_available_after_entry_deadline_is_expired_without_reference_capture(tmp_path, monkeypatch):
    prediction, reference, _, _ = run_fractional_review(
        tmp_path, monkeypatch, as_of="2026-10-12T02:06:55Z", imported="2026-10-12T02:03:00Z"
    )
    assert prediction["outcome"] == "EVALUATED"
    assert reference["outcome"] == "EXPIRED_WITHOUT_ENTRY"
    assert reference["evaluated_start"] == reference["evaluated_end"]


def test_later_gap_cannot_invalidate_earlier_forecast_or_reference_window(tmp_path, monkeypatch):
    from copy import deepcopy
    from datetime import timedelta

    clock = Clock()
    clock.value = CUTOFF
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        workflows = app.state.workflows
        market = refresh(client, app)
        response = post(
            client,
            "/v1/analysis/requests",
            {
                "kind": "new",
                "market_snapshot_id": market["snapshot_id"],
                "forecast_id": None,
                "base_version_id": None,
                "horizon_minutes": [3, 6, 12],
            },
            "separate-windows",
        )
        assert response.status_code == 201, response.text
        request = response.json()
        answer = fractional_answer(request, turning_points=False)
        template = deepcopy(answer["horizons"][0])
        answer["horizons"] = []
        for minutes in (3, 6, 12):
            end = (parse_utc(CUTOFF) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
            horizon = deepcopy(template)
            horizon.update(horizon_minutes=minutes, end_at=end)
            horizon["phases"][0]["end_at"] = end
            answer["horizons"].append(horizon)
        answer["plans"][0]["exit_deadline"] = answer["horizons"][0]["end_at"]
        clock.value = IMPORTED
        raw = json.dumps(answer, ensure_ascii=False)
        job, _, _ = submit(client, app, request, raw)
        assert job["state"] == "succeeded", job
        history_path(monkeypatch, workflows, missing_bar=False)
        original_request = workflows.market._bridge.request

        def history_with_later_gap(operation, params):
            response = original_request(operation, params)
            if operation == "history":
                h = response["result"]
                removed = [
                    b for b in h["bars"] if parse_utc(b["open_at"]) == parse_utc("2026-10-12T02:10:00Z")
                ]
                if removed:
                    h["bars"] = [b for b in h["bars"] if b not in removed]
                    h["bar_status"].update(state="incomplete", reasons=["合成长窗口内部真实缺柱"])
                    h["status"].update(state="incomplete", reasons=["合成长窗口内部真实缺柱"])
                    h["tick_status"] = "incomplete"
                    h["coverage"]["complete"] = False
            return response

        monkeypatch.setattr(workflows.market._bridge, "request", history_with_later_gap)
        clock.value = "2026-10-12T02:06:40Z"
        first = post(
            client,
            "/v1/reviews/refresh",
            {"forecast_id": request["forecast_id"], "through": clock()},
            "first-due",
        )
        assert first.status_code == 202 and workflows.process_once()
        before = get(client, "/v1/reviews").json()["items"]
        assert next(r for r in before if r["target_kind"] == "forecast")["outcome"] == "EVALUATED"
        assert next(r for r in before if r["target_kind"] == "reference_plan")["outcome"] == "TIME_EXIT"
        clock.value = "2026-10-12T02:16:00Z"
        later = post(
            client,
            "/v1/reviews/refresh",
            {"forecast_id": request["forecast_id"], "through": clock()},
            "last-due",
        )
        assert later.status_code == 202 and workflows.process_once()
        assert get(client, "/v1/jobs/" + later.json()["job_id"]).json()["state"] == "succeeded"
        latest = get(client, "/v1/reviews").json()["items"]
        forecasts = {r["inputs"]["horizon_minutes"]: r for r in latest if r["target_kind"] == "forecast"}
        assert forecasts[3]["outcome"] == forecasts[6]["outcome"] == "EVALUATED"
        assert forecasts[12]["outcome"] == "DATA_INCOMPLETE"
        assert next(r for r in latest if r["target_kind"] == "reference_plan")["outcome"] == "TIME_EXIT"
        history = get(client, "/v1/reviews?revisions=all&limit=100").json()["items"]
        assert all(r in history for r in before)
        assert (
            get(client, "/v1/forecasts/" + request["forecast_id"]).json()["current_version"]["raw_text"]
            == raw
        )
