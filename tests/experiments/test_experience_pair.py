"""SYNTHETIC HTTP transport and isolated SQLite: behavior only, never forecast efficacy."""

import copy
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from xau_next.platform.model_gateway.public import Gateway, GatewayResult
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc
from xau_next.workflows.experience_experiment import (
    create_plan,
    digest,
    freeze_pair,
    load_pair,
    make_plan,
    prompt_facts,
    read,
    run_pair,
)
from xau_next.workflows.experience_scoring import report, score_pair, score_window

from tests.model_api.test_experience_projection import synthetic_experience
from tests.model_api.test_gateway import envelope
from tests.model_api.test_workflow import ModelFlow

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    # This suite exercises the legacy 1.6/11 experiment contract, not the
    # current product prompt. Construct that original schema from the outset;
    # never relabel a frozen 1.7 packet to bypass the version gate.
    from xau_next.domains.analysis import service as analysis_service
    monkeypatch.setattr(analysis_service, "ANSWER_SCHEMA_VERSION", "1.6.0")
    monkeypatch.setattr(analysis_service, "PROMPT_VERSION", "manual-analysis/11")
    flow = ModelFlow(tmp_path / "db")
    flow.configure()
    flow.setup_model()
    req = flow.request(flow.refresh())
    with flow.w.db.read() as tx:
        base = flow.w.db.documents("analysis").get(tx, "request", req["request_id"])
    exp = synthetic_experience()
    exp["review_refs"].append(req["market_snapshot_id"])  # shared current + historical reference
    context = base["context"]
    req = flow.w.analysis.create_request(
        {
            "kind": "new",
            "forecast_id": None,
            "base_version_id": None,
            "market_snapshot_id": req["market_snapshot_id"],
            "horizon_minutes": req["horizon_minutes"],
        },
        market=Snapshot.of(context["market"]),
        portfolio=Snapshot.of(context["portfolio"]),
        settings=Snapshot.of(context["settings"]),
        experience=Snapshot.of(exp),
        prompt_version="manual-analysis/11",
    ).to_dict()
    with flow.w.db.read() as tx:
        source = flow.w.db.documents("analysis").get(tx, "request", req["request_id"])
    config = flow.w.model_api.settings.snapshot()["values"]
    plan = make_plan(
        config,
        pairs=3,
        horizons=req["horizon_minutes"],
        cohort="synthetic",
        call_budget=6,
        accept_unknown_cost=True,
        source_root=flow.w.db.data_root,
        clock=flow.clock,
    )
    path = tmp_path / "experiment"
    create_plan(path, plan)
    freeze_pair(path, "pair-0001", source, clock=flow.clock)
    yield flow, path, plan, source
    flow.close()


def answer_for(body):
    prompt = body.get("input") or body["messages"][0]["content"]
    facts = prompt_facts(prompt)
    req = facts["request"]
    answer = json.loads((ROOT / "fixtures/analysis-answer.experience.json").read_text(encoding="utf-8"))
    answer["schema_version"] = req["answer_schema_version"]
    for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        answer[key] = req[key]

    def stamp(n):
        return (parse_utc(req["window_start"]) + timedelta(minutes=n)).isoformat().replace("+00:00", "Z")

    answer["horizons"] = [
        {
            "horizon_minutes": n,
            "end_at": stamp(n),
            "direction": "long" if facts["experience"] else "short",
            "expected_range": {"low": "98", "high": "104"},
            "phases": [
                {
                    "start_at": req["market_cutoff"],
                    "end_at": stamp(n),
                    "direction": "long",
                    "note": "SYNTHETIC",
                }
            ],
            "turning_points": [],
            "confidence": None,
        }
        for n in req["horizon_minutes"]
    ]
    answer.update(plans=[], order_advice=[], evidence_refs=[req["market_snapshot_id"]])
    if req["answer_schema_version"] == "1.6.0":
        for horizon in answer["horizons"]:
            horizon["forecast_basis"] = {"direction_basis":"SYNTHETIC 端点变化依据", "range_basis":"SYNTHETIC 上下界依据",
                "supporting_evidence":["SYNTHETIC 当前结构"], "opposing_evidence":["SYNTHETIC 反向路径"],
                "evidence_refs":[req["market_snapshot_id"]], "limitations":["没有真实效力证据。"]}
    for c in answer["candidate_assessments"]:
        c.update(
            outcome="no_candidate",
            plan_key=None,
            rationale="SYNTHETIC，不提供方案。",
            missing_conditions=["合成流程测试。"],
        )
    if facts["experience"]:
        answer["experience_application"].update(
            digest_id=facts["experience"]["digest_id"], status="reviewed_no_change"
        )
    return answer


def execute(experiment, handler=None):
    flow, path, plan, _ = experiment
    seen = []

    def receive(req):
        seen.append(req.content)
        flow.clock.value = (
            (parse_utc(flow.clock.value) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        )
        answer = answer_for(json.loads(req.content))
        return handler(req, answer) if handler else httpx.Response(200, json=envelope(json.dumps(answer)))

    results = run_pair(
        path,
        "pair-0001",
        calls=flow.w.model_api.calls,
        settings=flow.w.model_api.settings,
        gateway=Gateway(httpx.MockTransport(receive)),
        acknowledge_plan_sha256=digest(plan),
        clock=flow.clock,
    )
    return results, seen


def actual_market(source):
    market = copy.deepcopy(source["context"]["market"])
    start = parse_utc(source["market_cutoff"]).replace(second=0, microsecond=0)

    def stamp(at):
        return at.isoformat().replace("+00:00", "Z")

    market.update(snapshot_id="SYNTHETIC-actual-path", market_cutoff=stamp(start + timedelta(hours=13)))
    market["series"] = [
        {
            "timeframe": "M1",
            "price_basis": "bid",
            "status": {"state": "available", "reasons": []},
            "bars": [
                {
                    "open_at": stamp(start + timedelta(minutes=i)),
                    "close_at": stamp(start + timedelta(minutes=i + 1)),
                    "open": "100",
                    "high": "102",
                    "low": "99",
                    "close": "101",
                    "closed": True,
                    "tick_volume": 1,
                }
                for i in range(782)
            ],
        }
    ]
    return market


def test_final_http_bodies_only_change_experience_and_exclusive_refs(experiment):
    flow, path, plan, source = experiment
    before = digest(source)
    results, seen = execute(experiment)
    assert all(r["state"] == "succeeded" for r in results.values()), results
    assert len(seen) == 2 and digest(source) == before
    a, b = [prompt_facts(json.loads(raw)["input"]) for raw in seen]
    assert a["experience"] == source["context"]["experience"] and b["experience"] is None
    assert source["market_snapshot_id"] in b["evidence_refs"]
    assert source["context"]["experience"]["digest_id"] not in b["evidence_refs"]
    assert a["request"] == b["request"] and a["request"]["request_id"].startswith("experience-exp-")
    for key in a:
        if key not in ("experience", "evidence_refs", "experience_cohort"):
            assert a[key] == b[key]
    assert a['experience_cohort']['case_count']==len(a['experience']['cases'])
    assert b['experience_cohort']['case_count']==0
    assert seen[0] == (path / "pair-0001/A-body.json").read_bytes()
    assert seen[1] == (path / "pair-0001/B-body.json").read_bytes()
    assert plan["samples"][1]["order"] == ["B", "A"]
    with flow.w.db.read() as tx:
        assert (
            flow.w.model_api.calls.docs.get(tx, "model_daily_reservation", "2026-10-12")["count"] == 3
        )  # 1 synthetic probe + 2 arms
        assert len(flow.w.model_api.calls.docs.list(tx, "experience_experiment_call")) == 2
        for owner, kind in [
            ("analysis", "version"),
            ("analysis", "forecast"),
            ("plans", "plan"),
            ("review", "review"),
        ]:
            assert flow.w.db.documents(owner).list(tx, kind) == []
    assert flow.get("/v1/workspace")["current_forecast"] is None
    with pytest.raises(FileExistsError):
        execute(experiment)
    _, report_value = report(path, clock=flow.clock)
    assert report_value["registered_pairs"] == 3 and report_value["registered_arms"] == 6
    assert report_value["engineering"]["A"]["registered_denominator"] == 3
    assert report_value["engineering"]["A"]["states"] == {"succeeded": 1, "not_run": 2}


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("missing", "SCHEMA_INVALID"),
        ("reference", "EXPERIENCE_CASE_REF_INVALID"),
        ("refused", "MODEL_REFUSED"),
        ("truncated", "MODEL_OUTPUT_LIMIT"),
    ],
)
def test_failures_are_preserved_stop_other_arm_and_stay_in_denominators(experiment, kind, expected):
    flow, path, _, _ = experiment

    def handler(req, answer):
        if kind == "missing":
            answer.pop("experience_application")
        if kind == "reference":
            answer["experience_application"]["case_assessments"] = [
                {
                    "review_ref": "not-provided",
                    "relevance": "unknown",
                    "current_evidence_refs": [],
                    "similarities": [],
                    "differences": [],
                    "decision": "uncertain",
                    "reason": "未知",
                }
            ]
        if kind == "refused":
            return httpx.Response(
                200,
                json=envelope(
                    output=[{"type": "message", "content": [{"type": "refusal", "refusal": "SYNTHETIC"}]}]
                ),
            )
        if kind == "truncated":
            return httpx.Response(
                200, json=envelope(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            )
        return httpx.Response(200, json=envelope(json.dumps(answer)))

    results, seen = execute(experiment, handler)
    assert results["A"]["error_code"] == expected and len(seen) == 1
    assert results["B"]["state"] == "not_sent"
    _, value = report(path, clock=flow.clock)
    assert value["engineering"]["A"]["registered_denominator"] == 3
    assert value["engineering"]["A"]["experience_errors"] == (1 if kind in {"missing", "reference"} else 0)
    assert value["engineering"]["B"]["states"] == {"not_sent": 1, "not_run": 2}


def test_timeout_unknown_reservation_survives_and_copy_cannot_bypass_claim(experiment, tmp_path):
    import shutil

    flow, path, plan, _ = experiment
    clone = tmp_path / "copied-experiment"
    shutil.copytree(path, clone)

    class TimeoutGateway:
        def exchange(self, *args):
            return GatewayResult(error_code="MODEL_TIMEOUT_UNKNOWN")

    results = run_pair(
        path,
        "pair-0001",
        calls=flow.w.model_api.calls,
        settings=flow.w.model_api.settings,
        gateway=TimeoutGateway(),
        acknowledge_plan_sha256=digest(plan),
        clock=flow.clock,
    )
    assert results["A"]["state"] == "outcome_unknown" and results["A"]["input_tokens"] is None
    retry = run_pair(
        clone,
        "pair-0001",
        calls=flow.w.model_api.calls,
        settings=flow.w.model_api.settings,
        gateway=TimeoutGateway(),
        acknowledge_plan_sha256=digest(plan),
        clock=flow.clock,
    )
    assert retry["A"]["error_code"] == "EXPERIMENT_ALREADY_CLAIMED" and retry["A"]["state"] == "not_sent"
    with flow.w.db.read() as tx:
        assert flow.w.model_api.calls.docs.get(tx, "model_daily_reservation", "2026-10-12")["count"] == 2


@pytest.mark.parametrize("cap", ["plan_calls", "daily_calls", "plan_cost", "missing_budget"])
def test_existing_daily_and_plan_budgets_block_before_http(experiment, cap, tmp_path):
    flow, _, old_plan, source = experiment
    config = flow.w.model_api.settings.snapshot()
    plan = copy.deepcopy(old_plan)
    plan["experiment_id"] += "-" + cap
    if cap == "plan_calls":
        plan["call_budget"] = 1
    if cap == "missing_budget":
        plan["call_budget"] = None
    if cap == "plan_cost":
        plan["cost_budget"] = "0"
        config["values"].update(
            input_price_per_million="1",
            output_price_per_million="1",
            price_currency="USD",
            price_source="SYNTHETIC",
        )
    if cap == "daily_calls":
        config["values"]["daily_call_limit"] = 1
    if cap in {"daily_calls", "plan_cost"}:
        with flow.w.db.write() as tx:
            flow.w.model_api.settings.update(
                {"values": config["values"], "expected_revision": config["revision"]}, uow=tx
            )
        plan["configuration"] = flow.w.model_api.settings.snapshot()["values"]
    path = tmp_path / cap
    create_plan(path, plan)
    freeze_pair(path, "pair-0001", source, clock=flow.clock)
    results, seen = execute((flow, path, plan, source))
    if cap == "plan_calls":
        assert len(seen) == 1 and results["B"]["error_code"] == "EXPERIMENT_CALL_LIMIT"
    else:
        assert seen == [] and results["A"]["state"] == "not_sent"


def test_direction_range_and_three_time_scopes_use_same_original_end(experiment):
    flow, path, _plan, source = experiment
    execute(experiment)
    market = actual_market(source)
    flow.clock.value = "2026-10-12T15:00:00Z"
    score = score_pair(path, "pair-0001", market, as_of=flow.clock.value, clock=flow.clock)
    assert score["common_available_at"] == read(path / "pair-0001/B-result.json")["available_at"]
    for arm in ("A", "B"):
        full = next(
            r for r in score["arms"][arm] if r["horizon_minutes"] == 180 and r["scope"] == "full_descriptive"
        )
        common = next(
            r
            for r in score["arms"][arm]
            if r["horizon_minutes"] == 180 and r["scope"] == "after_common_available"
        )
        metrics = {m["name"]: m["value"] for m in full["metrics"]}
        assert metrics["direction_correct"] == ("1" if arm == "A" else "0")
        assert (
            metrics["lower_boundary_absolute_error"] == "1"
            and metrics["upper_boundary_absolute_error"] == "2"
        )
        assert metrics["range_contained"] == "1" and metrics["predicted_range_width"] == "6"
        assert full["end"] == common["end"] and common["start"] == score["common_available_at"]
    _, value = report(path, clock=flow.clock)
    row = next(
        r
        for r in value["aggregates"]
        if r["arm"] == "A" and r["horizon_minutes"] == 180 and r["scope"] == "full_descriptive"
    )
    assert (
        row["direction_scorable"] == 1
        and row["direction_unscorable"] == 2
        and row["registered_denominator"] == 3
    )
    pair = next(
        r
        for r in value["paired_direction"]
        if r["horizon_minutes"] == 180 and r["scope"] == "full_descriptive"
    )
    assert pair["A_correct_B_wrong"] == 1 and pair["incomplete_pairs"] == 2


@pytest.mark.parametrize("defect", ["gap", "duplicate", "late"])
def test_missing_duplicate_bars_and_no_remaining_window_are_not_fabricated(experiment, defect):
    _flow, path, _, source = experiment
    execute(experiment)
    market = actual_market(source)
    raw = read(path / "pair-0001/A-answer.json")
    original = digest(raw)
    version = {
        "answer": raw,
        "available_at": "2026-10-12T02:01:00Z",
        "version_id": "experiment-version",
        "forecast_id": raw["forecast_id"],
        "baselines": [],
    }
    start = raw["market_cutoff"]
    if defect == "gap":
        market["series"][0]["bars"].pop(4)
    if defect == "duplicate":
        market["series"][0]["bars"].insert(4, copy.deepcopy(market["series"][0]["bars"][4]))
    if defect == "late":
        start = raw["horizons"][0]["end_at"]
    scored = score_window(version, market, 180, start, "2026-10-12T15:00:00Z", "0.00", "after_own_available")
    assert scored["state"] in ("unscorable", "direction_unscorable") and digest(raw) == original
    assert all(m["value"] is None for m in scored["metrics"] if m["name"] == "direction_correct")


def test_repeats_and_overlaps_do_not_expand_independent_sample_count(experiment):
    flow, path, _, source = experiment
    freeze_pair(path, "pair-0002", source, clock=flow.clock)
    _, value = report(path, clock=flow.clock)
    assert value["frozen_pairs"] == 2 and value["overlap_components_upper_bound"] == 1
    assert value["sample_groups"]["pair-0001"] == value["sample_groups"]["pair-0002"]


def test_changed_input_and_missing_authorization_are_rejected(experiment):
    flow, path, _plan, _ = experiment
    with pytest.raises(DomainError, match="主动启动"):
        run_pair(
            path,
            "pair-0001",
            calls=flow.w.model_api.calls,
            settings=flow.w.model_api.settings,
            gateway=flow.gateway,
            acknowledge_plan_sha256="wrong",
            clock=flow.clock,
        )
    target = path / "pair-0001/A-body.json"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(DomainError, match="哈希"):
        load_pair(path, "pair-0001")


def test_second_pair_reverses_http_order_without_sharing_answers(experiment):
    flow, path, plan, source = experiment
    freeze_pair(path, "pair-0002", source, clock=flow.clock)
    seen = []

    def receive(req):
        body = json.loads(req.content)
        facts = prompt_facts(body["input"])
        seen.append(facts["experience"])
        assert "SYNTHETIC-RESPONSE-MARKER" not in req.content.decode()
        answer = answer_for(body)
        answer["summary"] = "SYNTHETIC-RESPONSE-MARKER，无方案。"
        return httpx.Response(200, json=envelope(json.dumps(answer)))

    results = run_pair(
        path,
        "pair-0002",
        calls=flow.w.model_api.calls,
        settings=flow.w.model_api.settings,
        gateway=Gateway(httpx.MockTransport(receive)),
        acknowledge_plan_sha256=digest(plan),
        clock=flow.clock,
    )
    assert all(r["state"] == "succeeded" for r in results.values())
    assert seen[0] is None and seen[1] is not None


def test_configuration_change_between_arms_stops_second_request(experiment):
    flow, _, _, _ = experiment

    def handler(req, answer):
        config = flow.w.model_api.settings.snapshot()
        config["values"]["daily_call_limit"] = 1
        with flow.w.db.write() as tx:
            flow.w.model_api.settings.update(
                {"values": config["values"], "expected_revision": config["revision"]}, uow=tx
            )
        return httpx.Response(200, json=envelope(json.dumps(answer)))

    results, seen = execute(experiment, handler)
    assert (
        len(seen) == 1
        and results["B"]["state"] == "not_sent"
        and results["B"]["error_code"] == "EXPERIMENT_CONFIG_CHANGED"
    )


def test_forward_registration_cannot_be_done_after_the_frozen_cutoff(experiment, tmp_path):
    flow, _, old_plan, source = experiment
    plan = make_plan(
        old_plan["configuration"],
        pairs=1,
        horizons=source["horizon_minutes"],
        cohort="forward",
        start_at=source["market_cutoff"],
        clock=lambda: "2026-10-12T03:00:00Z",
    )
    path = tmp_path / "late-plan"
    create_plan(path, plan)
    with pytest.raises(DomainError, match="先登记"):
        freeze_pair(path, "pair-0001", source, clock=flow.clock)


def test_claimed_but_interrupted_pair_is_unknown_in_report(experiment):
    from xau_next.workflows.experience_experiment import save

    flow, path, _, _ = experiment
    save(path / "pair-0001/started.json", {"at": flow.clock()})
    _, value = report(path, clock=flow.clock)
    assert value["engineering"]["A"]["states"] == {"outcome_unknown": 1, "not_run": 2}
