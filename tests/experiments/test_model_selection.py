"""SYNTHETIC only: real SQLite claims and MockTransport, no provider or terminal I/O."""

import copy
import gzip
import hashlib
import json
import sqlite3
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from xau_next.domains.analysis.public import ModelCallStore, frozen_answer_schema
from xau_next.domains.review.public import score_selection_window
from xau_next.platform.files.public import AttachmentStore
from xau_next.platform.model_gateway.public import Gateway, GatewayResult
from xau_next.platform.model_gateway.selection import SelectionLedger
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from xau_next.workflows.model_selection import (
    ARMS,
    authorize,
    digest,
    freeze_sample,
    make_plan,
    read,
    report,
    run_sample,
    score_sample,
    seal_followup,
    seal_plan,
    slot_for,
    validate_plan,
)

from tests.model_api.test_gateway import envelope
from tests.model_api.test_workflow import MemoryCredentials, ModelFlow


@pytest.fixture(autouse=True)
def forbid_real_http(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("REAL_HTTP_FORBIDDEN_IN_STAGE0_TESTS")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)


@pytest.fixture
def case(tmp_path, monkeypatch):
    from xau_next.domains.analysis import service

    monkeypatch.setattr(service, "PROMPT_VERSION", "manual-analysis/14")
    flow = ModelFlow(tmp_path / "formal-synthetic")
    flow.configure()
    market = flow.refresh()
    request = flow.post(
        "/v1/analysis/requests",
        {
            "kind": "new",
            "forecast_id": None,
            "base_version_id": None,
            "market_snapshot_id": market["snapshot_id"],
            "horizon_minutes": [30, 60, 180],
        },
    )
    with flow.w.db.read() as tx:
        request = flow.w.db.documents("analysis").get(tx, "request", request["request_id"])
    request = {k: v for k, v in request.items() if k not in ("id", "revision", "recorded_at")}
    config = flow.w.model_api.settings.snapshot()["values"]
    config.update(base_url="http://127.0.0.1:8317/v1", protocol="responses", output_format="json_schema")
    profile = {"profile_id": "synthetic-cpa", "values": config, "credential_ref": "XAU-Next/model/test-cpa"}
    plan = make_plan(
        profile,
        flow.w.db.data_root,
        answer_schema=frozen_answer_schema(request),
        clock=flow.clock,
        cohort="synthetic",
    )
    plan["accept_unknown_cost"] = True
    plan["registered_samples"] = [
        {"sample_id": "sample-0001", "stage": 1, "not_before": flow.clock(), "arms": list(ARMS)}
    ]
    root = tmp_path / "experiment"
    plan = seal_plan(root, plan)
    freeze_sample(root, "sample-0001", request, clock=flow.clock)
    answer = flow.answer(request)
    template = answer["horizons"][0]
    answer["horizons"] = []
    for minutes in (30, 60, 180):
        h = copy.deepcopy(template)
        end = (
            (parse_utc(request["market_cutoff"]) + timedelta(minutes=minutes))
            .isoformat()
            .replace("+00:00", "Z")
        )
        h.update(
            horizon_minutes=minutes,
            end_at=end,
            phases=[
                {
                    "start_at": request["market_cutoff"],
                    "end_at": end,
                    "direction": h["direction"],
                    "note": "SYNTHETIC单阶段。",
                }
            ],
            turning_points=[],
        )
        answer["horizons"].append(h)
    # No plans are needed to exercise forecasts; keep each enabled style's honest no-candidate result.
    answer["plans"] = []
    for item in answer["candidate_assessments"]:
        item.update(outcome="no_candidate", plan_key=None, missing_conditions=["SYNTHETIC无入口。"])
    credentials = MemoryCredentials()
    for item in plan["arms"].values():
        credentials.write(item["credential_ref"], "synthetic-selection-key")
    calls = ModelCallStore(flow.w.db, AttachmentStore(flow.w.db), flow.clock)
    authorization = {
        "allow_paid": True,
        "plan_sha256": digest(plan),
        "stages": [1],
        "max_attempts": 48,
        "data_kind": "SYNTHETIC",
        "accept_unknown_cost": True,
    }
    yield flow, root, plan, request, answer, credentials, calls, authorization
    flow.close()


def gateway_for(answer, sent, *, change=None, barrier=None):
    def receive(request):
        body = json.loads(request.content)
        sent.append(body)
        if barrier:
            barrier.wait(timeout=10)
        payload = envelope(json.dumps(answer, ensure_ascii=False), model=body["model"])
        if change:
            payload = change(payload, body)
        events = [
            {"type": "response.created", "response": {"status": "in_progress"}},
            {"type": "response.reasoning_text.delta", "delta": "SYNTHETIC"},
            {"type": "response.output_text.delta", "delta": payload["output"][0]["content"][0]["text"]},
            {"type": "response.completed", "response": payload},
        ]
        raw = b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n" for e in events)
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    return Gateway(httpx.MockTransport(receive))


def run(case, gateway, **overrides):
    flow, root, _plan, _request, _answer, credentials, calls, authorization = case
    return run_sample(
        root,
        "sample-0001",
        authorization=authorization,
        gateway=gateway,
        credentials=credentials,
        calls=calls,
        clock=flow.clock,
        **overrides,
    )


def test_same_27b_prompt_four_models_and_cpa_reference_without_activation(case):
    flow, _root, plan, request, answer, _credentials, _calls, _auth = case
    before = flow.w.model_api.settings.snapshot()
    sent = []
    results = run(case, gateway_for(answer, sent, barrier=threading.Barrier(4)))
    assert {r["state"] for r in results.values()} == {"succeeded"}, results
    assert len(sent) == 4 and {b["model"] for b in sent} == set(ARMS.values())
    assert len({b["input"] for b in sent}) == 1
    assert all(b["reasoning"] == {"effort": "high"} and b["text"]["format"]["strict"] for b in sent)
    assert flow.w.model_api.settings.snapshot() == before
    assert plan["arms"]["B"]["configuration"]["base_url"] == "http://127.0.0.1:8317/v1"
    assert plan["arms"]["A"]["credential_ref"] == plan["arms"]["B"]["credential_ref"]
    assert plan["arms"]["C"]["credential_ref"] != plan["arms"]["A"]["credential_ref"]
    assert request["prompt_version"] == "manual-analysis/14"
    with flow.w.db.read() as tx:
        assert flow.w.db.documents("analysis").list(tx, "version", limit=100) == []
    assert all(r["diagnostics"]["first_answer_delta_ms"] is not None for r in results.values())


def test_no_authorization_no_dispatch_or_key_read(case):
    case[-1]["allow_paid"] = False
    sent = []
    with pytest.raises(DomainError, match="授权"):
        run(case, gateway_for(case[4], sent))
    assert sent == [] and not (case[1] / "selection.sqlite3").exists()


def test_changed_packet_or_config_is_rejected_before_dispatch(case):
    root = case[1]
    path = root / "sample-0001/B-body.json"
    path.write_bytes(path.read_bytes().replace(b"gpt-6-astra", b"gpt-6-other"))
    with pytest.raises(DomainError) as error:
        run(case, gateway_for(case[4], []))
    assert error.value.code == "SELECTION_SAMPLE_CHANGED"


def test_relaunch_cannot_send_any_slot_again(case):
    sent = []
    gateway = gateway_for(case[4], sent)
    run(case, gateway)
    with pytest.raises((FileExistsError, DomainError)):
        run(case, gateway)
    assert len(sent) == 4


@pytest.mark.parametrize("defect", ["model", "schema", "identity", "empty", "truncated", "refused"])
def test_invalid_real_shape_is_preserved_and_halts_no_retry(case, defect):
    def change(p, body):
        if defect == "model":
            p["model"] = "different-model"
        if defect in ("schema", "identity"):
            a = json.loads(p["output"][0]["content"][0]["text"])
            if defect == "schema":
                a.pop("summary")
            else:
                a["request_id"] = "different-request"
            p["output"][0]["content"][0]["text"] = json.dumps(a)
        if defect == "empty":
            p["output"] = [{"type": "message", "content": [{"type": "output_text", "text": ""}]}]
        if defect == "truncated":
            p.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        if defect == "refused":
            p["output"][0]["content"] = [{"type": "refusal", "text": ""}]
        return p

    sent = []
    results = run(case, gateway_for(case[4], sent, change=change))
    assert any(r["state"] == "failed" for r in results.values())
    ledger = SelectionLedger(case[1] / "selection.sqlite3", digest(case[2]))
    assert ledger.status()["halt"]
    n = len(sent)
    with pytest.raises(DomainError):
        run(case, gateway_for(case[4], sent))
    assert len(sent) == n


@pytest.mark.parametrize(
    "error_code", ["MODEL_TIMEOUT_UNKNOWN", "MODEL_CONNECTION_UNKNOWN", "MODEL_CANCELLED"]
)
def test_transport_unknown_and_cancel_are_spent_and_never_retried(case, error_code):
    class FailingGateway:
        transport = httpx.MockTransport(lambda _: None)

        def exchange(self, *_args, **_kwargs):
            return GatewayResult(error_code=error_code, diagnostics={"failure_kind": "synthetic"})

    results = run(case, FailingGateway())
    assert any(r["error_code"] == error_code for r in results.values())
    ledger = SelectionLedger(case[1] / "selection.sqlite3", digest(case[2]))
    assert ledger.status()["attempts_reserved"] >= 1
    assert ledger.status()["halt"]


def test_pending_claim_after_crash_stops_whole_experiment(case):
    root, plan = case[1:3]
    ledger = SelectionLedger(root / "selection.sqlite3", digest(plan))
    ledger.claim(
        "crashed-slot", 1, "saved_cpa", case[0].clock(), total_limit=48, stage_limit=24, daily_limit=50
    )
    sent = []
    with pytest.raises(DomainError):
        run(case, gateway_for(case[4], sent))
    assert sent == []


def test_independent_workers_cannot_exceed_one_remaining_reservation(tmp_path):
    path = tmp_path / "ledger.sqlite3"

    def claim(index):
        ledger = SelectionLedger(path, "same-plan")
        try:
            ledger.claim(
                str(index),
                1,
                "official",
                "2026-10-21T23:59:59Z",
                total_limit=1,
                stage_limit=24,
                daily_limit=50,
            )
            return True
        except DomainError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(8)))
    assert sum(results) == 1


def test_stage_cap_currency_budget_and_utc_day_are_independent(tmp_path):
    ledger = SelectionLedger(tmp_path / "ledger.sqlite3", "plan")
    ledger.claim(
        "one",
        1,
        "official",
        "2026-10-21T23:59:59Z",
        total_limit=48,
        stage_limit=1,
        daily_limit=1,
        reserved="1",
        currency="USD",
        cost_limit="2",
    )
    with pytest.raises(DomainError) as e:
        ledger.claim(
            "two", 1, "official", "2026-10-22T00:00:01Z", total_limit=48, stage_limit=1, daily_limit=1
        )
    assert e.value.code == "SELECTION_STAGE_LIMIT"
    ledger.claim(
        "three",
        2,
        "official",
        "2026-10-22T00:00:01Z",
        total_limit=48,
        stage_limit=24,
        daily_limit=1,
        reserved="1",
        currency="USD",
        cost_limit="2",
    )
    with pytest.raises(DomainError):
        ledger.claim(
            "four",
            2,
            "another",
            "2026-10-22T00:00:01Z",
            total_limit=48,
            stage_limit=24,
            daily_limit=10,
            reserved="1",
            currency="USD",
            cost_limit="2",
        )


def actual_market(request):
    base = request["context"]["market"]
    start = parse_utc(request["market_cutoff"])
    stamp = lambda m: (start + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
    bars = [
        {
            "open_at": stamp(i),
            "close_at": stamp(i + 1),
            "open": "4200",
            "close": "4200",
            "high": "4202",
            "low": "4198",
            "closed": True,
            "tick_volume": 1,
        }
        for i in range(180)
    ]
    return {
        "snapshot_id": "synthetic-actual",
        "source_kind": base["source_kind"],
        "symbol": base["symbol"],
        "account_scope_id": base["account_scope_id"],
        "price_basis": "bid",
        "from": stamp(-1),
        "to": stamp(181),
        "bars": bars,
        "tick_status": "available",
        "ticks": [{"event_time": stamp(i), "bid": "4200", "ask": "4200.1"} for i in range(181)],
        "status": {"state": "available", "captured_at": stamp(181)},
    }


def test_all_three_scopes_are_scored_and_originals_preserved(case):
    sent = []
    run(case, gateway_for(case[4], sent))
    _flow, root, _plan, request, _answer = case[:5]
    market = actual_market(request)
    raw = (root / "sample-0001/A-answer.json").read_bytes()
    future = (parse_utc(request["market_cutoff"]) + timedelta(minutes=182)).isoformat().replace("+00:00", "Z")
    result = score_sample(root, "sample-0001", market, as_of=future, clock=lambda: future)
    assert len(result["arms"]["A"]) == 9
    assert {r["scope"] for r in result["arms"]["A"]} == {
        "full_descriptive",
        "after_own_available",
        "after_common_available",
    }
    assert (root / "sample-0001/A-answer.json").read_bytes() == raw


def test_post_availability_uses_actual_bid_and_missing_exact_tick_stays_unknown(case):
    request, answer = case[3:5]
    market = actual_market(request)
    start = parse_utc(request["market_cutoff"])
    own = (start + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    asof = (start + timedelta(minutes=182)).isoformat().replace("+00:00", "Z")
    first = copy.deepcopy(answer)
    first["horizons"][0]["direction"] = "long"
    for tick in market["ticks"]:
        if parse_utc(tick["event_time"]) == parse_utc(own):
            tick.update(bid="4190", ask="4190.1")
    scored = score_selection_window(
        first,
        request["context"],
        market,
        minutes=30,
        start=own,
        as_of=asof,
        scope="after_own_available",
        available_at=own,
    )
    assert scored["direction_class"] == "correct"
    market["ticks"] = [t for t in market["ticks"] if t["event_time"] != own]
    scored = score_selection_window(
        first,
        request["context"],
        market,
        minutes=30,
        start=own,
        as_of=asof,
        scope="after_own_available",
        available_at=own,
    )
    assert scored["direction_class"] == "unknown"


def _claim_in_process(args):
    path, slot = args
    ledger = SelectionLedger(path, "cross-process-plan")
    try:
        ledger.claim(
            slot, 1, "official", "2026-10-22T00:00:00Z", total_limit=1, stage_limit=24, daily_limit=10
        )
        return True
    except DomainError:
        return False


def test_cross_process_budget_and_slot_exclusivity(tmp_path):
    path = str(tmp_path / "process-ledger.sqlite3")
    SelectionLedger(path, "cross-process-plan")
    with multiprocessing.get_context("spawn").Pool(3) as pool:
        values = pool.map(_claim_in_process, [(path, str(i)) for i in range(6)])
    assert sum(values) == 1


def test_pre_send_cancel_uses_no_budget_and_sends_nothing(case):
    sent = []
    with pytest.raises(DomainError) as e:
        run(case, gateway_for(case[4], sent), stopped=lambda: True)
    assert e.value.code == "SELECTION_CANCELLED_BEFORE_SEND"
    assert not sent


def test_shared_formal_daily_limit_is_enforced_before_http(case):
    _flow, _root, plan, _request, answer, _credentials, calls, _auth = case
    # Consume the existing shared synthetic day budget, without model calls.
    config = plan["arms"]["A"]["configuration"]
    for _ in range(config["daily_call_limit"]):
        with calls.db.write() as tx:
            calls.reserve(config, 1, uow=tx)
    sent = []
    result = run(case, gateway_for(answer, sent))
    assert not sent
    assert any(r["error_code"] == "MODEL_CALL_LIMIT" for r in result.values())


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda p: p["arms"]["C"]["configuration"].update(base_url="https://untrusted.example/v1"),
            "SELECTION_OFFICIAL_REQUIRED",
        ),
        (
            lambda p: p["arms"]["B"]["configuration"].update(reasoning_effort="low"),
            "SELECTION_CONFIG_INVALID",
        ),
        (lambda p: p.update(total_call_limit=49), "SELECTION_BUDGET_INVALID"),
        (
            lambda p: p["arms"]["C"].update(credential_ref="OtherApplication/token"),
            "SELECTION_KEY_REFERENCE_INVALID",
        ),
    ],
)
def test_draft_cannot_silently_change_channel_effort_cap_or_credential_owner(case, change, code):
    plan = copy.deepcopy(case[2])
    change(plan)
    with pytest.raises(DomainError) as error:
        validate_plan(plan)
    assert error.value.code == code


def test_future_bars_or_modified_schema_cannot_enter_freeze(case, tmp_path):
    flow, _root, plan, request = case[:4]
    root = tmp_path / "new-experiment"
    seal_plan(root, plan)
    future = copy.deepcopy(request)
    future["context"]["market"]["series"][0]["bars"][-1]["close_at"] = "2099-01-01T00:00:00Z"
    with pytest.raises(DomainError) as error:
        freeze_sample(root, "sample-0001", future, clock=flow.clock)
    assert error.value.code == "SELECTION_FUTURE_INPUT"
    from xau_next.domains.analysis.public import build_packet

    weakened = copy.deepcopy(request)
    schema = frozen_answer_schema(request)
    schema["required"].remove("summary")
    weakened["packet_text"] = build_packet(
        {k: v for k, v in weakened.items() if k not in ("packet_text", "context")},
        weakened["context"],
        schema,
    )
    with pytest.raises(DomainError) as error:
        freeze_sample(root, "sample-0001", weakened, clock=flow.clock)
    assert error.value.code == "SELECTION_SCHEMA_CHANGED"


def test_report_retains_registered_denominators_and_never_fabricates_winner(case):
    result = report(case[1])
    assert len(result["rows"]) == 36
    assert all(r["class"] == "not_sent" for r in result["rows"])
    assert all(s["registered"] == 1 and s["conditional_hit_rate"] is None for s in result["summaries"])
    assert result["winner"] is None


def test_followup_is_appended_baseline_preserved_and_stage_two_needs_new_authorization(case):
    flow, root, plan, request, answer = case[:5]
    run(case, gateway_for(answer, []))
    asof = (parse_utc(flow.clock()) + timedelta(minutes=182)).isoformat().replace("+00:00", "Z")
    score_sample(root, "sample-0001", actual_market(request), as_of=asof, clock=lambda: asof)
    evidence = [str(next((root / "sample-0001").glob("score-*/scores.json")))]
    before = (root / "plan.json").read_bytes()
    later = (parse_utc(flow.clock()) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    followup = seal_followup(
        root, [{"sample_id": "sample-0002", "not_before": later}], "C", evidence, clock=flow.clock
    )
    assert followup["samples"][0]["arms"] == ["A", "C"]
    assert (root / "plan.json").read_bytes() == before
    assert slot_for(plan, "sample-0002", root)["stage"] == 2
    assert SelectionLedger(root / "selection.sqlite3", digest(plan)).status()["attempts_reserved"] == 4


def test_original_duplicate_json_and_business_failures_still_fail(case):
    from xau_next.workflows.experience_experiment import validate_output

    request, answer = case[3:5]
    schema = frozen_answer_schema(request)
    raw = json.dumps(answer, ensure_ascii=False)
    duplicate = raw[:-1] + ',"summary":"duplicate"}'
    assert not validate_output(duplicate.encode(), schema, request, request["context"])["business_valid"]
    wrong = copy.deepcopy(answer)
    wrong["market_cutoff"] = "2099-01-01T00:00:00Z"
    assert not validate_output(json.dumps(wrong).encode(), schema, request, request["context"])[
        "business_valid"
    ]


def test_prepare_reads_only_saved_cpa_without_activation_or_secret_access(case, tmp_path):
    from tooling.experiments.model_selection import main

    flow = case[0]
    profile_id = "synthetic-saved-cpa"
    with flow.w.db.write() as tx:
        flow.w.db.documents("settings").insert(
            tx,
            "model_profile",
            profile_id,
            {
                "name": "synthetic CPA",
                "values": case[2]["arms"]["A"]["configuration"],
                "credential_ref": "XAU-Next/model/test-cpa",
                "config_revision": 1,
            },
        )
    before = flow.w.model_api.settings.snapshot()
    root = tmp_path / "draft"
    assert (
        main(
            [
                "prepare",
                "--data-root",
                str(flow.w.db.data_root),
                "--cpa-profile",
                profile_id,
                "--root",
                str(root),
            ]
        )
        == 0
    )
    assert flow.w.model_api.settings.snapshot() == before
    assert read(root / "plan.draft.json")["status"] == "DRAFT"
    assert not list(root.glob("*-sent.json"))


def saved_source_fingerprint(data_root):
    connection = sqlite3.connect((data_root / "db/xau-next.sqlite3").as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        rows = "\n".join(connection.iterdump())
    finally:
        connection.close()
    attachments = {
        path.relative_to(data_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (data_root / "attachments").rglob("*") if path.is_file()
    }
    return hashlib.sha256(rows.encode()).hexdigest(), attachments


@pytest.mark.parametrize("storage_format", ["legacy", "chunked"])
def test_capture_saved_source_constructs_only_isolated_request(case, tmp_path, storage_format):
    from tooling.experiments.model_selection import capture_saved_snapshot

    flow, _oldroot, plan = case[:3]
    flow.w.market._storage_format = storage_format
    flow.clock.value = (parse_utc(flow.clock()) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    market = flow.refresh()
    expected = flow.w.market.read_evidence(market["snapshot_id"]).to_dict()
    root = tmp_path / "capture-experiment"
    seal_plan(root, plan)
    before = saved_source_fingerprint(flow.w.db.data_root)
    schema = Path(__file__).resolve().parents[2] / "contracts/analysis-answer.schema.json"
    result = capture_saved_snapshot(root, "sample-0001", schema, clock=flow.clock)
    assert result["state"] == "FROZEN_FROM_SAVED_SOURCE"
    frozen = read(root / "sample-0001/request.json")
    assert frozen["prompt_version"] == "manual-analysis/14"
    assert frozen["context"]["market"]["series"] == expected["result"]["market"]["series"]
    assert frozen["context"]["portfolio"]["orders"] == expected["result"]["portfolio"]["orders"]
    assert saved_source_fingerprint(flow.w.db.data_root) == before


@pytest.mark.parametrize("fault", ["missing", "corrupt", "unregistered", "path_escape", "binding"])
def test_capture_saved_source_rejects_invalid_chunks_and_bindings_without_writes(case, tmp_path, fault):
    from tooling.experiments.model_selection import capture_saved_snapshot

    flow, _oldroot, plan = case[:3]
    data_root = flow.w.db.data_root
    with flow.w.db.read() as scope:
        snapshot = flow.w.market.latest(scope=scope).to_dict()
        evidence = flow.w.db.documents("market").get(scope, "evidence", snapshot["snapshot_id"])
        files = AttachmentStore(flow.w.db)
        manifest = json.loads(gzip.decompress(files.read(scope, evidence["attachment_id"])))
        part_id = next(ref["id"] for array in manifest["arrays"] for ref in array["chunks"])
        part = files.get(scope, part_id)
    path = (data_root / part.relative_path).resolve()
    assert path.is_relative_to((data_root / "attachments").resolve())
    if fault == "missing":
        path.unlink()
    elif fault == "corrupt":
        content = path.read_bytes()
        path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    elif fault in {"unregistered", "path_escape"}:
        connection = sqlite3.connect(flow.w.db.path)
        try:
            if fault == "unregistered":
                connection.execute("DELETE FROM attachments WHERE id=?", (part_id,))
            else:
                connection.execute("UPDATE attachments SET relative_path=? WHERE id=?", ("../outside", part_id))
            connection.commit()
        finally:
            connection.close()
    else:
        with flow.w.db.write() as scope:
            docs = flow.w.db.documents("market")
            stored = docs.get(scope, "snapshot", snapshot["snapshot_id"])
            stored["header"]["symbol"] = "SYNTHETIC-MISMATCH"
            docs.update(scope, "snapshot", snapshot["snapshot_id"], stored, stored["revision"])
    root = tmp_path / "invalid-capture"
    seal_plan(root, plan)
    before = saved_source_fingerprint(data_root)
    schema = Path(__file__).resolve().parents[2] / "contracts/analysis-answer.schema.json"
    with pytest.raises((ValueError, OSError)):
        capture_saved_snapshot(root, "sample-0001", schema, clock=flow.clock)
    assert not (root / "input-sample-0001").exists()
    assert not (root / "sample-0001").exists()
    assert saved_source_fingerprint(data_root) == before


def test_later_source_never_repairs_original_missing_endpoint(case):
    _flow, root, _plan, request, answer = case[:5]
    run(case, gateway_for(answer, []))
    complete = actual_market(request)
    missing = copy.deepcopy(complete)
    end = parse_utc(request["market_cutoff"]) + timedelta(minutes=30)
    missing["ticks"] = [t for t in missing["ticks"] if parse_utc(t["event_time"]) != end]
    stamp = lambda n: (
        (parse_utc(request["market_cutoff"]) + timedelta(minutes=n)).isoformat().replace("+00:00", "Z")
    )
    one = score_sample(root, "sample-0001", missing, as_of=stamp(182), clock=lambda: stamp(182))
    original = (root / "sample-0001/actual-30.json").read_bytes()
    two = score_sample(root, "sample-0001", complete, as_of=stamp(183), clock=lambda: stamp(183))
    for result in (one, two):
        row = next(r for r in result["arms"]["A"] if r["minutes"] == 30 and r["scope"] == "full_descriptive")
        assert row["direction_class"] == "unknown"
    assert (root / "sample-0001/actual-30.json").read_bytes() == original


def test_short_common_window_cannot_inflate_primary_hit_rate(case):
    flow, root, _plan, request, answer = case[:5]
    late = (parse_utc(request["market_cutoff"]) + timedelta(minutes=20)).isoformat().replace("+00:00", "Z")

    def advance(payload, _body):
        flow.clock.value = late
        return payload

    run(case, gateway_for(answer, [], change=advance))
    asof = (parse_utc(request["market_cutoff"]) + timedelta(minutes=182)).isoformat().replace("+00:00", "Z")
    score_sample(root, "sample-0001", actual_market(request), as_of=asof, clock=lambda: asof)
    summary = next(
        s
        for s in report(root)["summaries"]
        if s["arm"] == "A" and s["minutes"] == 30 and s["scope"] == "after_common_available"
    )
    assert summary["counts"] == {"unusable_short_window": 1}
    assert summary["correct_per_registered"] == "0"
    assert summary["conditional_hit_rate"] is None


def test_first_group_authorization_caps_both_ledgers_at_four(case):
    _flow, root, plan, _request, answer, _credentials, _calls, authorization = case
    authorization.update(max_attempts=4, sample_ids=["sample-0001"])
    results = run(case, gateway_for(answer, [], barrier=threading.Barrier(4)))
    assert all(r["state"] == "succeeded" for r in results.values())
    ledger = SelectionLedger(root / "selection.sqlite3", digest(plan))
    assert ledger.status()["attempts_reserved"] == 4
    with pytest.raises(DomainError, match="次数"):
        ledger.claim("fifth", 1, "saved_cpa", case[0].clock(), total_limit=4, stage_limit=24, daily_limit=50)
    with pytest.raises(DomainError):
        authorize(plan, authorization, 1, "sample-0002")


@pytest.mark.parametrize("attempts", [0, 49, True, "4"])
def test_invalid_narrow_authorization_never_dispatches(case, attempts):
    case[-1]["max_attempts"] = attempts
    sent = []
    with pytest.raises(DomainError):
        run(case, gateway_for(case[4], sent))
    assert sent == []
