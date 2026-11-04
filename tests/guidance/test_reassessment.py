"""Current closed-bar checks must not inherit unrelated historical gaps."""
import copy

import pytest
from xau_next.domains.plans.advice import rule_now
from xau_next.shared.snapshots import Snapshot

from tests.guidance.test_advice import case
from tests.guidance.test_workflow import Flow

GAP = "区间存在缺柱或非交易时段，未补造 K 线"
NOW = "2026-10-12T02:07:00Z"
RULE = {"kind": "bar_close", "timeframe": "M5", "operator": "gt", "price": "99"}


def gap_market():
    market = case()[1]
    bars = [{"open_at": start, "close_at": end, "open": "100", "high": "101", "low": "99", "close": "100",
             "closed": True, "tick_volume": 5} for start, end in [
        ("2026-10-12T01:30:00Z", "2026-10-12T01:35:00Z"),
        ("2026-10-12T01:55:00Z", "2026-10-12T02:00:00Z"),
        ("2026-10-12T02:00:00Z", "2026-10-12T02:05:00Z")]]
    market["series"] = [{"timeframe": "M5", "price_basis": "bid", "bars": bars,
        "status": {"state": "incomplete", "captured_at": NOW, "observed_at": bars[-1]["close_at"], "reasons": [GAP]}}]
    return market


def test_old_gap_does_not_hide_a_complete_recent_rule_window():
    market = gap_market()
    original = copy.deepcopy(market)
    assert rule_now(RULE, market, NOW, 10000) is True
    assert market == original  # Source incomplete status and gaps stay original.
    assert rule_now(RULE, market, NOW, 10000, after="2026-10-12T02:06:00Z") is False


@pytest.mark.parametrize("problem", ["recent_gap", "duplicate", "wrong_length", "unknown_basis", "time_basis", "unknown_failure", "old_capture", "future_capture"])
def test_current_window_or_source_defects_still_fail_closed(problem):
    market = gap_market(); series = market["series"][0]
    if problem == "recent_gap": series["bars"].pop(-2)
    if problem == "duplicate": series["bars"].append(copy.deepcopy(series["bars"][-1]))
    if problem == "wrong_length": series["bars"][-1]["open_at"] = "2026-10-12T02:01:00Z"
    if problem == "unknown_basis": series["price_basis"] = "unknown"
    if problem == "time_basis": series["status"]["reasons"].append("TIME_BASIS_UNKNOWN：终端时间基准待确认")
    if problem == "unknown_failure": series["status"]["reasons"] = ["未知的来源异常"]
    if problem == "old_capture": series["status"]["captured_at"] = "2026-10-12T02:06:00Z"
    if problem == "future_capture": series["status"]["captured_at"] = "2026-10-12T02:08:00Z"
    assert rule_now(RULE, market, NOW, 10000) is None


@pytest.mark.parametrize("has_plan", [False, True])
def test_data_gap_waits_for_facts_without_requesting_another_model_answer(tmp_path, monkeypatch, has_plan):
    flow = Flow(tmp_path)
    try:
        def no_plan(answer):
            if not has_plan:
                answer["plans"] = []
                for item in answer["candidate_assessments"]:
                    item.update(outcome="no_candidate", plan_key=None, missing_conditions=["合成：本模式结构依据不足。"])
            answer["analysis_basis"]["reassessment_conditions"] = [{"trading_style": "scalp", "condition": RULE}]
        _req, raw = flow.initial(mutate=no_plan)
        original = flow.w.market.current

        def unavailable(*args, **kwargs):
            market = original(*args, **kwargs).to_dict()
            next(s for s in market["series"] if s["timeframe"] == "M5")["status"].update(
                state="unavailable", reasons=["合成读取失败"])
            return Snapshot.of(market)

        monkeypatch.setattr(flow.w.market, "current", unavailable)
        for _ in range(2):
            flow.w.guidance.tick()
        ws = flow.get("/v1/workspace")
        refresh = next(r for r in ws["analysis_refresh"] if r["trading_style"] == "scalp")
        assert refresh["state"] == "WAITING_DATA"
        assert "M5" in "；".join(refresh["reasons"])
        assert refresh["pending_request_id"] is None
        assert len(ws["plans"]) == int(has_plan) and not ws["notifications"]
        if has_plan:
            assert ws["action_advice"][0]["state"] == "BLOCKED"
        assert flow.get("/v1/analysis/requests?state=awaiting_answer")["items"] == []
        monkeypatch.setattr(flow.w.market, "current", original)
        restored = flow.get("/v1/workspace")
        assert next(r for r in restored["analysis_refresh"] if r["trading_style"] == "scalp")["state"] == "FRESH"
        assert flow.get("/v1/forecasts/" + _req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        flow.close()
