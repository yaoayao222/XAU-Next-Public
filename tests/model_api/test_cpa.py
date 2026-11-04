"""SYNTHETIC CPA-shaped transport only; no real CPA, credentials or network."""

import asyncio
import json
import socket
import sys

import httpx
import pytest
from xau_next.domains.settings.model_api import defaults
from xau_next.platform.model_gateway.public import Gateway, extract, normalize_base, request_body
from xau_next.shared.errors import DomainError

from tests.model_api.test_completion import update
from tests.model_api.test_gateway import envelope
from tests.model_api.test_workflow import KEY, ModelFlow

BASE = "http://127.0.0.1:8317/v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("CPA compatibility tests must not open a network connection")

    original_connect = socket.socket.connect
    socketpair_code = getattr(socket.socketpair, "__code__", None)

    def connect(sock, address):
        # Windows asyncio builds its own authenticated self-pipe with socketpair.
        # Exempt that exact stdlib caller, never arbitrary loopback connections.
        if socketpair_code is not None and sys._getframe(1).f_code is socketpair_code:
            return original_connect(sock, address)
        return denied()

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.mark.parametrize("port", [1, 8317, 65535])
@pytest.mark.parametrize("suffix", ["", "/"])
def test_explicit_loopback_http_base(port, suffix):
    assert normalize_base(f"http://127.0.0.1:{port}/v1{suffix}") == f"http://127.0.0.1:{port}/v1"


@pytest.mark.parametrize("base", [
    "http://localhost:8317/v1", "http://[::1]:8317/v1", "http://127.0.0.2:8317/v1",
    "http://127.1:8317/v1", "http://2130706433:8317/v1", "http://0.0.0.0:8317/v1",
    "http://192.168.1.2:8317/v1", "http://example.com:8317/v1",
    "http://127.0.0.1.example.com:8317/v1", "http://127.0.0.1/v1",
    "http://127.0.0.1:0/v1", "http://127.0.0.1:65536/v1", "http://127.0.0.1:bad/v1",
    "http://127.0.0.1:8317", "http://127.0.0.1:8317/dashboard",
    "http://127.0.0.1:8317/v1/chat/completions", "http://127.0.0.1:8317/v1/../v1",
    "http://127.0.0.1:8317/%76%31", "http://user:pass@127.0.0.1:8317/v1",
    "http://@127.0.0.1:8317/v1", "http://127.0.0.1:8317/v1?key=x",
    "http://127.0.0.1:8317/v1#fragment", "http://127.0.0.1:8317/v1?",
    "http://127.0.0.1:8317/v1#", "http://127.0.0.1:8317/v1\n",
    "https://example.com:invalid/v1", "https://[bad/v1",
])
def test_loopback_exception_does_not_weaken_url_validation(base):
    with pytest.raises(DomainError) as error:
        normalize_base(base)
    assert error.value.code == "MODEL_BASE_INVALID"


@pytest.mark.parametrize("protocol,fmt", [
    ("responses", "json_schema"), ("chat_completions", "json_schema"),
    ("chat_completions", "json_object"),
])
def test_local_request_bearer_and_explicit_parameters(protocol, fmt):
    config = {**defaults(), "base_url": BASE, "protocol": protocol, "output_format": fmt,
              "stream": True, "max_output_tokens": 25000, "timeout_seconds": 600}
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    body = request_body(config, "SYNTHETIC 中文 JSON", schema)
    requests = []

    def receive(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + KEY
        assert KEY.encode() not in request.content and not request.url.query
        return httpx.Response(401, json={"error": {"code": "invalid_api_key"}})

    result = Gateway(httpx.MockTransport(receive)).exchange(config, KEY, body)
    assert len(requests) == 1 and result.status == 401
    expected = "/responses" if protocol == "responses" else "/chat/completions"
    assert str(requests[0].url) == BASE + expected
    payload = json.loads(body)
    assert payload["model"] == "gpt-6.1-sol" and payload["stream"] is True
    if protocol == "responses":
        assert payload["max_output_tokens"] == 25000 and payload["reasoning"] == {"effort": "high"}
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True and payload["store"] is False
    else:
        assert payload["max_completion_tokens"] == 25000 and payload["reasoning_effort"] == "high"
        assert payload["response_format"]["type"] == fmt


class Chunks(httpx.AsyncByteStream):
    def __init__(self, raw, *, hold_open=False):
        self.raw, self.hold_open = raw, hold_open

    async def __aiter__(self):
        for start in range(0, len(self.raw), 17):
            yield self.raw[start:start + 17]
        if self.hold_open:
            await asyncio.sleep(30)


def stream(answer, protocol="responses", *, status="completed", terminal=True, hold_open=False):
    def event(value):
        return "data: " + json.dumps(value, ensure_ascii=False) + "\r\n\r\n"

    if protocol == "responses":
        raw = event({"type": "response.output_text.delta", "delta": answer})
        raw += event({"type": "response.output_text.done", "text": answer})
        if terminal:
            final = envelope(answer, status=status)
            if status == "incomplete":
                final["incomplete_details"] = {"reason": "max_output_tokens"}
            raw += event({"type": "response." + status, "response": final})
    else:
        raw = event({"model": "gpt-6.1-sol", "choices": [
            {"index": 0, "delta": {"content": answer}, "finish_reason": "stop"}]})
        raw += event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 34}})
        if terminal:
            raw += "data: [DONE]\r\n\r\n"
    return httpx.Response(200, stream=Chunks(raw.encode(), hold_open=hold_open),
                          headers={"content-type": "text/event-stream"})


@pytest.fixture
def flow(tmp_path):
    instance = ModelFlow(tmp_path)
    yield instance
    instance.close()


def test_saving_local_address_does_not_probe_or_reuse_remote_verification(flow):
    flow.ready()
    before = flow.get("/v1/model/settings")
    credentials = dict(flow.credentials.values)
    assert before["capability_verified"] and len(flow.sent) == 1
    saved = update(flow, base_url=BASE)
    assert saved["values"]["base_url"] == BASE and not saved["capability_verified"]
    assert flow.credentials.values == credentials and len(flow.sent) == 1
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 1


def ready(flow, protocol="responses"):
    flow.configure()

    def receive(request):
        payload = json.loads(request.content)
        prompt = payload.get("input") or payload["messages"][0]["content"]
        answer = flow.answer_text if "冻结事实" in prompt else json.dumps({
            "data_kind": "SYNTHETIC", "ok": True, "date_time": "2000-01-02T03:04:05.006Z",
            "reference": "probe-a_b--01", "signed_number": -12.5, "text": "中文"}, ensure_ascii=False)
        return stream(answer, protocol, hold_open=True)

    flow.handler = receive
    flow.setup_model(base_url=BASE, protocol=protocol, output_format="json_schema", stream=True,
                     timeout_seconds=600, max_output_tokens=25000, max_response_bytes=16000000)
    request = flow.request(flow.refresh())
    flow.answer_text = json.dumps(flow.answer(request), ensure_ascii=False)
    return request


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_local_settings_sse_full_answer_validation_and_import(flow, protocol):
    request = ready(flow, protocol)
    assert request["answer_schema_version"] == "1.7.0"
    before = flow.get("/v1/model/settings")
    assert before["values"]["base_url"] == BASE
    started = flow.start(request)
    assert flow.w.model_api.process_once(flow.w)
    call = flow.record(started)
    assert call["state"] == "succeeded" and call["stage"] == "imported"
    assert call["answer_valid"] and call["output_tokens"] == 34
    saved = flow.get("/v1/forecasts/" + request["forecast_id"])["current_version"]
    assert saved["raw_text"] == flow.answer_text and saved["version_id"] == call["version_id"]
    assert flow.get("/v1/model/calls/" + call["call_id"] + "/raw")["answer"] == flow.answer_text
    assert flow.start(request)["version_id"] == call["version_id"]
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2
    assert flow.get("/v1/model/settings") == before
    assert all(r.headers["authorization"] == "Bearer " + KEY for r in flow.sent)


@pytest.mark.parametrize("failure,code,state", [
    ("schema", "SCHEMA_INVALID", "failed"), ("business", "REQUEST_MISMATCH", "failed"),
    ("truncated", "MODEL_INCOMPLETE", "failed"), ("limit", "MODEL_OUTPUT_LIMIT", "failed"),
    ("upstream", "MODEL_UPSTREAM_FAILED", "failed"), ("auth", "MODEL_AUTH_FAILED", "failed"),
    ("rate", "MODEL_RATE_LIMITED", "failed"), ("redirect", "MODEL_HTTP_ERROR", "failed"),
    ("disconnect", "MODEL_CONNECTION_UNKNOWN", "outcome_unknown"),
    ("timeout", "MODEL_TIMEOUT_UNKNOWN", "outcome_unknown"),
    ("bytes", "RESPONSE_TOO_LARGE", "outcome_unknown"),
])
def test_local_failure_retains_raw_without_import_retry_or_fallback(flow, monkeypatch, failure, code, state):
    request = ready(flow)
    before = flow.get("/v1/model/settings")
    if failure in {"timeout", "bytes"}:
        original = flow.gateway.exchange

        def accelerated(config, key, body, **kwargs):
            # Exercise the real receive loop with small synthetic limits while
            # retaining the saved configuration and call identity unchanged.
            limits = {"timeout_seconds": 0.15} if failure == "timeout" else {"max_response_bytes": 32}
            return original({**config, **limits}, key, body, **kwargs)

        monkeypatch.setattr(flow.gateway, "exchange", accelerated)

    def receive(_request):
        if failure in {"timeout", "bytes"}:
            return httpx.Response(200, stream=Chunks(b"" if failure == "timeout" else b"x" * 33,
                                                    hold_open=failure == "timeout"))
        if failure == "disconnect":
            raise httpx.RemoteProtocolError("SYNTHETIC disconnect")
        if failure in {"auth", "rate", "redirect"}:
            return httpx.Response({"auth": 401, "rate": 429, "redirect": 307}[failure],
                                  json={"error": {}}, headers={"location": "https://elsewhere.invalid/v1"})
        answer = json.loads(flow.answer_text)
        if failure == "schema":
            del answer["summary"]
        if failure == "business":
            answer["request_id"] = "synthetic-wrong-request"
        return stream(json.dumps(answer, ensure_ascii=False), terminal=failure != "truncated",
                      status={"limit": "incomplete", "upstream": "failed"}.get(failure, "completed"))

    flow.handler = receive
    started = flow.start(request)
    assert flow.w.model_api.process_once(flow.w)
    call = flow.record(started)
    assert (call["state"], call["error_code"], call["version_id"]) == (state, code, None)
    assert call["raw_response"] is not None
    assert flow.get("/v1/workspace")["current_forecast"] is None
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2
    assert flow.get("/v1/model/settings") == before
    assert all(str(r.url) == BASE + "/responses" for r in flow.sent)
    assert all(json.loads(r.content)["model"] == "gpt-6.1-sol" for r in flow.sent)
    assert all(r.headers["authorization"] == "Bearer " + KEY for r in flow.sent)


@pytest.mark.parametrize("failure,code", [
    ("timeout", "MODEL_TIMEOUT_UNKNOWN"), ("bytes", "RESPONSE_TOO_LARGE"),
])
def test_local_receiving_limits_never_turn_into_token_limit_or_retry(failure, code):
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(200, stream=Chunks(b"" if failure == "timeout" else b"x" * 33,
                                                hold_open=failure == "timeout"))

    config = {**defaults(), "base_url": BASE, "timeout_seconds": 0.15, "max_response_bytes": 32}
    result = Gateway(httpx.MockTransport(receive)).exchange(config, KEY, b"{}")
    assert result.error_code == code and not result.complete and len(requests) == 1
    with pytest.raises(DomainError) as error:
        extract(result, "responses")
    assert error.value.code == code and len(requests) == 1
