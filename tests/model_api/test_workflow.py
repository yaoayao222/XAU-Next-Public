"""SYNTHETIC provider + public API + isolated real SQLite and import path."""

import copy
import json
import sqlite3
import threading
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient
from xau_next.bootstrap import create_app
from xau_next.platform.model_gateway.public import Gateway, GatewayResult
from xau_next.shared.values import new_id

from tests.guidance.test_workflow import TOKEN, Clock, Flow, Sink
from tests.model_api.test_gateway import envelope

KEY = "synthetic-key-only-do-not-use"
COMMAND = {"retry_of": None, "acknowledge_possible_charge": False}


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def write(self, reference, key):
        self.values[reference] = key

    def read(self, reference):
        return self.values[reference]

    def configured(self, reference):
        return reference in self.values


class ModelFlow(Flow):
    def __init__(self, root, clock=None, credentials=None):
        self.clock, self.sink, self.credentials = clock or Clock(), Sink(), credentials or MemoryCredentials()
        self.sent = []
        self.answer_text = None
        self.handler = None
        self.gateway = Gateway(httpx.MockTransport(self.receive))
        self.app = create_app(
            data_root=root,
            token=TOKEN,
            source="synthetic",
            clock=self.clock,
            start_worker=False,
            notification_sink=self.sink,
            model_gateway=self.gateway,
            credential_store=self.credentials,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.closed = False
        self.count = 0

    def receive(self, request):
        self.sent.append(request)
        if self.handler:
            return self.handler(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-6.1-sol"}]})
        if request.url.path.endswith("/usage"):
            return httpx.Response(
                200, json={"quota": {"remaining": 12, "unit": "CREDITS"}, "is_active": True}
            )
        body = json.loads(request.content)
        answer = (
            self.answer_text if "冻结事实" in body.get("input", "") else json.dumps({
                "data_kind": "SYNTHETIC", "ok": True, "date_time": "2000-01-02T03:04:05.006Z",
                "reference": "probe-a_b--01", "signed_number": -12.5, "text": "中文",
            }, ensure_ascii=False)
        )
        return httpx.Response(200, json=envelope(answer), headers={"x-request-id": "synthetic-provider-id"})

    def setup_model(self, **overrides):
        config = self.get("/v1/model/settings")
        config["values"]["enabled"] = True
        config["values"].update(overrides)
        response = self.client.put(
            "/v1/model/settings",
            json={"expected_revision": config["revision"], "values": config["values"]},
            headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()},
        )
        assert response.status_code == 200, response.text
        self.post("/v1/model/credential", {"expected_revision": response.json()["revision"], "key": KEY}, 200)
        probe = self.post("/v1/model/probe", {**COMMAND, "acknowledge_possible_charge": True}, 202)
        self.w.model_api.process_once(self.w)
        assert self.get("/v1/model/calls/" + probe["call_id"])["state"] == "succeeded"
        assert self.get("/v1/model/settings")["capability_verified"] is True

    def ready(self):
        self.configure()
        self.setup_model()
        req = self.request(self.refresh())
        self.answer_text = json.dumps(self.answer(req), ensure_ascii=False)
        return req

    def start(self, req, command=None):
        return self.post(
            "/v1/analysis/requests/" + req["request_id"] + "/model-call", command or COMMAND, 202
        )

    def record(self, started):
        return self.get("/v1/model/calls/" + started["call"]["call_id"])


@pytest.fixture
def flow(tmp_path):
    f = ModelFlow(tmp_path)
    yield f
    f.close()


def test_success_double_click_reopen_and_imported_request_never_repays(flow, tmp_path):
    req = flow.ready()
    start = flow.start(req)
    assert flow.start(req)["call"]["call_id"] == start["call"]["call_id"]
    flow.clock.value = "2026-10-12T02:00:05Z"
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert (
        record["state"] == "succeeded" and record["answer_valid"] is True and record["candidate_count"] == 1
    )
    assert record["input_tokens"] == 12 and record["estimated_cost"] is None
    saved = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    assert (
        saved["raw_text"] == flow.answer_text
        and saved["available_at"] == flow.clock.value
        and saved["origin"]["source"] == "model_api"
    )
    assert len(flow.sent) == 2 and flow.start(req)["version_id"] == record["version_id"]
    assert len(flow.sent) == 2
    flow.close()
    reopened = ModelFlow(tmp_path, flow.clock, flow.credentials)
    try:
        assert reopened.start(req)["version_id"] == record["version_id"] and not reopened.sent
        assert (
            reopened.get("/v1/model/calls/" + record["call_id"])["raw_response"]["sha256"]
            == record["raw_response"]["sha256"]
        )
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("invalid_json", "SCHEMA_INVALID"),
        ("business", "PRICE_GRID_INVALID"),
        ("401", "MODEL_AUTH_FAILED"),
        ("429", "MODEL_RATE_LIMITED"),
        ("quota", "MODEL_QUOTA_EXHAUSTED"),
        ("refused", "MODEL_REFUSED"),
        ("truncated", "MODEL_INCOMPLETE"),
    ],
)
def test_failed_answers_never_create_versions(flow, failure, code):
    req = flow.ready()
    if failure == "invalid_json":
        flow.answer_text = '{"not complete"'
    elif failure == "business":
        answer = json.loads(flow.answer_text)
        answer["plans"][0]["stop_loss"] = "104.001"
        flow.answer_text = json.dumps(answer)
    elif failure == "refused":
        flow.handler = lambda request: httpx.Response(
            200,
            json=envelope(
                output=[{"type": "message", "content": [{"type": "refusal", "refusal": "SYNTHETIC"}]}]
            ),
        )
    elif failure == "truncated":
        flow.handler = lambda request: httpx.Response(200, json=envelope(status="incomplete"))
    else:
        flow.handler = lambda request: httpx.Response(
            401 if failure == "401" else 429,
            json={"error": {"code": "insufficient_quota" if failure == "quota" else "synthetic"}},
        )
    start = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert record["state"] == "failed" and record["error_code"] == code, record
    assert record["raw_response"] and flow.get("/v1/workspace")["current_forecast"] is None
    assert len(flow.sent) == 2


def test_usage_unknown_fields_preserve_prior_success_after_error(flow):
    flow.setup_model()
    first = flow.post("/v1/model/checks", {"kind": "usage"}, 200)
    assert first["last_successful_usage"]["unit"] == "CREDITS"
    flow.handler = lambda request: httpx.Response(401, json={"error": KEY})
    failed = flow.post("/v1/model/checks", {"kind": "usage"}, 200)
    assert (
        failed["last_successful_usage"] == first["last_successful_usage"]
        and next(c for c in failed["checks"] if c["kind"] == "usage")["status"] == "failed"
    )
    flow.handler = lambda request: httpx.Response(200, json={})
    unknown = flow.post("/v1/model/checks", {"kind": "usage"}, 200)["last_successful_usage"]
    assert unknown["remaining"] is None and unknown["unit"] is None and unknown["is_active"] is None


def test_full_response_crash_recovers_locally_without_new_http(flow, monkeypatch, tmp_path):
    req = flow.ready()
    start = flow.start(req)
    import xau_next.workflows.model_api as module

    original = module.extract
    monkeypatch.setattr(
        module,
        "extract",
        lambda *args: (_ for _ in ()).throw(SystemExit("SYNTHETIC crash after response saved")),
    )
    with pytest.raises(SystemExit):
        flow.w.model_api.process_once(flow.w)
    assert flow.record(start)["stage"] == "response_saved"
    monkeypatch.setattr(module, "extract", original)
    flow.clock.value = "2026-10-12T02:00:31Z"
    flow.close()
    reopened = ModelFlow(tmp_path, flow.clock, flow.credentials)
    try:
        assert reopened.w.model_api.process_once(reopened.w)
        assert reopened.record(start)["state"] == "succeeded" and reopened.sent == []
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "mode,code,state",
    [
        ("failed", "MODEL_UPSTREAM_FAILED", "failed"),
        ("limit", "MODEL_OUTPUT_LIMIT", "failed"),
        ("disconnect", "MODEL_CONNECTION_UNKNOWN", "outcome_unknown"),
    ],
)
def test_stream_failures_preserve_evidence_without_import_or_retry(flow, mode, code, state):
    req = flow.ready()

    def receive(request):
        if mode == "disconnect":
            raise httpx.RemoteProtocolError("do not persist " + KEY)
        final = envelope(
            status="failed" if mode == "failed" else "incomplete",
            id="synthetic-terminal-id",
            incomplete_details={"reason": "max_output_tokens"} if mode == "limit" else None,
        )
        event = "response.failed" if mode == "failed" else "response.incomplete"
        raw = 'event: response.output_text.done\ndata: {"type":"response.output_text.done","text":"{}"}\n\n'
        raw += "event: response.completed\n\n"
        raw += "data: " + json.dumps({"type": event, "response": final}) + "\n\n"
        return httpx.Response(200, text=raw, headers={"content-type": "text/event-stream"})

    flow.handler = receive
    started = flow.start(req)
    assert flow.w.model_api.process_once(flow.w)
    call = flow.record(started)
    assert call["state"] == state and call["error_code"] == code and call["version_id"] is None
    assert flow.get("/v1/analysis/requests/" + req["request_id"])["state"] == "awaiting_answer"
    assert len(flow.sent) == 2
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2
    with flow.w.db.read() as scope:
        saved = flow.w.model_api.calls.get(call["call_id"], scope=scope, internal=True)
        diagnostics = saved["response_meta"]["diagnostics"]
    assert diagnostics["elapsed_ms"] >= 0 and KEY not in json.dumps(diagnostics)
    if mode == "disconnect":
        assert diagnostics["failure_kind"] == "remote_protocol_error" and call["input_tokens"] is None
    else:
        assert call["input_tokens"] == 12 and call["output_tokens"] == 34
        assert call["provider_response_id"] == "synthetic-terminal-id"


def test_sent_crash_recovers_as_unknown_and_requires_explicit_paid_retry(flow, monkeypatch):
    req = flow.ready()
    start = flow.start(req)
    monkeypatch.setattr(
        flow.gateway,
        "exchange",
        lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit("SYNTHETIC after send")),
    )
    with pytest.raises(SystemExit):
        flow.w.model_api.process_once(flow.w)
    flow.clock.value = "2026-10-12T02:00:31Z"
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert record["state"] == "outcome_unknown" and record["version_id"] is None
    error = flow.post("/v1/analysis/requests/" + req["request_id"] + "/model-call", COMMAND, 409)
    assert error["code"] == "MODEL_EXPLICIT_RETRY_REQUIRED"
    error = flow.post(
        "/v1/analysis/requests/" + req["request_id"] + "/model-call",
        {"retry_of": record["call_id"], "acknowledge_possible_charge": False},
        409,
    )
    assert error["code"] == "MODEL_CHARGE_ACK_REQUIRED"


def test_waiting_model_does_not_block_market_workspace_and_late_cancel_cannot_import(flow, monkeypatch):
    req = flow.ready()
    start = flow.start(req)
    entered = threading.Event()
    release = threading.Event()

    def wait(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return GatewayResult(raw=json.dumps(envelope(flow.answer_text)).encode(), status=200, complete=True)

    monkeypatch.setattr(flow.gateway, "exchange", wait)
    thread = threading.Thread(target=lambda: flow.w.model_api.process_once(flow.w))
    thread.start()
    try:
        assert entered.wait(5)
        assert flow.w.model_api.process_once(flow.w) is False
        assert flow.get("/v1/workspace")["market"]
        flow.refresh()
        flow.post("/v1/jobs/" + start["call"]["job_id"] + "/cancel", None, 202)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert (
            flow.record(start)["state"] == "cancelled"
            and flow.get("/v1/workspace")["current_forecast"] is None
        )
    finally:
        release.set()
        thread.join(5)


def test_key_never_enters_database_attachments_or_logs_and_echo_is_redacted(flow, tmp_path, caplog):
    req = flow.ready()
    start = flow.start(req)
    flow.handler = lambda request: httpx.Response(
        401, json={"error": {"message": KEY}}, headers={"x-request-id": KEY}
    )
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert record["raw_redacted"] is True
    assert KEY not in json.dumps(flow.get("/v1/model/calls/" + record["call_id"] + "/raw"))
    assert KEY not in caplog.text
    for folder in ("db", "attachments", "logs"):
        for path in (tmp_path / folder).rglob("*"):
            if path.is_file():
                assert KEY.encode() not in path.read_bytes(), path
    with sqlite3.connect(flow.w.db.path) as database:
        assert KEY not in "\n".join(database.iterdump())
        for (compressed,) in database.execute("SELECT compressed FROM payload_blobs"):
            assert KEY.encode() not in zlib.decompress(compressed)


def test_enabled_model_is_never_called_by_fact_refresh_or_guidance_timer(flow):
    req = flow.ready()
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    assert flow.record(started)["state"] == "succeeded"
    count = len(flow.sent)
    flow.clock.value = "2026-10-12T02:25:00Z"
    flow.refresh()
    flow.w.guidance.tick()
    assert flow.w.model_api.process_once(flow.w) is False
    assert len(flow.sent) == count


def test_exhausted_local_recovery_is_reflected_in_durable_call_without_http(flow):
    from sqlalchemy import text

    req = flow.ready()
    started = flow.start(req)
    with flow.w.db.write() as uow:
        uow._session.execute(
            text("UPDATE jobs SET attempt=max_attempts WHERE job_id=:job"), {"job": started["call"]["job_id"]}
        )
    assert flow.w.model_api.process_once(flow.w) is False
    record = flow.record(started)
    assert (
        record["state"] == "failed" and record["error_code"] == "ATTEMPTS_EXHAUSTED" and len(flow.sent) == 1
    )


def test_empty_candidates_is_valid_but_never_an_entry_signal(flow):
    req = flow.ready()
    answer = json.loads(flow.answer_text)
    answer["plans"] = []
    for item in answer["candidate_assessments"]:
        item.update(outcome="no_candidate", plan_key=None, missing_conditions=["SYNTHETIC 缺少结构依据。"])
    flow.answer_text = json.dumps(answer)
    start = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    assert flow.record(start)["state"] == "succeeded" and flow.record(start)["candidate_count"] == 0
    workspace = flow.get("/v1/workspace")
    assert workspace["plans"] == [] and workspace["action_advice"] == []


@pytest.mark.parametrize("when", ["before_click", "queued", "returning"])
def test_analysis_freshness_is_checked_before_send_and_before_commit(flow, when):
    req = flow.ready()
    if when == "before_click":
        flow.clock.value = "2026-10-12T02:30:00Z"
        error = flow.post("/v1/analysis/requests/" + req["request_id"] + "/model-call", COMMAND, 409)
        assert error["code"] == "MODEL_ANALYSIS_EXPIRED" and len(flow.sent) == 1
        return
    start = flow.start(req)
    if when == "queued":
        flow.clock.value = "2026-10-12T02:30:00Z"
    else:

        def delayed(request):
            flow.clock.value = "2026-10-12T02:30:00Z"
            # A running worker retains its valid lease via a successful heartbeat.
            # The scenario isolates business expiry from independent lease expiry.
            with flow.w.db.write() as uow:
                from sqlalchemy import text

                uow._session.execute(
                    text(
                        "UPDATE jobs SET lease_expires_at='2026-10-12T02:31:00.000000Z' WHERE kind='model_call' AND state='running'"
                    )
                )
            return httpx.Response(200, json=envelope(flow.answer_text))

        flow.handler = delayed
    flow.w.model_api.process_once(flow.w)
    assert flow.record(start)["error_code"] == "MODEL_ANALYSIS_EXPIRED"
    assert flow.get("/v1/workspace")["current_forecast"] is None
    assert len(flow.sent) == (1 if when == "queued" else 2)


def test_config_is_frozen_for_queued_call_and_price_unknown_stays_unknown(flow):
    req = flow.ready()
    start = flow.start(req)
    current = flow.get("/v1/model/settings")
    changed = copy.deepcopy(current["values"])
    changed.update(model_id="gpt-6-astra", reasoning_effort="medium")
    response = flow.client.put(
        "/v1/model/settings",
        json={"expected_revision": current["revision"], "values": changed},
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()},
    )
    assert response.status_code == 200 and response.json()["capability_verified"] is False
    flow.w.model_api.process_once(flow.w)
    sent = json.loads(flow.sent[-1].content)
    assert sent["model"] == "gpt-6.1-sol" and sent["reasoning"] == {"effort": "high"}
    assert flow.record(start)["config_revision"] == current["revision"]


def test_disconnect_does_not_retry_and_no_partial_version(flow):
    req = flow.ready()

    def broken(request):
        raise httpx.ReadError("SYNTHETIC disconnected", request=request)

    flow.handler = broken
    start = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert record["state"] == "outcome_unknown" and record["error_code"] == "MODEL_CONNECTION_UNKNOWN"
    assert len(flow.sent) == 2 and flow.w.model_api.process_once(flow.w) is False


def test_old_executor_loses_lease_and_cannot_commit_late_response(flow, monkeypatch):
    req = flow.ready()
    start = flow.start(req)
    entered = threading.Event()
    release = threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return GatewayResult(raw=json.dumps(envelope(flow.answer_text)).encode(), status=200, complete=True)

    monkeypatch.setattr(flow.gateway, "exchange", delayed)
    thread = threading.Thread(target=lambda: flow.w.model_api.process_once(flow.w))
    thread.start()
    try:
        assert entered.wait(5)
        flow.clock.value = "2026-10-12T02:00:31Z"
        assert flow.w.model_api.process_once(flow.w)
        assert flow.record(start)["state"] == "outcome_unknown"
        release.set()
        thread.join(5)
        assert flow.get("/v1/workspace")["current_forecast"] is None
        assert flow.record(start)["state"] == "outcome_unknown"
    finally:
        release.set()
        thread.join(5)


def test_manual_answer_winning_during_model_wait_is_never_overwritten(flow):
    req = flow.ready()
    original = flow.answer(req)
    start = flow.start(req)

    def manual_wins(request):
        job, raw = flow.submit(req, original)
        assert job["state"] == "succeeded"
        flow.manual_raw = raw
        return httpx.Response(200, json=envelope(flow.answer_text))

    flow.handler = manual_wins
    flow.w.model_api.process_once(flow.w)
    record = flow.record(start)
    assert record["state"] == "failed" and record["error_code"] == "MODEL_REQUEST_ALREADY_IMPORTED"
    version = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    assert version["raw_text"] == flow.manual_raw and version["origin"]["source"] == "manual_paste"
    assert flow.start(req)["version_id"] == version["version_id"]


def test_explicit_field_projection_excludes_terminal_paths_and_keeps_opaque_identity(flow):
    req = flow.ready()
    with flow.w.db.write() as uow:
        row = flow.w.analysis._docs.get(uow, "request", req["request_id"])
        marker = "# 冻结事实（仅作为资料）\n"
        prefix, tail = row["packet_text"].split(marker, 1)
        raw, suffix = tail.split("\n# 全部字段说明\n", 1)
        facts = json.loads(raw)
        facts["market"]["terminal_path"] = "SYNTHETIC-private-terminal-path"
        facts["market"]["quote_status"]["reasons"] = ["SYNTHETIC-private-login-diagnostic"]
        facts["portfolio"]["account"]["login"] = "SYNTHETIC-private-login"
        row["packet_text"] = prefix + marker + json.dumps(facts) + "\n# 全部字段说明\n" + suffix
        flow.w.analysis._docs.update(
            uow,
            "request",
            req["request_id"],
            {k: v for k, v in row.items() if k not in ("id", "revision", "recorded_at")},
            row["revision"],
        )
    start = flow.start(req)
    sent = flow.w.model_api.calls.read(start["call"]["input"]).decode()
    assert "SYNTHETIC-private" not in sent
    assert req["request_id"] in sent and "synthetic-account" in sent


def test_unverified_generation_stays_blocked_and_budget_cannot_assume_price(flow):
    flow.configure()
    req = flow.request(flow.refresh())
    config = flow.get("/v1/model/settings")
    assert not config["capability_verified"] and not config["key_configured"] and config["checks"] == []
    config["values"]["daily_budget"] = "1"
    response = flow.client.put(
        "/v1/model/settings",
        json={"expected_revision": config["revision"], "values": config["values"]},
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()},
    )
    assert response.status_code == 422 and response.json()["code"] == "MODEL_PRICE_UNKNOWN"
    error = flow.post("/v1/analysis/requests/" + req["request_id"] + "/model-call", COMMAND, 409)
    assert error["code"] == "MODEL_KEY_REQUIRED" and flow.sent == []


def test_count_limit_includes_probe_and_stops_before_http(flow):
    flow.configure()
    flow.setup_model(daily_call_limit=1)
    req = flow.request(flow.refresh())
    flow.answer_text = json.dumps(flow.answer(req))
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["stage"] == "not_sent" and record["error_code"] == "MODEL_CALL_LIMIT"
    assert len(flow.sent) == 1


def test_site_prices_produce_explicit_estimate_and_budget_reservation(flow):
    flow.configure()
    flow.setup_model(
        input_price_per_million="2",
        output_price_per_million="6",
        price_currency="TEST",
        price_source="SYNTHETIC provider pricing fixture",
        daily_budget="1",
    )
    req = flow.request(flow.refresh())
    flow.answer_text = json.dumps(flow.answer(req))
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["state"] == "succeeded" and record["estimated_cost"] == "0.000228"
    assert record["cost_currency"] == "TEST" and record["cost_source"].startswith("SYNTHETIC")
    flow.post("/v1/model/checks", {"kind": "usage"}, 200)
    assert flow.record(started)["estimated_cost"] == "0.000228"


def test_base_version_conflict_preserves_both_original_answers(flow):
    req = flow.ready()
    base_job, base_raw = flow.submit(req, flow.answer(req))
    assert base_job["state"] == "succeeded"
    base = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    command = {
        "kind": "update",
        "forecast_id": req["forecast_id"],
        "base_version_id": base["version_id"],
        "market_snapshot_id": req["market_snapshot_id"],
        "horizon_minutes": req["horizon_minutes"],
    }
    update = flow.post("/v1/analysis/requests", command)
    winner = flow.post("/v1/analysis/requests", command)
    flow.answer_text = json.dumps(flow.answer(update))
    started = flow.start(update)

    def replaced(request):
        job, raw = flow.submit(winner, flow.answer(winner))
        assert job["state"] == "succeeded"
        flow.winner_raw = raw
        return httpx.Response(200, json=envelope(flow.answer_text))

    flow.handler = replaced
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["state"] == "failed" and record["error_code"] == "MODEL_REQUEST_UNAVAILABLE"
    current = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    assert current["raw_text"] == flow.winner_raw and current["previous_version_id"] == base["version_id"]
    assert (
        flow.get("/v1/forecasts/" + req["forecast_id"] + "/versions/" + base["version_id"])["raw_text"]
        == base_raw
    )


def test_api_update_keeps_original_window_and_raw_history(flow):
    req = flow.ready()
    first = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    base = flow.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
    original = base["raw_text"]
    update = flow.post(
        "/v1/analysis/requests",
        {
            "kind": "update",
            "forecast_id": req["forecast_id"],
            "base_version_id": base["version_id"],
            "market_snapshot_id": req["market_snapshot_id"],
            "horizon_minutes": req["horizon_minutes"],
        },
    )
    flow.answer_text = json.dumps(flow.answer(update))
    second = flow.start(update)
    flow.w.model_api.process_once(flow.w)
    assert flow.record(second)["state"] == "succeeded"
    current = flow.get("/v1/forecasts/" + req["forecast_id"])
    assert (
        current["forecast"]["window_end"] == req["window_end"]
        and current["current_version"]["previous_version_id"] == flow.record(first)["version_id"]
    )
    assert (
        flow.get("/v1/forecasts/" + req["forecast_id"] + "/versions/" + base["version_id"])["raw_text"]
        == original
    )
