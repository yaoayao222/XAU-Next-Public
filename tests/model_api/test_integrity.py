"""SYNTHETIC regressions for character loss, legacy proof and safe network phases."""
import copy
import json

import httpx
import pytest
from xau_next.domains.analysis.validation import validate_answer
from xau_next.domains.settings.model_api import defaults
from xau_next.platform.model_gateway.public import Gateway, extract
from xau_next.shared.errors import DomainError

from tests.model_api.test_gateway import envelope
from tests.model_api.test_workflow import COMMAND, KEY, ModelFlow

SAMPLE = {
    "data_kind": "SYNTHETIC", "ok": True,
    "date_time": "2000-01-02T03:04:05.006Z",
    "reference": "probe-a_b--01", "signed_number": -12.5, "text": "中文",
}


@pytest.fixture
def flow(tmp_path):
    instance = ModelFlow(tmp_path)
    yield instance
    instance.close()


@pytest.mark.parametrize("change", ["legacy", "hyphens", "underscore", "negative", "unicode"])
def test_character_loss_fails_probe_and_keeps_original_without_retry(flow, change):
    flow.setup_model()
    value = copy.deepcopy(SAMPLE)
    if change == "legacy":
        value = {"data_kind": "SYNTHETIC", "ok": True}
    elif change == "underscore":
        value["reference"] = value["reference"].replace("_", "")
    elif change == "negative":
        value["signed_number"] = 12.5
    elif change == "unicode":
        value["text"] = "??"
    raw = json.dumps(value, ensure_ascii=False)
    if change == "hyphens":
        raw = raw.replace("-", "")
    flow.handler = lambda request: httpx.Response(200, json=envelope(raw))
    call = flow.post("/v1/model/probe", {**COMMAND, "acknowledge_possible_charge": True}, 202)
    flow.w.model_api.process_once(flow.w)
    record = flow.get("/v1/model/calls/" + call["call_id"])
    assert record["state"] == "failed" and record["error_code"] == "MODEL_PROBE_INVALID"
    assert record["answer_valid"] is False
    assert not flow.get("/v1/model/settings")["capability_verified"]
    assert flow.get("/v1/model/calls/" + call["call_id"] + "/raw")["answer"] == raw
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2


def test_equivalent_json_escapes_pass_exact_value_probe(flow):
    flow.setup_model()
    raw = json.dumps(SAMPLE, ensure_ascii=True).replace("2000-01-02", r"2000\u002d01\u002d02")
    raw = raw.replace("probe-a_b--01", r"probe\u002da_b\u002d\u002d01")
    flow.handler = lambda request: httpx.Response(200, json=envelope(raw))
    call = flow.post("/v1/model/probe", {**COMMAND, "acknowledge_possible_charge": True}, 202)
    assert call["prompt_version"] == "synthetic-integrity/2"
    flow.w.model_api.process_once(flow.w)
    assert flow.get("/v1/model/calls/" + call["call_id"])["state"] == "succeeded"
    assert flow.get("/v1/model/settings")["capability_verified"]


def test_old_connectivity_proof_is_readable_but_never_grants_integrity_capability(flow):
    flow.setup_model()
    settings = flow.w.model_api.settings
    with flow.w.db.write() as tx:
        current = settings.snapshot(scope=tx)
        proof = settings.docs.get(tx, "model_verified", str(current["revision"]))
        legacy = {key: value for key, value in proof.items() if key not in {"id", "revision", "recorded_at", "validation_policy"}}
        settings.docs.update(tx, "model_verified", str(current["revision"]), legacy, proof["revision"])
    with flow.w.db.read() as tx:
        before = settings.docs.get(tx, "model_verified", str(current["revision"]))
    assert flow.get("/v1/model/settings")["capability_verified"] is False
    with flow.w.db.read() as tx:
        assert settings.docs.get(tx, "model_verified", str(current["revision"])) == before
    assert len(flow.sent) == 1
    current = flow.get("/v1/model/settings")
    current["values"]["daily_call_limit"] += 1
    with flow.w.db.write() as tx:
        settings.update({"expected_revision": current["revision"], "values": current["values"]}, uow=tx)
    assert not flow.get("/v1/model/settings")["capability_verified"]
    assert len(flow.sent) == 1


def test_queued_legacy_probe_is_not_sent_or_upgraded(flow):
    flow.setup_model()
    call = flow.post("/v1/model/probe", {**COMMAND, "acknowledge_possible_charge": True}, 202)
    with flow.w.db.write() as tx:
        flow.w.model_api.calls.update(call["call_id"], {"prompt_version": "synthetic-connectivity/1"}, uow=tx)
    flow.w.model_api.process_once(flow.w)
    record = flow.get("/v1/model/calls/" + call["call_id"])
    assert record["stage"] == "not_sent" and record["error_code"] == "MODEL_PROBE_POLICY_CHANGED"
    assert len(flow.sent) == 1 and not flow.get("/v1/model/settings")["capability_verified"]


def test_date_and_reference_loss_are_reported_together_without_mutation(flow):
    request = flow.ready()
    with flow.w.db.read() as tx:
        _, schema, _ = flow.w.analysis.model_packet(request["request_id"], scope=tx)
        frozen = flow.w.analysis._docs.get(tx, "request", request["request_id"])
    context = copy.deepcopy(frozen["context"])
    context["evidence_refs"].append("SYNTHETIC-evidence-1")
    answer = json.loads(flow.answer_text)
    answer["market_cutoff"] = request["market_cutoff"].replace("-", "")
    answer["evidence_refs"] = ["SYNTHETICevidence1"]
    before = copy.deepcopy(answer)
    with pytest.raises(DomainError) as error:
        validate_answer(answer, schema, frozen, context)
    fields = {item["path"]: item for item in error.value.field_errors}
    assert error.value.code == "SCHEMA_INVALID"
    assert "/market_cutoff" in fields and "/evidence_refs/0" in fields
    assert "疑似字符丢失" in error.value.detail
    assert "SYNTHETIC-evidence-1" in fields["/evidence_refs/0"]["message"]
    assert answer == before


def test_ambiguous_reference_is_not_guessed_and_wrong_types_remain_schema_errors(flow):
    request = flow.ready()
    with flow.w.db.read() as tx:
        _, schema, _ = flow.w.analysis.model_packet(request["request_id"], scope=tx)
        frozen = flow.w.analysis._docs.get(tx, "request", request["request_id"])
    context = copy.deepcopy(frozen["context"])
    context["evidence_refs"] += ["SYNTHETIC-evidence-1", "SYNTHETICevidence-1"]
    answer = json.loads(flow.answer_text)
    answer["market_cutoff"] = None
    answer["evidence_refs"] = ["SYNTHETICevidence1", {"invalid": True}]
    with pytest.raises(DomainError) as error:
        validate_answer(answer, schema, frozen, context)
    assert error.value.code == "SCHEMA_INVALID"
    assert not any("合法原值" in field["message"] for field in error.value.field_errors)


def test_network_trace_distinguishes_sent_body_wait_from_connection_failure_without_secrets():
    calls = []

    async def send(request):
        calls.append(request)
        trace = request.extensions["trace"]
        for event in ("connection.connect_tcp.started", "connection.connect_tcp.complete",
                      "connection.start_tls.started", "connection.start_tls.complete",
                      "http11.send_request_headers.started", "http11.send_request_headers.complete",
                      "http11.send_request_body.started", "http11.send_request_body.complete",
                      "http11.receive_response_headers.started", "http11.receive_response_headers.failed"):
            await trace(event, {"request": request, "secret": KEY})
        await trace("SYNTHETIC-untrusted-event-" + KEY, {"secret": KEY})
        raise httpx.RemoteProtocolError("SYNTHETIC-sensitive-exception-" + KEY)

    response = Gateway(httpx.MockTransport(send)).exchange(defaults(), KEY, b"{}")
    assert response.diagnostics["failure_kind"] == "remote_protocol_error"
    assert response.diagnostics["network_stage"] == "waiting_response_headers"
    assert len(response.diagnostics["network_events"]) == 10
    assert all(isinstance(item["elapsed_ms"], int) and item["elapsed_ms"] >= 0 for item in response.diagnostics["network_events"])
    assert KEY not in json.dumps(response.diagnostics) and "untrusted" not in json.dumps(response.diagnostics)
    with pytest.raises(DomainError) as error:
        extract(response, "responses")
    assert "请求已写出，等待响应头" in error.value.detail
    assert len(calls) == 1


@pytest.mark.parametrize("event,stage,detail", [
    ("connection.connect_tcp.started", "tcp_connecting", "正在建立 TCP 连接"),
    ("connection.start_tls.started", "tls_handshake", "正在 TLS 握手"),
])
def test_network_trace_is_bounded_and_reports_connection_phase(event, stage, detail):
    async def send(request):
        for _ in range(40):
            await request.extensions["trace"](event, {"exception": KEY})
        raise httpx.ConnectError(KEY)

    response = Gateway(httpx.MockTransport(send)).exchange(defaults(), KEY, b"{}")
    assert response.diagnostics["network_stage"] == stage
    assert len(response.diagnostics["network_events"]) == 32
    assert response.diagnostics["network_events_truncated"] is True
    assert KEY not in json.dumps(response.diagnostics)
    with pytest.raises(DomainError) as error:
        extract(response, "responses")
    assert detail in error.value.detail
