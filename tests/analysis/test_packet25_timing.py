"""25 timing budgets and ordinary runtime; no model network or real keys."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, post, run, execute
from tests.analysis.test_analysis_lab import add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.entry_schedule import build_schedule, project_schedule, explain_entry_timing
from xau_next.domains.analysis.public import build_packet, outbound_packet, frozen_answer_schema
from xau_next.shared.entry_timing import planned_timing
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc


def timing_case():
    r = {"request_id": "SYNTHETIC-q", "forecast_id": "SYNTHETIC-f", "market_snapshot_id": "SYNTHETIC-s",
         "market_cutoff": "2026-10-28T07:09:04.739Z", "created_at": "2026-10-28T07:09:08.776Z",
         "window_end": "2026-10-28T10:09:04.739Z", "answer_schema_version": "1.7.0",
         "analysis_preferences": {"enabled_styles": ["scalp", "intraday"], "scalp_refresh_minutes": 30,
                                  "intraday_refresh_minutes": 120}}
    c = {"timing_reference": {"policy_version": "entry-observation/2", "planning_delay_seconds": 393}}
    p = {"trading_style": "scalp", "valid_from": "2026-10-28T07:16:00Z",
         "entry_deadline": "2026-10-28T07:29:04.739Z", "entry_checks": [],
         "activation": {"kind": "bar_close", "timeframe": "M5"}, "entry": {"mode": "limit_retest"}}
    return r, c, p


def facts(text):
    return json.JSONDecoder().raw_decode(text.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def prepare25(f, models="A", kind="new", base=None):
    bundle = add_bundle(f, multi())
    with f.w.db.read() as tx:
        market = f.w.market.latest(scope=tx).to_dict()
    result = post(f, "/v1/analysis/daily/groups", {"package_kind": "candidate25", "models": list(models),
        "kind": kind, "base_group_id": base, "bundle_id": bundle["bundle_id"],
        "market_snapshot_id": market["snapshot_id"], "horizon_minutes": [30, 60, 180] if kind == "new" else None})
    assert result.status_code == 201, result.text
    return result.json()


def test_schedule_contains_concrete_deadlines_and_original_hard_limits():
    r, c, _ = timing_case()
    t = build_schedule(r, c)
    assert t["estimated_available_at"] == "2026-10-28T07:15:41.776000Z"
    assert t["style_windows"] == [
        {"trading_style": "scalp", "max_entry_deadline": "2026-10-28T07:39:04.739000Z"},
        {"trading_style": "intraday", "max_entry_deadline": "2026-10-28T09:09:04.739000Z"}]
    assert len(t["cases"]) == 48
    row = next(x for x in t["cases"] if x["timeframe"] == "M5" and x["entry_mode"] == "limit_retest" and x["extra_closed_check"] is None)
    assert row["minimum_entry_deadline"] == "2026-10-28T07:30:00Z"
    assert row["post_trigger_reserve_seconds"] == 300
    # M15 trigger: first07:30, second07:45, retest allowance15min ->08:00.
    row = next(x for x in t["cases"] if x["timeframe"] == "M15" and x["entry_mode"] == "limit_retest" and x["extra_closed_check"] is None)
    assert row["minimum_entry_deadline"] == "2026-10-28T08:00:00Z"
    assert "延后生效" in t["assumptions"]


@pytest.mark.parametrize("created", ["2026-10-28T07:08:26.999Z", "2026-10-28T07:08:27Z", "2026-10-28T07:08:27.001Z"])
def test_all_table_combinations_match_original_policy_across_close_boundary(created):
    r, c, p = timing_case()
    r["created_at"] = created  # +393seconds straddles07:15:00 exactly.
    for row in build_schedule(r, c)["cases"]:
        activation = {"kind": row["activation_kind"]}
        if row["timeframe"]:
            activation["timeframe"] = row["timeframe"]
        checks = [{"kind": "bar_close", "timeframe": row["extra_closed_check"], "observation": "new_since_available"}] if row["extra_closed_check"] else []
        plan = {**p, "valid_from": r["market_cutoff"], "activation": activation,
                "entry": {"mode": row["entry_mode"]}, "entry_checks": checks}
        assert planned_timing(plan, r, c)["minimum_entry_deadline"] == row["minimum_entry_deadline"]


def test_diagnostics_preserve_failed_answer_precise_gap_and_later_start():
    r, c, p = timing_case()
    answer = {**{k: r[k] for k in ["request_id", "forecast_id", "market_snapshot_id", "market_cutoff"]}, "plans": [p]}
    original = deepcopy((r, c, answer))
    assert explain_entry_timing(answer, r, c)[0]["shortfall_seconds"] == "55.261"
    p["entry_deadline"] = "2026-10-28T07:29:00Z"
    assert explain_entry_timing(answer, r, c)[0]["shortfall_seconds"] == "60.0"
    p["entry_deadline"] = "2026-10-28T07:29:59.999Z"
    assert explain_entry_timing(answer, r, c)[0]["shortfall_seconds"] == "0.001"
    p["entry_deadline"] = "2026-10-28T07:30:00Z"
    assert explain_entry_timing(answer, r, c) == []
    p["valid_from"] = "2026-10-28T07:20:00.001Z"
    assert explain_entry_timing(answer, r, c)[0]["minimum_entry_deadline"] == "2026-10-28T07:35:00Z"
    # Caller owns these intentional synthetic changes; helper never edits them.
    before = deepcopy((r, c, answer))
    explain_entry_timing(answer, r, c)
    assert (r, c, answer) == before
    assert original[2]["plans"][0]["entry_deadline"] == "2026-10-28T07:29:04.739Z"


def test_window_cap_multiple_checks_and_wrong_identity_stay_explicit():
    r, c, p = timing_case()
    r["window_end"] = "2026-10-28T07:25:00Z"
    assert all(w["max_entry_deadline"] == r["window_end"] for w in build_schedule(r, c)["style_windows"])
    p["activation"] = {"kind": "immediate"}
    p["entry_checks"] = [{"kind": "bar_close", "timeframe": x} for x in ["M1", "M15"]]
    p["entry_deadline"] = "2026-10-28T07:40:00Z"
    a = {**{k: r[k] for k in ["request_id", "forecast_id", "market_snapshot_id", "market_cutoff"]}, "plans": [p]}
    row = explain_entry_timing(a, r, c)[0]
    assert row["minimum_entry_deadline"] == "2026-10-28T07:31:00Z"
    assert row["exceeds_seconds"] == "900.0"
    a["request_id"] = "wrong"
    assert explain_entry_timing(a, r, c) == []


def test_schedule_projection_drops_nested_extensions_and_rejects_unknown_revision():
    r, c, _ = timing_case()
    table = build_schedule(r, c)
    expected = deepcopy(table)
    for node in [table, *table["cases"], *table["style_windows"]]:
        node["private_extension"] = "DO_NOT_SEND"
    assert project_schedule(table) == expected
    table["version"] = "unknown"
    with pytest.raises(DomainError):
        project_schedule(table)


@pytest.mark.parametrize("models", ["A", "ABCD"])
def test25_prepare_and_outbound_keep24_facts_schema_and_no_credential_access(flow, monkeypatch, models):
    monkeypatch.setattr(flow.credentials, "read", lambda *_: pytest.fail("Preparation accessed key"))
    g = prepare25(flow, models)
    assert g["template_version"] == "manual-analysis/25" and not g["run_registered"]
    for arm in g["arms"]:
        with flow.w.db.read() as tx:
            q = flow.w.analysis.model_request(arm["request_id"], scope=tx)["request"]
        before = deepcopy(q)
        raw, api = facts(q["packet_text"]), facts(outbound_packet(q["packet_text"]))
        assert api["entry_timing_reference"] == raw["entry_timing_reference"]
        assert api["forecast_deliberation"] == raw["forecast_deliberation"]
        base = {k: v for k, v in q.items() if k not in {"context", "packet_text", "id", "revision", "recorded_at"}}
        base["prompt_version"] = "manual-analysis/24"
        base_text = build_packet(base, q["context"], frozen_answer_schema(q))
        for x, y in [(facts(base_text), raw), (facts(outbound_packet(base_text)), api)]:
            y = deepcopy(y); y.pop("entry_timing_reference")
            y["request"]["prompt_version"] = "manual-analysis/24"
            assert x == y
        assert q == before and len(q["packet_text"]) <= 250000
    assert not flow.sent


def test25_simulated_import_update_and_history_keep_identity_and_template(flow):
    g = prepare25(flow, "AB")
    assert run(flow, g["group_id"]).status_code == 202
    execute(flow, 2)
    saved = flow.get("/v1/analysis/daily/groups/" + g["group_id"])
    assert all(a["state"] == "succeeded" and a["model_call"]["outbound_transform_version"] == "api-projection/25-timing1" for a in saved["arms"])
    flow.clock.value = (parse_utc(flow.clock.value) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    flow.refresh()
    update = prepare25(flow, "AB", "update", g["group_id"])
    assert update["window_end"] == g["window_end"] and update["template_version"] == "manual-analysis/25"
    assert [a["forecast_id"] for a in update["arms"]] == [a["forecast_id"] for a in g["arms"]]
    assert run(flow, update["group_id"]).status_code == 202
    execute(flow, 2)
    assert all(a["state"] == "succeeded" for a in flow.get("/v1/analysis/daily/groups/" + update["group_id"])["arms"])
    assert any(r["template_version"] == "manual-analysis/25" for r in flow.get("/v1/analysis/daily/performance?data_kind=SYNTHETIC")["rows"])
    assert not flow.sent


def test_old_failed24_get_adds_diagnostics_without_writing_or_reading_keys(flow, monkeypatch):
    from tests.analysis.test_packet24_runtime import prepare24
    g = prepare24(flow, "A")
    run(flow, g["group_id"])
    with flow.w.db.write() as tx:
        q = flow.w.analysis.model_request(g["arms"][0]["request_id"], scope=tx)["request"]
        p = {"trading_style": "scalp", "valid_from": q["market_cutoff"],
             "entry_deadline": q["market_cutoff"], "entry_checks": [],
             "activation": {"kind": "bar_close", "timeframe": "M5"}, "entry": {"mode": "limit_retest"}}
        answer = {**{k: q[k] for k in ["request_id", "forecast_id", "market_cutoff", "market_snapshot_id"]}, "plans": [p]}
        call = flow.w.model_api.calls.latest(q["request_id"], scope=tx)
        raw = flow.w.model_api.calls.prepare(json.dumps(answer).encode())
        ref = flow.w.model_api.calls.register(raw, uow=tx)
        flow.w.model_api.calls.update(call["call_id"], {"state": "failed", "answer": ref,
            "transport_succeeded": True, "answer_valid": False, "error_code": "ENTRY_WINDOW_INSUFFICIENT"}, uow=tx)
    with flow.w.db.read() as tx:
        before = deepcopy(flow.w.model_api.calls.get(call["call_id"], scope=tx, internal=True))
    monkeypatch.setattr(flow.credentials, "read", lambda *_: pytest.fail("Read-only detail accessed key"))
    result = flow.get("/v1/analysis/daily/groups/" + g["group_id"])
    arm = result["arms"][0]
    assert arm["state"] == "failed" and arm["version_id"] is None
    assert float(arm["entry_timing_diagnostics"][0]["shortfall_seconds"]) > 0
    with flow.w.db.read() as tx:
        assert flow.w.model_api.calls.get(call["call_id"], scope=tx, internal=True) == before
    assert not flow.sent


def test25_429_is_recorded_once_no_resend_or_channel_switch(flow, monkeypatch):
    import httpx
    from xau_next.platform.model_gateway.public import Gateway
    sent = []
    def receive(request):
        sent.append(request.url)
        return httpx.Response(429, json={"error": {"message": "SYNTHETIC upstream rate limit exceeded", "type": "rate_limit_error"}})
    monkeypatch.setattr("xau_next.workflows.model_api.simulation_gateway", lambda _: Gateway(httpx.MockTransport(receive)))
    g = prepare25(flow)
    assert run(flow, g["group_id"]).status_code == 202
    execute(flow, 1)
    arm = flow.get("/v1/analysis/daily/groups/" + g["group_id"])["arms"][0]
    assert arm["state"] == "failed" and arm["model_call"]["error_code"] == "MODEL_RATE_LIMITED"
    assert not arm["entry_timing_diagnostics"] and arm["model_call"]["answer"] is None
    assert len(sent) == 1 and not flow.w.model_api.process_once(flow.w, kind="daily_model_call")
    assert run(flow, g["group_id"]).status_code == 409 and len(sent) == 1
    assert not flow.sent
