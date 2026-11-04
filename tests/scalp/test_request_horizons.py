import copy

import pytest

from tests.guidance.test_workflow import Flow, TOKEN
from tests.scalp.test_integration import facts


@pytest.fixture
def flow(tmp_path):
    instance = Flow(tmp_path)
    yield instance
    instance.close()


def command(market, **changes):
    return {"kind": "new", "forecast_id": None, "base_version_id": None,
            "market_snapshot_id": market["snapshot_id"], **changes}


def change_settings(flow, **changes):
    current = flow.get("/v1/settings")
    values = copy.deepcopy(current["values"])
    values.update(changes)
    response = flow.client.put("/v1/settings", json={"expected_revision": current["revision"], "values": values},
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "horizons-" + str(current["revision"])})
    assert response.status_code == 200, response.text


def test_normal_api_resolves_all_enabled_horizons_and_freezes_outbound(flow):
    market = flow.refresh()
    created = flow.post("/v1/analysis/requests", command(market))
    expected = [30, 60, 180, 360, 720]
    assert created["horizon_minutes"] == expected
    frozen = facts(created["packet_text"])
    assert frozen["request"]["horizon_minutes"] == expected
    with flow.w.db.read() as scope:
        projected, _, _ = flow.w.analysis.model_packet(created["request_id"], scope=scope)
        assert not flow.w.db.documents("analysis").list(scope, "model_call")
    assert facts(projected)["request"]["horizon_minutes"] == expected
    assert [item["horizon_minutes"] for item in frozen["statistics"]["non_overlapping_change_distributions"]] == expected
    preferences = copy.deepcopy(flow.get("/v1/settings")["values"]["analysis_preferences"])
    preferences["enabled_styles"] = ["intraday"]
    change_settings(flow, analysis_preferences=preferences, daily_horizons_minutes=[240, 480])
    assert flow.post("/v1/analysis/requests", command(market))["horizon_minutes"] == [240, 480]
    assert flow.get("/v1/analysis/requests/" + created["request_id"])["packet_text"] == created["packet_text"]


def test_explicit_custom_windows_are_not_silently_expanded(flow):
    created = flow.post("/v1/analysis/requests", command(flow.refresh(), horizon_minutes=[180, 360, 720]))
    assert created["horizon_minutes"] == [180, 360, 720]


def test_update_without_horizons_inherits_original_not_current_settings(flow):
    flow.initial()
    before = flow.get("/v1/workspace")["current_forecast"]
    change_settings(flow, daily_horizons_minutes=[240, 480])
    flow.clock.value = "2026-10-12T02:01:00Z"
    updated = flow.post("/v1/analysis/requests", command(flow.refresh(), kind="update",
        forecast_id=before["forecast_id"], base_version_id=before["current_version_id"]))
    assert updated["horizon_minutes"] == before["horizon_minutes"] == [180, 360, 720]
    assert updated["window_start"] == before["window_start"]
    assert updated["window_end"] == before["window_end"]


def test_default_overflow_rejected_without_dropping_configured_horizons(flow):
    change_settings(flow, daily_horizons_minutes=[15, 45, 90, 180, 360, 720])
    market = flow.refresh()
    result = flow.post("/v1/analysis/requests", command(market), expected=422)
    assert result["code"] == "TIME_WINDOW_INVALID"
    assert "6" in result["detail"] and "截断" in result["detail"]
    assert flow.get("/v1/analysis/requests?state=awaiting_answer")["items"] == []


def test_default_union_is_sorted_deduplicated_and_keeps_sixth_window(flow):
    change_settings(flow, daily_horizons_minutes=[720, 15, 360, 60, 30, 180])
    created = flow.post("/v1/analysis/requests", command(flow.refresh()))
    assert created["horizon_minutes"] == [15, 30, 60, 180, 360, 720]


def test_auto_preparation_uses_same_defaults_without_model_call(flow):
    flow.initial()
    flow.clock.value = "2026-10-12T02:31:00Z"
    flow.refresh()
    flow.w.guidance.tick()
    requests = flow.get("/v1/analysis/requests?state=awaiting_answer")["items"]
    assert requests
    prepared = flow.get("/v1/analysis/requests/" + requests[0]["request_id"])
    assert prepared["horizon_minutes"] == [30, 60, 180, 360, 720]
    with flow.w.db.read() as scope:
        assert not flow.w.db.documents("analysis").list(scope, "model_call")


def test_overflow_auto_preparation_does_not_break_fact_refresh_or_truncate(flow):
    flow.initial()
    change_settings(flow, daily_horizons_minutes=[15, 45, 90, 180, 360, 720])
    flow.clock.value = "2026-10-12T02:31:00Z"
    refreshed = flow.refresh()
    flow.w.guidance.tick()
    assert flow.get("/v1/workspace")["market"]["snapshot_id"] == refreshed["snapshot_id"]
    assert flow.get("/v1/analysis/requests?state=awaiting_answer")["items"] == []


@pytest.mark.parametrize("horizons", [None, [], [30, 30]])
def test_explicit_invalid_horizons_are_not_replaced_with_defaults(flow, horizons):
    flow.post("/v1/analysis/requests", command(flow.refresh(), horizon_minutes=horizons), expected=422)
