"""SYNTHETIC protocol fixtures; never contact a real provider."""

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator
from xau_next.domains.settings.model_api import defaults
from xau_next.platform.model_gateway.public import (
    Gateway,
    GatewayResult,
    extract,
    normalize_base,
    provider_schema,
    redact_response,
    request_body,
)
from xau_next.shared.errors import DomainError


def result(payload, *, protocol="responses", status=200, content_type="application/json"):
    raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    return GatewayResult(raw=raw, status=status, complete=True, content_type=content_type)


def envelope(answer='{"ok":true}', **changes):
    return {
        "status": "completed",
        "model": "provider-declared-model",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
        "usage": {"input_tokens": 12, "output_tokens": 34},
        **changes,
    }


def test_explicit_protocol_parameters_and_authoritative_schema_subset():
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/analysis-answer.schema.json").read_text(
            encoding="utf-8"
        )
    )
    schema["properties"]["schema_version"] = {"type": "string", "const": "1.3.0"}
    reduced = provider_schema(schema)
    encoded = json.dumps(reduced)
    assert '"if"' not in encoded and '"then"' not in encoded and '"allOf"' not in encoded
    assert "analysis_basis" in reduced["required"] and "candidate_assessments" in reduced["required"]
    assert "post_activation_invalidation" in reduced["properties"]["plans"]["items"]["required"]
    config = defaults()
    response = json.loads(request_body(config, "JSON", schema))
    assert response["reasoning"] == {"effort": "high"} and response["model"] == "gpt-6.1-sol"
    assert response["text"]["format"]["strict"] is True
    config.update(protocol="chat_completions", reasoning_effort="omit", output_format="json_object")
    chat = json.loads(request_body(config, "JSON", schema))
    assert "reasoning" not in chat and "reasoning_effort" not in chat
    assert chat["response_format"] == {"type": "json_object"}
    config.update(protocol="anthropic_messages", reasoning_effort="high", output_format="json_schema")
    anthropic = json.loads(request_body(config, "JSON", schema))
    assert anthropic == {
        "model": "gpt-6.1-sol",
        "max_tokens": 12000,
        "messages": [{"role": "user", "content": "JSON"}],
        "stream": False,
    }


@pytest.mark.parametrize(
    "value", ["https://api.zyuou.com", "https://api.zyuou.com/", "https://api.zyuou.com/v1/"]
)
def test_base_normalization(value):
    assert normalize_base(value) == "https://api.zyuou.com/v1"


@pytest.mark.parametrize(
    "value",
    [
        "https://api.zyuou.com/dashboard",
        "https://api.zyuou.com/v1/v1",
        "https://user:key@api.zyuou.com",
        "http://api.zyuou.com",
        "https://api.zyuou.com?key=x",
    ],
)
def test_unsafe_or_dashboard_base_rejected(value):
    with pytest.raises(DomainError):
        normalize_base(value)


def test_normal_and_sse_responses_extract_only_completed_final():
    final = envelope()
    assert extract(result(final), "responses")["output_tokens"] == 34
    raw = "data: " + json.dumps({"type": "response.output_text.delta", "delta": "wrong partial"}) + "\n\n"
    raw += "data: " + json.dumps({"type": "response.completed", "response": final}) + "\n\n"
    assert extract(result(raw, content_type="text/event-stream"), "responses")["answer"] == '{"ok":true}'
    with pytest.raises(DomainError, match="结束事件"):
        extract(
            result(
                'data: {"type":"response.output_text.delta","delta":"{}"}\n\n',
                content_type="text/event-stream",
            ),
            "responses",
        )


def test_chat_sse_requires_finish_and_done_and_keeps_usage():
    events = [
        {"choices": [{"index": 0, "delta": {"content": '{"ok":'}}]},
        {
            "model": "declared",
            "choices": [{"index": 0, "delta": {"content": "true}"}, "finish_reason": "stop"}],
        },
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 6}},
    ]
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    value = extract(result(raw + "data: [DONE]\n\n", content_type="text/event-stream"), "chat_completions")
    assert value["answer"] == '{"ok":true}' and value["input_tokens"] == 5
    with pytest.raises(DomainError):
        extract(result(raw, content_type="text/event-stream"), "chat_completions")


def test_anthropic_messages_extracts_complete_response_and_stream():
    payload = {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "model": "claude-synthetic",
        "content": [{"type": "text", "text": '{"ok":true}'}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 8},
    }
    value = extract(result(payload), "anthropic_messages")
    assert value["answer"] == '{"ok":true}'
    assert value["input_tokens"] == 7 and value["output_tokens"] == 8
    events = [
        {"type": "message_start", "message": {"id": "msg_synthetic", "model": "claude-synthetic", "usage": {"input_tokens": 7}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"ok":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "true}"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 8}},
        {"type": "message_stop"},
    ]
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    stream_value = extract(result(raw, content_type="text/event-stream"), "anthropic_messages")
    assert stream_value["answer"] == '{"ok":true}'
    assert stream_value["provider_response_id"] == "msg_synthetic"


@pytest.mark.parametrize(
    "stop_reason,code",
    [("max_tokens", "MODEL_OUTPUT_LIMIT"), ("refusal", "MODEL_REFUSED"), ("tool_use", "MODEL_INCOMPLETE")],
)
def test_anthropic_messages_never_imports_truncated_refused_or_tool_turns(stop_reason, code):
    payload = {"content": [{"type": "text", "text": "SYNTHETIC"}], "stop_reason": stop_reason}
    with pytest.raises(DomainError) as error:
        extract(result(payload), "anthropic_messages")
    assert error.value.code == code


def test_anthropic_messages_refusal_block_and_upstream_error_are_not_answers():
    refused = {
        "content": [{"type": "refusal", "text": "SYNTHETIC refusal"}],
        "stop_reason": "end_turn",
    }
    with pytest.raises(DomainError) as refusal:
        extract(result(refused), "anthropic_messages")
    assert refusal.value.code == "MODEL_REFUSED"

    error_payload = {"type": "error", "error": {"type": "overloaded_error"}}
    with pytest.raises(DomainError) as upstream:
        extract(result(error_payload), "anthropic_messages")
    assert upstream.value.code == "MODEL_UPSTREAM_FAILED"


def test_anthropic_messages_uses_bearer_version_header_and_messages_path():
    config = {**defaults(), "base_url": "https://xcmapi.org/v1", "protocol": "anthropic_messages"}
    body = request_body(config, "SYNTHETIC", {"type": "object"})
    sent = []

    def receive(request):
        sent.append(request)
        return httpx.Response(401, json={"error": {"type": "authentication_error"}})

    response = Gateway(httpx.MockTransport(receive)).exchange(config, "SYNTHETIC-KEY", body)
    assert response.status == 401 and len(sent) == 1
    assert str(sent[0].url) == "https://xcmapi.org/v1/messages"
    assert sent[0].headers["authorization"] == "Bearer SYNTHETIC-KEY"
    assert sent[0].headers["anthropic-version"] == "2023-06-01"
    assert sent[0].headers.get("x-api-key") is None
    assert b"SYNTHETIC-KEY" not in sent[0].content
    assert json.loads(sent[0].content) == json.loads(body)
    assert not {"response_format", "reasoning_effort", "stream_options"}.intersection(json.loads(body))


def test_anthropic_messages_check_requests_keep_version_and_bearer_headers():
    config = {**defaults(), "base_url": "https://xcmapi.org/v1", "protocol": "anthropic_messages"}
    sent = []

    def receive(request):
        sent.append(request)
        return httpx.Response(200, json={"data": []})

    response = Gateway(httpx.MockTransport(receive)).exchange(config, "SYNTHETIC-KEY", b"", path="models")
    assert response.complete and len(sent) == 1
    assert str(sent[0].url) == "https://xcmapi.org/v1/models"
    assert sent[0].headers["authorization"] == "Bearer SYNTHETIC-KEY"
    assert sent[0].headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize(
    "payload,code",
    [
        (envelope(status="incomplete"), "MODEL_INCOMPLETE"),
        (
            envelope(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]),
            "MODEL_REFUSED",
        ),
        ("not JSON", "MODEL_RESPONSE_INVALID"),
    ],
)
def test_refusal_truncation_invalid_json(payload, code):
    with pytest.raises(DomainError) as error:
        extract(result(payload), "responses")
    assert error.value.code == code


@pytest.mark.parametrize(
    "status,payload,code",
    [
        (401, {}, "MODEL_AUTH_FAILED"),
        (429, {}, "MODEL_RATE_LIMITED"),
        (429, {"error": {"code": "insufficient_quota"}}, "MODEL_QUOTA_EXHAUSTED"),
        (404, {}, "MODEL_PROTOCOL_UNSUPPORTED"),
    ],
)
def test_http_errors_have_safe_distinct_codes(status, payload, code):
    with pytest.raises(DomainError) as error:
        extract(result(payload, status=status), "responses")
    assert error.value.code == code


def test_no_automatic_retry_redirect_or_protocol_fallback():
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(307, headers={"location": "https://elsewhere.invalid/"})

    response = Gateway(httpx.MockTransport(send)).exchange(defaults(), "SYNTHETIC-KEY", b"{}")
    assert len(calls) == 1 and calls[0].url.path == "/v1/responses" and response.status == 307


def test_response_size_is_bounded_and_cancellation_interrupts_silent_stream():
    cfg = {**defaults(), "max_response_bytes": 8}
    response = Gateway(
        httpx.MockTransport(lambda request: httpx.Response(200, content=b"123456789"))
    ).exchange(cfg, "SYNTHETIC-KEY", b"{}")
    assert response.error_code == "RESPONSE_TOO_LARGE" and not response.complete and len(response.raw) <= 8

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(30)
            yield b"late"

    checks = []

    def stopped():
        checks.append(True)
        return len(checks) > 2

    response = Gateway(httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow()))).exchange(
        defaults(), "SYNTHETIC-KEY", b"{}", stopped=stopped
    )
    assert response.error_code == "MODEL_CANCELLED" and not response.complete


def test_total_deadline_interrupts_silent_stream_and_does_not_retry():
    class Silent(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(30)
            yield b"late"

    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, stream=Silent())

    start = time.monotonic()
    result = Gateway(httpx.MockTransport(send)).exchange(
        {**defaults(), "timeout_seconds": 0.2}, "SYNTHETIC", b"{}"
    )
    assert time.monotonic() - start < 2
    assert result.error_code == "MODEL_TIMEOUT_UNKNOWN" and len(calls) == 1


def test_current_and_legacy_version_schema_requirements_are_derived_without_parallel_definitions():
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
    current = json.loads((root / "fixtures/analysis-answer.staged.json").read_text(encoding="utf-8"))
    for version in ("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"):
        schema["properties"]["schema_version"] = {"type": "string", "const": version}
        reduced = provider_schema(schema)
        plan = reduced["properties"]["plans"]["items"]
        assert ("candidate_assessments" in reduced["required"]) == (version in ("1.2.0", "1.3.0", "1.4.0", "1.5.0"))
        assert ("analysis_basis" in reduced["required"]) == (version != "1.0.0")
        assert ("post_activation_invalidation" in plan["required"]) == (version in ("1.3.0", "1.4.0", "1.5.0"))
        assert ("holding_time_basis" in plan["required"]) == (version == "1.5.0")
        assert ("experience_application" in reduced["required"]) == (version in ("1.4.0", "1.5.0"))
        assert ("prediction_adjustments" in reduced["required"]) == (version in ("1.4.0", "1.5.0"))
        if version in ("1.4.0", "1.5.0"):
            sample = json.loads((root / "fixtures/analysis-answer.experience.json").read_text(encoding="utf-8"))
            sample["schema_version"] = version
            if version == "1.5.0":
                for candidate in sample["plans"]:
                    candidate["holding_time_basis"] = None
            Draft202012Validator(reduced).validate(sample)
        if version == "1.3.0":
            Draft202012Validator(reduced).validate(current)
    plain = json.loads(request_body({**defaults(), "output_format": "plain_json"}, "JSON", schema))
    assert "text" not in plain


def test_secret_redaction_handles_nested_json_unicode_escape_and_preserves_other_raw_bytes():
    raw = b'{"status":"completed", "nothing":42}\n'
    assert redact_response(raw, "SYNTHETIC-key") == (raw, False)
    hidden = "".join(f"\\u{ord(c):04x}" for c in "SYNTHETIC-key")
    echoed = json.dumps(envelope('{"summary":"' + hidden + '"}')).encode()
    safe, changed = redact_response(echoed, "SYNTHETIC-key")
    assert (
        changed
        and "SYNTHETIC-key"
        not in json.loads(extract(result(safe.decode()), "responses")["answer"])["summary"]
    )


def test_failed_stream_with_completed_text_is_not_accepted_as_success():
    # A real relay defect: output_text.done and an empty completed event precede failure.
    events = (
        'event: response.output_text.done\r\ndata: {"type":"response.output_text.done","text":"{}"}\r\n\r\n'
        "event: response.completed\r\n\r\n"
        "event: response.failed\r\ndata: "
        + json.dumps(
            {
                "type": "response.failed",
                "response": {
                    "id": "synthetic-failed-id",
                    "model": "declared",
                    "status": "failed",
                    "error": {"code": "upstream_error", "message": "SYNTHETIC-SECRET"},
                    "usage": {"input_tokens": 9, "output_tokens": 3},
                },
            }
        )
        + "\r\n\r\n"
    )
    with pytest.raises(DomainError) as failure:
        extract(result(events, content_type="text/event-stream"), "responses")
    assert failure.value.code == "MODEL_UPSTREAM_FAILED"
    assert "SYNTHETIC-SECRET" not in failure.value.detail
    assert failure.value.model_metadata["provider_response_id"] == "synthetic-failed-id"
    assert failure.value.model_metadata["output_tokens"] == 3


@pytest.mark.parametrize("stream", [False, True])
def test_output_limit_is_distinct_and_preserves_reported_usage(stream):
    final = envelope(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    response = result(final)
    if stream:
        response = result(
            "data: " + json.dumps({"type": "response.incomplete", "response": final}) + "\n\n",
            content_type="text/event-stream",
        )
    with pytest.raises(DomainError) as failure:
        extract(response, "responses")
    assert failure.value.code == "MODEL_OUTPUT_LIMIT"
    assert failure.value.model_metadata["output_tokens"] == 34


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_complete_terminal_frame_does_not_wait_for_socket_close(protocol):
    terminal = (
        "data: "
        + json.dumps(
            {"type": "response.completed", "response": envelope('{"文字":"完成"}')}, ensure_ascii=False
        )
        + "\r\n\r\n"
        if protocol == "responses"
        else "data: [DONE]\r\n\r\n"
    ).encode()
    consumed = []

    class KeptOpen(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(0, len(terminal), 7):
                yield terminal[index : index + 7]
            consumed.append("read beyond terminal")
            await asyncio.sleep(30)

    sent = []

    def send(request):
        sent.append(request)
        return httpx.Response(200, stream=KeptOpen(), headers={"content-type": "text/event-stream"})

    value = Gateway(httpx.MockTransport(send)).exchange(
        {**defaults(), "protocol": protocol, "stream": True, "timeout_seconds": 0.3}, "SYNTHETIC", b"{}"
    )
    assert value.complete and value.error_code is None and not consumed and len(sent) == 1
    assert value.raw == terminal and value.diagnostics["received_bytes"] == len(terminal)
    assert value.diagnostics["headers_ms"] is not None and value.diagnostics["first_byte_ms"] is not None
    if protocol == "responses":
        assert extract(value, protocol)["answer"] == '{"文字":"完成"}'


def test_empty_terminal_name_does_not_stop_receiving_or_hide_upstream_failure():
    raw = b"event: response.completed\n\n"
    raw += b'data: {"type":"response.failed","response":{"status":"failed","model":"test"}}\n\n'

    class Relay(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw[:27]
            yield raw[27:]
            raise AssertionError("terminal frame must end the stream")

    value = Gateway(
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Relay(), headers={"content-type": "text/event-stream"})
        )
    ).exchange(defaults(), "SYNTHETIC", b"{}")
    assert value.complete and value.raw == raw
    with pytest.raises(DomainError) as failure:
        extract(value, "responses")
    assert failure.value.code == "MODEL_UPSTREAM_FAILED"


@pytest.mark.parametrize(
    "error,kind",
    [
        (httpx.RemoteProtocolError, "remote_protocol_error"),
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.ConnectTimeout, "connect_timeout"),
        (httpx.WriteError, "write_error"),
    ],
)
def test_network_diagnostics_are_safe_and_never_retry(error, kind):
    sent = []

    def send(request):
        sent.append(request)
        raise error("SYNTHETIC-SECRET https://private.invalid?key=SYNTHETIC-SECRET")

    value = Gateway(httpx.MockTransport(send)).exchange(defaults(), "SYNTHETIC-SECRET", b"{}")
    assert value.error_code == "MODEL_CONNECTION_UNKNOWN" and not value.complete and len(sent) == 1
    assert value.diagnostics["failure_kind"] == kind and value.diagnostics["headers_ms"] is None
    assert value.diagnostics["received_bytes"] == 0 and value.diagnostics["elapsed_ms"] >= 0
    assert "SYNTHETIC-SECRET" not in json.dumps(value.diagnostics)
    with pytest.raises(DomainError) as failure:
        extract(value, "responses")
    assert "尚未收到响应头" in failure.value.detail
    assert "SYNTHETIC-SECRET" not in failure.value.detail


def test_partial_stream_disconnect_retains_raw_and_never_promotes_partial_json():
    raw = b'data: {"type":"response.output_text.done","text":"{}"}\n\n'

    class Dropped(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw
            raise httpx.ReadError("SYNTHETIC-SECRET")

    value = Gateway(
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Dropped(), headers={"content-type": "text/event-stream"})
        )
    ).exchange(defaults(), "SYNTHETIC-SECRET", b"{}")
    assert value.raw == raw and not value.complete and value.diagnostics["failure_kind"] == "read_error"
    with pytest.raises(DomainError) as failure:
        extract(value, "responses")
    assert failure.value.code == "MODEL_CONNECTION_UNKNOWN" and "读取响应时连接中断" in failure.value.detail


def test_close_failure_after_verified_terminal_does_not_discard_complete_response():
    raw = ("data: " + json.dumps({"type": "response.completed", "response": envelope()}) + "\n\n").encode()

    class CloseFails(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield raw

        async def aclose(self):
            raise httpx.CloseError("SYNTHETIC secret must not be logged")

    value = Gateway(
        httpx.MockTransport(
            lambda _: httpx.Response(200, stream=CloseFails(), headers={"content-type": "text/event-stream"})
        )
    ).exchange(defaults(), "SYNTHETIC", b"{}")
    assert value.complete and value.error_code is None and value.diagnostics["failure_kind"] == "close_error"
    assert extract(value, "responses")["answer"] == '{"ok":true}'
