"""Public API + SQLite + real synthetic bridge + injected local notification sink."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from xau_next.bootstrap import create_app
from xau_next.shared.values import new_id

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "synthetic-guidance-token"


class Clock:
    value = "2026-10-12T02:00:00Z"
    def __call__(self): return self.value


class Sink:
    def __init__(self): self.calls = []
    def send(self, notice):
        self.calls.append(copy.deepcopy(notice))
        return "submitted", "合成适配器已接收，不是 Windows 实际显示。"


class Flow:
    def __init__(self, root, clock=None, sink=None):
        self.clock, self.sink = clock or Clock(), sink or Sink()
        self.app = create_app(data_root=root, token=TOKEN, source="synthetic", clock=self.clock,
                              start_worker=False, notification_sink=self.sink, allow_desktop_notifications=True)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.count = 0
        self.closed = False

    @property
    def w(self): return self.app.state.workflows

    def get(self, path):
        response = self.client.get(path, headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path, body, expected=201):
        self.count += 1
        response = self.client.post(path, json=body, headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()})
        assert response.status_code == expected, response.text
        return response.json()

    def refresh(self):
        job = self.post("/v1/market/refresh", {"include_history": False, "history_from": None, "history_to": None}, 202)
        self.w.process_once(kind="market_refresh")
        result = self.get("/v1/jobs/" + job["job_id"])
        assert result["state"] == "succeeded", result
        return self.get("/v1/workspace")["market"]

    def configure(self):
        before = self.get("/v1/settings")
        values = copy.deepcopy(before["values"])
        values.update(default_volume_lots="0.01", per_trade_loss_cap={"amount": "20", "currency": "USD"}, min_net_rr="2",
                      cost_assumption={"commission_roundtrip_per_lot": "5", "slippage_reserve_per_lot": "20", "other_cost_per_lot": "0",
                                       "currency": "USD", "valid_from": self.clock.value, "valid_until": None,
                                       "account_scope_id": "synthetic-account", "symbol": "XAUUSD", "source_note": "合成验收"})
        response = self.client.put("/v1/settings", json={"expected_revision": before["revision"], "values": values},
                                   headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "settings"})
        assert response.status_code == 200, response.text

    def request(self, market):
        return self.post("/v1/analysis/requests", {"kind": "new", "forecast_id": None, "base_version_id": None,
                          "market_snapshot_id": market["snapshot_id"], "horizon_minutes": [180, 360, 720]})

    def answer(self, request, style="scalp"):
        filename = "analysis-answer.experience.json" if request["answer_schema_version"] in {"1.4.0", "1.5.0", "1.6.0", "1.7.0"} else "analysis-answer.staged.json"
        answer = json.loads((ROOT / "fixtures" / filename).read_text(encoding="utf-8"))
        answer["schema_version"] = request["answer_schema_version"]
        if request["answer_schema_version"] in {"1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
            facts = json.JSONDecoder().raw_decode(request["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]
            if facts.get("experience"):
                answer["experience_application"].update(digest_id=facts["experience"]["digest_id"], status="reviewed_no_change")
        for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
            answer[key] = request[key]
        answer["evidence_refs"] = [request["market_snapshot_id"]]
        answer["plans"][0].update(trading_style=style, max_holding_minutes=20 if style == "scalp" else 120,
                                  entry_deadline="2026-10-12T02:30:00Z" if style == "scalp" else "2026-10-12T03:00:00Z",
                                  activation={"kind": "immediate"}, stop_loss="99", take_profit="103",
                                  invalidation={"kind": "quote_compare", "quote_side": "bid", "operator": "lt", "price": "99"})
        if request["answer_schema_version"] in {"1.5.0", "1.6.0", "1.7.0"}:
            for plan in answer["plans"]:
                plan["holding_time_basis"] = "合成样例：局部结构仍完整，允许延长成交后观察至20分钟。" if style == "scalp" else None
        for row in answer["candidate_assessments"]:
            chosen = row["trading_style"] == style
            row.update(outcome="candidate" if chosen else "no_candidate", plan_key="primary" if chosen else None,
                       rationale="合成：有完整条件候选。" if chosen else "合成：缺少该周期结构依据。",
                       missing_conditions=[] if chosen else ["缺少该周期止损结构和目标空间依据。"])
        if request["answer_schema_version"] in {"1.6.0", "1.7.0"} and answer["schema_version"] in {"1.6.0", "1.7.0"}:
            for horizon in answer["horizons"]:
                horizon["forecast_basis"] = {
                    "direction_basis":"SYNTHETIC 端点变化判断，不将区间形态等同neutral。",
                    "range_basis":"SYNTHETIC 冻结结构区间，不能用覆盖证明准确。",
                    "supporting_evidence":["SYNTHETIC 当前闭柱资料。"],
                    "opposing_evidence":["SYNTHETIC 存在反向路径；未验证预测质量。"],
                    "evidence_refs":[request["market_snapshot_id"]],"limitations":["仅合成验收。"]}
            for plan in answer["plans"]:
                for rule in plan["entry_checks"]:
                    rule.update(observation="new_since_available",purpose="SYNTHETIC 独立入场条件")
        if request["answer_schema_version"] == "1.7.0":
            for plan in answer["plans"]:
                plan["holding_exit"] = {"condition": None, "reason": "SYNTHETIC：保留原止损，无额外结构退出条件。"}
            for advice in answer["order_advice"]:
                advice["target_assessment"] = None
        return answer

    def submit(self, request, answer):
        answer = copy.deepcopy(answer)
        if request["answer_schema_version"] in {"1.6.0", "1.7.0"} and answer["schema_version"] in {"1.6.0", "1.7.0"}:
            # Adapt existing synthetic rule builders to the explicit observation
            # contract; production never completes or repairs a model answer.
            for plan in answer["plans"]:
                for rule in plan["entry_checks"]:
                    rule.setdefault("observation", "new_since_available")
                    rule.setdefault("purpose", "合成：保留原样例的独立当前入场检查。")
        raw = "  " + json.dumps(answer, ensure_ascii=False, indent=2) + "\n"
        job = self.post("/v1/analysis/imports", {"request_id": request["request_id"], "raw_text": raw,
                        "source": "manual_paste", "provider": "synthetic", "model_id": "test-model",
                        "reasoning_effort": "high", "analysis_platform": "synthetic-test"}, 202)
        self.w.process_once(kind="answer_import")
        return self.get("/v1/jobs/" + job["job_id"]), raw

    def initial(self, style="scalp", mutate=None):
        self.configure()
        req = self.request(self.refresh())
        answer = self.answer(req, style)
        if mutate: mutate(answer)
        self.clock.value = "2026-10-12T02:00:01Z"
        job, raw = self.submit(req, answer)
        assert job["state"] == "succeeded", job
        self.clock.value = "2026-10-12T02:00:02Z"
        self.refresh()
        return req, raw

    def close(self):
        if not self.closed:
            self.client.__exit__(None, None, None)
            self.closed = True


@pytest.fixture
def flow(tmp_path):
    f = Flow(tmp_path)
    yield f
    f.close()


def test_ready_auto_margin_notification_and_restart_preserve_original(flow, tmp_path):
    req, raw = flow.initial()
    ws = flow.get("/v1/workspace")
    assert ws["action_advice"][0]["state"] == "READY", ws["action_advice"]
    assert ws["evaluations"][0]["margin_check"]["state"] == "satisfied"
    flow.w.guidance.tick()
    sent = flow.w.guidance.deliver_once()
    assert sent["state"] == "submitted"
    assert len(flow.sink.calls) == 1
    flow.w.guidance.tick()
    assert flow.w.guidance.deliver_once() is None
    saved = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    assert saved["raw_text"] == raw
    assert saved["origin"]["reasoning_effort"] == "high" and saved["origin"]["import_latency_seconds"] == 1
    assert saved["baselines"]
    flow.close()
    restarted = Flow(tmp_path, flow.clock, flow.sink)
    try:
        restarted.w.guidance.notifications.recover()
        restarted.clock.value = "2026-10-12T02:00:04Z"
        restarted.refresh()
        restarted.w.guidance.tick()
        assert restarted.w.guidance.deliver_once() is None
        assert len(restarted.sink.calls) == 1
        assert restarted.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        restarted.close()


def test_v12_empty_candidates_keep_two_explanations_without_entry_notifications(flow, tmp_path):
    flow.configure()
    req = flow.request(flow.refresh())
    assert req["answer_schema_version"] == "1.7.0" and req["prompt_version"] == "manual-analysis/14"
    assert "未触发不等于无候选" in req["packet_text"]
    assert "invalidation从等待阶段生效" in req["packet_text"]
    assert "post_activation_invalidation" in req["packet_text"]
    answer = flow.answer(req)
    answer["plans"] = []
    for row in answer["candidate_assessments"]:
        row.update(outcome="no_candidate", plan_key=None,
                   rationale="合成：本周期缺少构成候选的依据。",
                   missing_conditions=[row["trading_style"] + "：缺少明确的结构止损依据。"])
    flow.clock.value = "2026-10-12T02:00:01Z"
    job, raw = flow.submit(req, answer)
    assert job["state"] == "succeeded", job
    for second in (2, 4):
        flow.clock.value = f"2026-10-12T02:00:0{second}Z"
        flow.refresh()
        flow.w.guidance.tick()
    ws = flow.get("/v1/workspace")
    assert ws["plans"] == ws["action_advice"] == []
    assert ws["candidate_assessments"] == answer["candidate_assessments"]
    assert all(r["state"] == "FRESH" for r in ws["analysis_refresh"])
    assert not ws["notifications"]
    again, _ = flow.submit(req, answer)
    assert again["state"] == "succeeded"
    assert flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["sequence"] == 1
    flow.close()
    restarted = Flow(tmp_path, flow.clock, flow.sink)
    try:
        restarted.refresh()
        assert restarted.get("/v1/workspace")["candidate_assessments"] == answer["candidate_assessments"]
        assert restarted.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        restarted.close()


def test_v12_candidate_waits_for_trigger_then_retest_and_current_money(flow, monkeypatch):
    original = flow.w.market._bridge.request
    quote = {"bid": "99.98", "ask": "100.00"}

    def source(operation, params):
        response = original(operation, params)
        if operation == "snapshot":
            response["result"]["market"]["quote"].update(quote)
        return response

    monkeypatch.setattr(flow.w.market._bridge, "request", source)
    def waiting(answer):
        answer["plans"][0]["activation"] = {"kind": "quote_compare", "quote_side": "bid", "operator": "gte", "price": "100.20"}
        # The breakout must be ABOVE the retest zone, as in the actual imported plan.
        answer["plans"][0]["entry"]["zone"]["high"] = "100.10"
    req, raw = flow.initial(mutate=waiting)
    assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "WAITING"
    flow.w.guidance.tick()
    assert not flow.get("/v1/workspace")["notifications"]
    quote.update(bid="100.20", ask="100.22")
    flow.clock.value = "2026-10-12T02:00:03Z"
    flow.refresh()
    ws = flow.get("/v1/workspace")
    assert ws["plans"][0]["lifecycle"] == "ACTIVE"
    assert ws["action_advice"][0]["state"] == "WAITING"
    flow.w.guidance.tick()
    assert not flow.get("/v1/workspace")["notifications"]
    quote.update(bid="99.98", ask="100.00")
    flow.clock.value = "2026-10-12T02:00:04Z"
    flow.refresh()
    ws = flow.get("/v1/workspace")
    assert ws["action_advice"][0]["state"] == "READY", ws["action_advice"]
    flow.w.guidance.tick()
    assert len([n for n in flow.get("/v1/workspace")["notifications"] if n["kind"] == "entry"]) == 1
    assert flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw


def test_staged_invalidation_survives_waiting_and_restart_then_invalidates_after_activation(tmp_path, monkeypatch):
    f = Flow(tmp_path)
    quote = {"bid":"99.80", "ask":"99.82"}
    def connect(current):
        original = current.w.market._bridge.request
        def source(operation, params):
            response = original(operation, params)
            if operation == "snapshot": response["result"]["market"]["quote"].update(quote)
            return response
        monkeypatch.setattr(current.w.market._bridge, "request", source)
    def candidate(answer):
        answer["plans"][0].update(
            activation={"kind":"quote_compare","quote_side":"bid","operator":"gte","price":"100.20"},
            post_activation_invalidation={"kind":"quote_compare","quote_side":"bid","operator":"lt","price":"99.90"})
    try:
        connect(f)
        req, raw = f.initial(mutate=candidate)
        assert f.get("/v1/workspace")["plans"][0]["lifecycle"] == "WAITING_TRIGGER"
        saved_clock, saved_sink = f.clock, f.sink
        f.close()
        f = Flow(tmp_path, saved_clock, saved_sink)
        connect(f)
        quote.update(bid="100.20", ask="100.22")
        f.clock.value = "2026-10-12T02:00:03Z"
        f.refresh()
        assert f.get("/v1/workspace")["action_advice"][0]["state"] == "WAITING"
        quote.update(bid="99.98", ask="100.00")
        f.clock.value = "2026-10-12T02:00:04Z"
        f.refresh()
        assert f.get("/v1/workspace")["action_advice"][0]["state"] == "READY"
        quote.update(bid="99.80", ask="99.82")
        f.clock.value = "2026-10-12T02:00:05Z"
        f.refresh()
        ws = f.get("/v1/workspace")
        assert ws["plans"][0]["invalidation_phase"] == "post_activation"
        assert ws["action_advice"][0]["state"] == "INVALIDATED"
        assert f.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        f.close()


def test_actual_late_import_keeps_raw_but_blocks_short_window_across_restart(flow, tmp_path):
    flow.configure()
    req = flow.request(flow.refresh())
    original_packet = req["packet_text"]
    answer = flow.answer(req)
    answer["plans"][0]["activation"] = {"kind":"bar_close","timeframe":"M5","operator":"gt","price":"101"}
    flow.clock.value = "2026-10-12T02:22:00Z"
    job, raw = flow.submit(req, answer)
    assert job["state"] == "succeeded", job
    flow.refresh()
    ws = flow.get("/v1/workspace")
    a = ws["action_advice"][0]
    assert a["state"] == "REFRESH_REQUIRED" and a["entry_timing"]["remaining_at_import_seconds"] == 480
    assert a["entry_timing"]["state"] == "INSUFFICIENT"
    assert a["entry_timing"]["analysis_fresh_until"] == "2026-10-12T02:30:00Z"
    assert ws["plans"][0]["definition"]["entry_deadline"] == answer["plans"][0]["entry_deadline"]
    flow.w.guidance.tick()
    assert not any(n["kind"] == "entry" for n in flow.get("/v1/workspace")["notifications"])
    next_req = flow.request(ws["market"])
    packet_facts = json.JSONDecoder().raw_decode(next_req["packet_text"].split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]
    assert packet_facts['timing_reference']['planning_delay_seconds'] == 1320
    assert flow.get("/v1/analysis/requests/" + req["request_id"])["packet_text"] == original_packet
    flow.close()
    restarted = Flow(tmp_path, flow.clock, flow.sink)
    try:
        assert restarted.get("/v1/workspace")["action_advice"][0]["state"] == "REFRESH_REQUIRED"
        assert restarted.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        restarted.close()


@pytest.mark.parametrize("side", ["long", "short"])
def test_retest_beyond_zone_survives_restart_then_checks_and_notifies_once(tmp_path, monkeypatch, side):
    flow = Flow(tmp_path)
    quote = {"bid":"99.98", "ask":"100.00"}

    def connect(current):
        original = current.w.market._bridge.request
        def source(operation, params):
            response = original(operation, params)
            if operation == "snapshot": response["result"]["market"]["quote"].update(quote)
            return response
        monkeypatch.setattr(current.w.market._bridge, "request", source)

    def candidate(answer):
        p = answer["plans"][0]
        p.update(side=side, stop_loss="99" if side == "long" else "101", take_profit="103" if side == "long" else "97")
        p["activation"] = {"kind":"quote_compare", "quote_side":"bid" if side == "long" else "ask",
            "operator":"gte" if side == "long" else "lte", "price":"100.20" if side == "long" else "99.80"}
        p["invalidation"] = {"kind":"quote_compare", "quote_side":"bid" if side == "long" else "ask",
            "operator":"lt" if side == "long" else "gt", "price":p["stop_loss"]}
        p["entry_checks"] = [{"kind":"quote_compare", "quote_side":"ask" if side == "long" else "bid",
            "operator":"lte" if side == "long" else "gte", "price":"100.00"}]

    try:
        connect(flow)
        req, raw = flow.initial(mutate=candidate)
        quote.update(bid="100.20" if side == "long" else "99.78", ask="100.22" if side == "long" else "99.80")
        flow.clock.value = "2026-10-12T02:00:03Z"
        flow.refresh(); flow.w.guidance.tick()
        ws = flow.get("/v1/workspace")
        assert ws["plans"][0]["lifecycle"] == "ACTIVE"
        assert ws["action_advice"][0]["state"] == "WAITING"
        assert "等待回踩" in ws["action_advice"][0]["title"]
        assert ws["notifications"] == []
        old_clock, old_sink = flow.clock, flow.sink
        flow.close()
        flow = Flow(tmp_path, old_clock, old_sink)
        connect(flow)
        flow.clock.value = "2026-10-12T02:00:04Z"
        flow.refresh(); flow.w.guidance.tick()
        assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "WAITING"
        assert flow.get("/v1/workspace")["notifications"] == []
        quote.update(bid="99.98" if side == "long" else "100.00", ask="100.00" if side == "long" else "100.02")
        flow.clock.value = "2026-10-12T02:00:05Z"
        flow.refresh(); flow.w.guidance.tick()
        ws = flow.get("/v1/workspace")
        assert ws["action_advice"][0]["state"] == "READY", ws["action_advice"]
        assert ws["evaluations"][0]["margin_check"]["state"] == "satisfied"
        assert len([n for n in ws["notifications"] if n["kind"] == "entry"]) == 1
        flow.w.guidance.tick()
        assert len([n for n in flow.get("/v1/workspace")["notifications"] if n["kind"] == "entry"]) == 1
        assert flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        flow.close()


def test_previously_persisted_missed_is_not_rearmed_by_retest_fix(flow, tmp_path):
    req, raw = flow.initial()
    advice = flow.get("/v1/workspace")["action_advice"][0]
    advice.update(state="MISSED", title="合成旧记录：已错过", reasons=["保留旧判断，不自动恢复机会。"])
    with flow.w.db.write() as uow:
        flow.w.plans.remember_action(advice, uow=uow)
    flow.close()
    restarted = Flow(tmp_path, flow.clock, flow.sink)
    try:
        restarted.clock.value = "2026-10-12T02:00:04Z"
        restarted.refresh(); restarted.w.guidance.tick()
        assert restarted.get("/v1/workspace")["action_advice"][0]["state"] == "MISSED"
        assert not any(n["kind"] == "entry" for n in restarted.get("/v1/workspace")["notifications"])
        assert restarted.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
    finally:
        restarted.close()


def test_v12_new_request_rejects_missing_mode_and_downgrade_but_preserves_attempt(flow):
    req = flow.request(flow.refresh())
    for defect in ("missing", "downgrade"):
        answer = flow.answer(req)
        if defect == "missing": answer["candidate_assessments"].pop()
        else:
            answer["schema_version"] = "1.1.0"
            answer.pop("candidate_assessments")
            answer.pop("experience_application")
            answer.pop("prediction_adjustments")
            answer["plans"][0].pop("post_activation_invalidation")
            answer["plans"][0].pop("holding_time_basis", None)
            for horizon in answer["horizons"]:
                horizon.pop("forecast_basis", None)
            for plan in answer["plans"]:
                for rule in plan["entry_checks"]:
                    rule.pop("observation", None)
                    rule.pop("purpose", None)
        job, raw = flow.submit(req, answer)
        assert job["state"] == "failed"
        assert job["error"]["code"] == ("CANDIDATE_COVERAGE_INVALID" if defect == "missing" else "SCHEMA_INVALID")
        assert any(a.to_dict()["raw_text"] == raw for a in flow.w.analysis.list_import_attempts(req["request_id"]))
    assert flow.get("/v1/workspace")["current_forecast"] is None


def test_stale_answer_import_never_resets_analysis_clock(flow):
    flow.configure()
    req = flow.request(flow.refresh())
    answer = flow.answer(req)
    flow.clock.value = "2026-10-12T02:31:00Z"
    job, raw = flow.submit(req, answer)
    assert job["state"] == "succeeded"
    flow.refresh()
    ws = flow.get("/v1/workspace")
    scalp = next(r for r in ws["analysis_refresh"] if r["trading_style"] == "scalp")
    assert scalp["fresh_until"] == "2026-10-12T02:30:00Z" and scalp["state"] == "STALE"
    assert ws["action_advice"][0]["state"] == "EXPIRED"
    assert flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw


def test_early_reminder_creates_one_frozen_packet_and_old_packet_stays(flow):
    req, _raw = flow.initial()
    original = flow.get("/v1/analysis/requests/" + req["request_id"])["packet_text"]
    flow.clock.value = "2026-10-12T02:29:01Z"
    flow.refresh()
    flow.w.guidance.tick()
    ws = flow.get("/v1/workspace")
    scalp = next(r for r in ws["analysis_refresh"] if r["trading_style"] == "scalp")
    assert scalp["state"] == "DUE" and scalp["pending_request_id"]
    pending = flow.get("/v1/analysis/requests/" + scalp["pending_request_id"])
    assert pending["market_cutoff"] == flow.clock.value and pending["kind"] == "new"
    assert 30 in pending["horizon_minutes"]
    flow.w.guidance.tick()
    assert len(flow.get("/v1/analysis/requests?state=awaiting_answer")["items"]) == 1
    assert flow.get("/v1/analysis/requests/" + req["request_id"])["packet_text"] == original
    assert 'previous_analysis' in pending["packet_text"]
    flow.clock.value = "2026-10-12T03:00:00Z"
    flow.refresh()
    flow.w.guidance.tick()
    later = next(r for r in flow.get("/v1/workspace")["analysis_refresh"] if r["trading_style"] == "scalp")
    assert later["pending_request_id"] != pending["request_id"]
    assert flow.get("/v1/analysis/requests/" + pending["request_id"])["packet_text"] == pending["packet_text"]


def test_structured_reassessment_blocks_before_periodic_expiry(flow):
    flow.initial(mutate=lambda a: a["analysis_basis"].update(reassessment_conditions=[
        {"trading_style": "scalp", "condition": {"kind": "quote_compare", "quote_side": "ask", "operator": "gte", "price": "100"}}]))
    ws = flow.get("/v1/workspace")
    assert ws["action_advice"][0]["state"] == "REFRESH_REQUIRED"
    assert next(r for r in ws["analysis_refresh"] if r["trading_style"] == "scalp")["state"] == "REASSESS"


def test_quote_expiry_suppresses_queued_entry_and_withdraws_current_display(flow):
    flow.initial()
    flow.w.guidance.tick()
    flow.clock.value = "2026-10-12T02:00:15Z"
    assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "BLOCKED"
    assert flow.w.guidance.deliver_once()["state"] == "suppressed"
    assert not flow.sink.calls
    flow.w.guidance.tick()
    assert any(n["kind"] == "withdrawal" for n in flow.get("/v1/workspace")["notifications"])


def test_two_styles_same_time_only_one_ready(flow):
    def both(a):
        second = copy.deepcopy(a["plans"][0])
        second.update(plan_key="intraday", role="backup", trading_style="intraday", max_holding_minutes=120)
        a["plans"].append(second)
        a["candidate_assessments"][1].update(outcome="candidate", plan_key="intraday", missing_conditions=[])
    flow.initial(mutate=both)
    states = [a["state"] for a in flow.get("/v1/workspace")["action_advice"]]
    assert states.count("READY") == 1 and states.count("BLOCKED") == 1


def test_settings_change_during_external_margin_read_cannot_reuse_old_fact(flow, monkeypatch):
    flow.initial()
    original = flow.w.market.calculation
    changed = False

    def calculate(operation, values, settings):
        nonlocal changed
        result = original(operation, values, settings)
        if not changed:
            before = flow.w.settings.get_snapshot().to_dict()
            new_values = copy.deepcopy(before["values"])
            new_values["min_net_rr"] = "1"
            flow.w.settings.update_settings({"expected_revision": before["revision"], "values": new_values})
            changed = True
        return result

    monkeypatch.setattr(flow.w.market, "calculation", calculate)
    flow.clock.value = "2026-10-12T02:00:03Z"
    flow.refresh()
    ws = flow.get("/v1/workspace")
    assert ws["evaluations"][0]["margin_check"]["state"] == "unknown"
    assert ws["action_advice"][0]["state"] == "BLOCKED"


@pytest.mark.parametrize("mutation,code", [
    (lambda a: a.pop("analysis_basis"), "SCHEMA_INVALID"),
    (lambda a: a["plans"][0]["entry"].update(zone=None), "SCHEMA_INVALID"),
    (lambda a: a["plans"][0].update(max_holding_minutes=120), "HOLDING_STYLE_INVALID"),
    (lambda a: a["plans"][0].update(activation={"kind": "manual_observation", "instruction": "等企稳"}), "AUTOMATIC_RULE_REQUIRED"),
])
def test_guided_import_rejects_non_deterministic_or_ambiguous_requirements(flow, mutation, code):
    req = flow.request(flow.refresh())
    answer = flow.answer(req)
    mutation(answer)
    job, raw = flow.submit(req, answer)
    assert job["state"] == "failed" and job["error"]["code"] == code, job
    attempts = flow.w.analysis.list_import_attempts(req["request_id"])
    assert attempts[0].to_dict()["raw_text"] == raw


def captured_order(flow, *, kind="position", ticket="used-ticket"):
    """Synthetic broker facts; no MT5 connection or trade command."""
    pf = flow.w.portfolio.get_portfolio().to_dict()
    pf["snapshot_id"] = new_id()
    order = {"order": {"account_scope_id": pf["account_scope_id"], "kind": kind, "broker_id": ticket},
             "symbol": "XAUUSD", "side": "long", "volume_lots": "0.01", "entry_price": "100",
             "stop_loss": "99", "take_profit": "103", "event_time": flow.clock.value,
             "captured_at": flow.clock.value, "plan_id": None}
    pf["orders"] = [order]
    flow.w.portfolio.sync_facts(pf)
    return order["order"]


def associate(flow, order, plan_id, action="link"):
    return flow.post("/v1/portfolio/associations", {"order": order, "plan_id": plan_id, "action": action,
        "reason": "合成核验明确关联", "expected_portfolio_revision": flow.w.portfolio.get_portfolio().to_dict()["revision"]})


@pytest.mark.parametrize("style", ["scalp", "intraday"])
def test_once_used_plan_never_rearms_after_flat_unlink_and_restart(flow, tmp_path, style):
    req, raw = flow.initial(style)
    plan_id = flow.get("/v1/workspace")["action_advice"][0]["plan_id"]
    flow.w.guidance.tick()  # Leave the original READY notice queued.
    ref = captured_order(flow)
    associate(flow, ref, plan_id)
    assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "USED"
    assert flow.w.guidance.deliver_once()["state"] == "suppressed"
    associate(flow, ref, plan_id, "unlink")
    flow.clock.value = "2026-10-12T02:00:04Z"
    flow.refresh()  # Same prices and conditions, now flat again.
    assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "USED"
    flow.w.guidance.tick()
    assert not [n for n in flow.sink.calls if n["kind"] == "entry"]
    flow.close()
    restarted = Flow(tmp_path, flow.clock, flow.sink)
    try:
        restarted.clock.value = "2026-10-12T02:00:06Z"
        restarted.refresh()
        ws = restarted.get("/v1/workspace")
        assert ws["action_advice"][0]["state"] == "USED"
        assert "不再入场" in ws["action_advice"][0]["title"]
        assert restarted.get("/v1/forecasts/" + req["forecast_id"])["current_version"]["raw_text"] == raw
        assert len([n for n in ws["notifications"] if n["kind"] == "entry"]) == 1
    finally:
        restarted.close()


def test_unlinked_execution_stays_uncertain_after_flat_instead_of_ready(flow):
    flow.initial()
    captured_order(flow)
    flow.clock.value = "2026-10-12T02:00:04Z"
    flow.refresh()
    advice = flow.get("/v1/workspace")["action_advice"][0]
    assert advice["state"] == "BLOCKED"
    assert "成交归属待核对" in advice["title"]


def test_cancelled_unfilled_pending_order_does_not_consume_plan(flow):
    flow.initial()
    plan_id = flow.get("/v1/workspace")["action_advice"][0]["plan_id"]
    ref = captured_order(flow, kind="pending_order")
    associate(flow, ref, plan_id)
    flow.clock.value = "2026-10-12T02:00:04Z"
    flow.refresh()
    assert flow.get("/v1/workspace")["action_advice"][0]["state"] == "READY"


def test_consumption_during_guidance_read_cannot_enqueue_old_ready(flow, monkeypatch):
    flow.initial()
    plan_id = flow.get("/v1/workspace")["action_advice"][0]["plan_id"]
    original = flow.w.guidance.snapshot

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        ref = captured_order(flow)
        associate(flow, ref, plan_id)
        return result

    monkeypatch.setattr(flow.w.guidance, "snapshot", changed)
    flow.w.guidance.tick()
    monkeypatch.setattr(flow.w.guidance, "snapshot", original)
    ws = flow.get("/v1/workspace")
    assert ws["action_advice"][0]["state"] == "USED"
    assert not [n for n in ws["notifications"] if n["kind"] == "entry"]
