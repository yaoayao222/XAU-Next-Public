import copy
import json

from tests.guidance.test_workflow import Flow


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def test_new_request_freezes_price_preferences_and_projects_them(tmp_path):
    flow = Flow(tmp_path)
    try:
        assert flow.get("/v1/health")["contract_version"] == flow.app.state.contracts.api["info"]["version"]
        flow.configure()
        request = flow.request(flow.refresh())
        preferences = request["analysis_preferences"]["scalp_preferences"]
        assert preferences["target_min"] == "2.00"
        assert preferences["target_max"] == "5.00"
        assert preferences["price_unit"] == "USD_per_ounce"
        local = facts(request["packet_text"])
        assert local["near_term"]["version"] == "near-term/1"
        with flow.w.db.read() as scope:
            projected, _, _ = flow.w.analysis.model_packet(request["request_id"], scope=scope)
        external = facts(projected)
        assert external["analysis_preferences"]["scalp_preferences"] == preferences
        assert external["near_term"] == local["near_term"]
        assert external["experience"] == local["experience"]
        for original, compact in zip(local["market"]["series"], external["market"]["series"]):
            assert [dict(zip(compact["bar_columns"], row)) for row in compact["bars"]] == original["bars"]
        before = flow.get("/v1/settings")
        values = copy.deepcopy(before["values"])
        values["analysis_preferences"]["scalp_preferences"]["target_max"] = "4.00"
        response = flow.client.put("/v1/settings", json={"expected_revision": before["revision"], "values": values},
            headers={"Authorization": "Bearer synthetic-guidance-token", "Idempotency-Key": "changed-scalp"})
        assert response.status_code == 200, response.text
        saved = flow.get("/v1/analysis/requests/" + request["request_id"])
        assert saved["packet_text"] == request["packet_text"]
        assert saved["analysis_preferences"]["scalp_preferences"] == preferences
        assert flow.request(flow.refresh())["analysis_preferences"]["scalp_preferences"]["target_max"] == "4.00"
    finally:
        flow.close()


def test_previous_forecast_windows_and_conditions_are_not_lost_in_api_projection(tmp_path):
    flow = Flow(tmp_path)
    try:
        flow.initial(mutate=lambda answer: answer["analysis_basis"].update(reassessment_conditions=[
            {"trading_style": "scalp", "condition": {"kind": "quote_compare", "quote_side": "bid", "operator": "lt", "price": "98.00"}}]))
        flow.clock.value = "2026-10-12T02:00:04Z"
        request = flow.request(flow.refresh())
        local = facts(request["packet_text"])
        with flow.w.db.read() as scope:
            external, _, _ = flow.w.analysis.model_packet(request["request_id"], scope=scope)
        assert facts(external)["previous_analysis"] == local["previous_analysis"]
    finally:
        flow.close()


def test_far_target_import_preserves_answer_but_never_ready(tmp_path):
    flow = Flow(tmp_path)
    try:
        flow.configure()
        request = flow.request(flow.refresh())
        answer = flow.answer(request)
        answer["plans"][0]["take_profit"] = "113.19"
        flow.clock.value = "2026-10-12T02:00:01Z"
        job, raw = flow.submit(request, answer)
        assert job["state"] == "succeeded", job
        workspace = flow.get("/v1/workspace")
        record = workspace["plans"][0]
        assert record["scalp_suitability"]["state"] == "incompatible"
        assert workspace["action_advice"][0]["state"] != "READY"
        version = flow.get("/v1/forecasts/" + request["forecast_id"] + "/versions/" + record["version_id"])
        assert version["answer"] == answer
        assert version["raw_text"] == raw
    finally:
        flow.close()
