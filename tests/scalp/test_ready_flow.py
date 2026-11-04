"""Synthetic public-API acceptance using unchanged money and readiness gates."""

import copy
import hashlib
from decimal import Decimal

import pytest
from xau_next.shared.values import new_id, parse_utc

from tests.guidance.test_workflow import TOKEN, Flow


@pytest.fixture
def flow(tmp_path):
    current = Flow(tmp_path)
    yield current
    current.close()


def install_market_path(flow, monkeypatch):
    path = {"quote": {"bid": "99.98", "ask": "100.00"}, "m1_closes": {}}
    original = flow.w.market._bridge.request

    def source(operation, params):
        response = original(operation, params)
        if operation == "snapshot":
            market = response["result"]["market"]
            market["quote"].update(path["quote"])
            for series in market["series"]:
                if series["timeframe"] != "M1":
                    continue
                for bar in series["bars"]:
                    if parse_utc(bar["close_at"]) in path["m1_closes"]:
                        close = path["m1_closes"][parse_utc(bar["close_at"])]
                        bar.update(close=close, high=str(max(Decimal(bar["high"]), Decimal(close))),
                                   low=str(min(Decimal(bar["low"]), Decimal(close))))
        return response

    monkeypatch.setattr(flow.w.market._bridge, "request", source)
    return path


def local_candidate(answer, side="long"):
    candidate = answer["plans"][0]
    candidate.update(side=side, max_holding_minutes=10, holding_time_basis=None,
                     stop_loss="99.00" if side == "long" else "101.00",
                     take_profit="103.00" if side == "long" else "97.00",
                     post_activation_invalidation=None,
                     activation={"kind": "bar_close", "timeframe": "M1",
                                 "operator": "gt" if side == "long" else "lt",
                                 "price": "100.20" if side == "long" else "99.80"},
                     invalidation={"kind": "quote_compare", "quote_side": "bid" if side == "long" else "ask",
                                   "operator": "lt" if side == "long" else "gt",
                                   "price": "99.00" if side == "long" else "101.00"},
                     entry_checks=[{"kind": "quote_compare", "quote_side": "ask" if side == "long" else "bid",
                                    "operator": "lte" if side == "long" else "gte", "price": "100.00",
                                    "observation":"new_since_available", "purpose":"合成：精确报价仍满足独立入场边界。"}])
    candidate["entry"] = {"mode": "limit_retest", "price": "100.00",
                          "zone": {"low": "99.90" if side == "long" else "100.00",
                                   "high": "100.00" if side == "long" else "100.10"}}
    return candidate


def create_and_import(flow, mutate):
    flow.configure()
    request = flow.request(flow.refresh())
    answer = flow.answer(request)
    mutate(answer)
    original = copy.deepcopy(answer)
    flow.clock.value = "2026-10-12T02:00:01Z"
    job, raw = flow.submit(request, answer)
    assert job["state"] == "succeeded", job
    assert answer == original
    return request, raw


def update_settings(flow, change):
    before = flow.get("/v1/settings")
    values = copy.deepcopy(before["values"])
    change(values)
    response = flow.client.put("/v1/settings", json={"expected_revision": before["revision"], "values": values},
                               headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()})
    assert response.status_code == 200, response.text
    return response.json()


def version(flow, request):
    return flow.get("/v1/forecasts/" + request["forecast_id"])["current_version"]


def stored_version(flow, request):
    current = version(flow, request)
    with flow.w.db.read() as scope:
        return flow.w.analysis._docs.get(scope, "version", current["version_id"])


@pytest.mark.parametrize("side", ["long", "short"])
def test_new_close_then_retest_and_all_original_money_checks_reach_ready(flow, monkeypatch, side):
    path = install_market_path(flow, monkeypatch)
    request, raw = create_and_import(flow, lambda answer: local_candidate(answer, side))
    raw_hash = hashlib.sha256(raw.encode()).hexdigest()
    path["quote"].update(bid="100.30" if side == "long" else "99.70",
                         ask="100.32" if side == "long" else "99.72")
    flow.clock.value = "2026-10-12T02:00:20Z"
    flow.refresh()
    waiting = flow.get("/v1/workspace")
    assert waiting["plans"][0]["lifecycle"] == "WAITING_TRIGGER"
    assert waiting["action_advice"][0]["state"] == "WAITING", waiting["action_advice"]
    diagnostic = waiting["action_advice"][0]["scalp_suitability"]
    assert diagnostic["trigger_distances"][0]["state"] == "price_satisfied"
    assert diagnostic["trigger_distances"][0]["distance"] == "0.00"
    assert "新闭柱" in diagnostic["trigger_distances"][0]["note"]
    path["m1_closes"][parse_utc("2026-10-12T02:01:00Z")] = "100.30" if side == "long" else "99.70"
    flow.clock.value = "2026-10-12T02:01:01Z"
    flow.refresh()
    active = flow.get("/v1/workspace")
    assert active["plans"][0]["lifecycle"] == "ACTIVE"
    assert active["action_advice"][0]["state"] == "WAITING", active["action_advice"]
    assert "回踩" in active["action_advice"][0]["title"]
    assert active["action_advice"][0]["scalp_suitability"]["trigger_distances"][0]["state"] == "latched"
    path["quote"].update(bid="99.98" if side == "long" else "100.00",
                         ask="100.00" if side == "long" else "100.02")
    flow.clock.value = "2026-10-12T02:01:02Z"
    flow.refresh()
    ready = flow.get("/v1/workspace")
    assert ready["action_advice"][0]["state"] == "READY", ready["action_advice"]
    assert ready["action_advice"][0]["entry_price"] == "100.00"
    net_check = ready["action_advice"][0]["scalp_suitability"]["range_net_check"]
    assert net_check["state"] == "satisfied" and len(net_check["boundary_results"]) == 2
    assert all(Decimal(boundary["net_rr"]) >= 2 for boundary in net_check["boundary_results"])
    assert all(ready["evaluations"][0][name]["state"] == "satisfied"
               for name in ("quote_check", "cost_check", "budget_check", "margin_check"))
    settings = flow.get("/v1/settings")["values"]
    assert settings["min_net_rr"] == "2"
    assert settings["per_trade_loss_cap"] == {"amount": "20", "currency": "USD"}
    assert settings["cost_assumption"]["commission_roundtrip_per_lot"] == "5"
    assert settings["cost_assumption"]["slippage_reserve_per_lot"] == "20"
    saved = version(flow, request)
    assert hashlib.sha256(saved["raw_text"].encode()).hexdigest() == raw_hash
    assert saved["answer"]["plans"][0]["stop_loss"] == ("99.00" if side == "long" else "101.00")


def test_best_current_net_rr_cannot_hide_failure_at_other_allowed_boundary(flow, monkeypatch):
    install_market_path(flow, monkeypatch)

    def candidate(answer):
        local_candidate(answer).update(activation={"kind": "immediate"}, entry_checks=[])
        answer["plans"][0]["entry"] = {"mode": "market", "price": "100.00",
                                       "zone": {"low": "99.80", "high": "100.80"}}

    create_and_import(flow, candidate)
    flow.clock.value = "2026-10-12T02:00:02Z"
    flow.refresh()
    workspace = flow.get("/v1/workspace")
    assert Decimal(workspace["evaluations"][0]["net_rr"]) >= 2
    assert workspace["evaluations"][0]["budget_check"]["state"] == "satisfied"
    advice = workspace["action_advice"][0]
    assert advice["state"] == "BLOCKED"
    assert advice["scalp_suitability"]["state"] == "compatible"
    check = advice["scalp_suitability"]["range_net_check"]
    assert check["state"] == "not_satisfied"
    boundaries = {row["entry_price"]: row for row in check["boundary_results"]}
    assert boundaries["99.80"]["state"] == "satisfied"
    assert boundaries["100.80"]["state"] == "not_satisfied"
    assert Decimal(boundaries["100.80"]["net_rr"]) < 2


def test_distance_is_recomputed_and_far_candidate_can_become_near_without_rewriting_import(flow, monkeypatch):
    path = install_market_path(flow, monkeypatch)

    def candidate(answer):
        local_candidate(answer).update(activation={"kind": "immediate"}, entry_checks=[],
                                       stop_loss="101.30", take_profit="105.30",
                                       invalidation={"kind": "quote_compare", "quote_side": "bid", "operator": "lt", "price": "97"})
        answer["plans"][0]["entry"] = {"mode": "limit_retest", "price": "102.30",
                                       "zone": {"low": "102.20", "high": "102.30"}}

    request, raw = create_and_import(flow, candidate)
    flow.clock.value = "2026-10-12T02:00:02Z"
    flow.refresh()
    far = flow.get("/v1/workspace")["action_advice"][0]
    assert far["state"] == "BLOCKED" and far["scalp_suitability"]["entry_distance"] == "2.20"
    assert "ENTRY_DISTANCE_EXCEEDED" in far["scalp_suitability"]["codes"]
    original_diagnostic = copy.deepcopy(stored_version(flow, request)["plan_drafts"][0]["scalp_suitability"])
    path["quote"].update(bid="102.20", ask="102.22")
    flow.clock.value = "2026-10-12T02:00:03Z"
    flow.refresh()
    near = flow.get("/v1/workspace")["action_advice"][0]
    assert near["state"] == "READY", near
    assert near["scalp_suitability"]["entry_distance"] == "0.00"
    saved = version(flow, request)
    assert saved["raw_text"] == raw
    assert stored_version(flow, request)["plan_drafts"][0]["scalp_suitability"] == original_diagnostic


def test_static_out_of_scale_scalp_does_not_reject_same_answer_intraday(flow, monkeypatch):
    path = install_market_path(flow, monkeypatch)

    def candidates(answer):
        scalp = local_candidate(answer)
        scalp.update(activation={"kind": "immediate"}, entry_checks=[], take_profit="111.69")
        intraday = copy.deepcopy(scalp)
        intraday.update(plan_key="intraday", role="backup", trading_style="intraday", take_profit="103.00",
                        max_holding_minutes=120, entry_deadline="2026-10-12T03:00:00Z")
        answer["plans"].append(intraday)
        for row in answer["candidate_assessments"]:
            row.update(outcome="candidate", plan_key="primary" if row["trading_style"] == "scalp" else "intraday",
                       missing_conditions=[], rationale="合成：保留每种模式的独立候选与判断。")

    request, raw = create_and_import(flow, candidates)
    for timestamp, bid, ask in [("2026-10-12T02:00:02Z", "99.98", "100.00"),
                                 ("2026-10-12T02:00:03Z", "99.93", "99.95")]:
        flow.clock.value = timestamp
        path["quote"].update(bid=bid, ask=ask)
        flow.refresh()
        workspace = flow.get("/v1/workspace")
        advice = {item["trading_style"]: item for item in workspace["action_advice"]}
        assert advice["scalp"]["state"] == "BLOCKED"
        assert "TARGET_SPACE_TOO_LARGE" in advice["scalp"]["scalp_suitability"]["codes"]
        assert advice["intraday"]["state"] == "READY", advice["intraday"]
        assert "scalp_suitability" not in advice["intraday"]
    assert version(flow, request)["raw_text"] == raw
    assert len(version(flow, request)["answer"]["plans"]) == 2


def test_settings_change_only_affects_new_request_not_frozen_existing_candidate(flow, monkeypatch):
    install_market_path(flow, monkeypatch)

    def candidate(answer):
        local_candidate(answer).update(activation={"kind": "immediate"}, entry_checks=[])

    request, raw = create_and_import(flow, candidate)
    packet = request["packet_text"]
    update_settings(flow, lambda values: values["analysis_preferences"]["scalp_preferences"].update(target_max="2.50"))
    flow.clock.value = "2026-10-12T02:00:02Z"
    market = flow.refresh()
    workspace = flow.get("/v1/workspace")
    assert workspace["action_advice"][0]["state"] == "READY", workspace["action_advice"]
    assert workspace["plans"][0]["scalp_preferences"]["target_max"] == "5.00"
    new_request = flow.request(market)
    assert new_request["analysis_preferences"]["scalp_preferences"]["target_max"] == "2.50"
    old_request = flow.get("/v1/analysis/requests/" + request["request_id"])
    assert old_request["packet_text"] == packet
    assert old_request["analysis_preferences"]["scalp_preferences"]["target_max"] == "5.00"
    assert version(flow, request)["raw_text"] == raw


def test_frozen_14_request_and_answer_remain_without_new_scalp_semantics(flow, monkeypatch):
    install_market_path(flow, monkeypatch)
    flow.configure()
    market = flow.refresh()
    with monkeypatch.context() as legacy:
        legacy.setattr("xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION", "1.4.0")
        legacy.setattr("xau_next.domains.analysis.service.PROMPT_VERSION", "manual-analysis/8")
        legacy.setattr("xau_next.domains.analysis.service.analysis_preferences",
                       lambda values: {key: value for key, value in values["analysis_preferences"].items()
                                       if key != "scalp_preferences"})
        request = flow.request(market)
    assert "scalp_preferences" not in request["analysis_preferences"]
    packet = request["packet_text"]
    answer = flow.answer(request)
    answer["plans"][0]["take_profit"] = "111.69"
    flow.clock.value = "2026-10-12T02:00:01Z"
    job, raw = flow.submit(request, answer)
    assert job["state"] == "succeeded", job
    flow.clock.value = "2026-10-12T02:00:02Z"
    flow.refresh()
    workspace = flow.get("/v1/workspace")
    assert workspace["action_advice"][0]["state"] == "READY", workspace["action_advice"]
    assert "scalp_suitability" not in workspace["action_advice"][0]
    assert "scalp_preferences" not in workspace["plans"][0]
    assert "scalp_suitability" not in workspace["plans"][0]
    saved = version(flow, request)
    assert saved["answer"] == answer and saved["raw_text"] == raw
    assert saved["answer"]["plans"][0]["take_profit"] == "111.69"
    assert flow.get("/v1/analysis/requests/" + request["request_id"])["packet_text"] == packet
