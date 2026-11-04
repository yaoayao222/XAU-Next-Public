import asyncio
import json

import httpx
import pytest

from xau_next.domains.settings.model_api import defaults
from xau_next.platform.model_gateway.public import Gateway, _SSEFrames, extract
from tests.model_api.test_gateway import envelope


def frame(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\n\n"


def test_complete_saved_sse_buffer_is_not_rescanned_quadratically():
    scanned = []

    class CountedBytes(bytes):
        def __add__(self, other):
            return CountedBytes(super().__add__(other))

        def split(self, *args, **kwargs):
            scanned.append(len(self))
            return [CountedBytes(item) for item in super().split(*args, **kwargs)]

    record = b'data: {"choices":[{"delta":{"content":"SYNTHETIC"}}]}\r\n\r\n'
    raw = record * 2000
    parser = _SSEFrames()
    parser.pending = CountedBytes(b"")
    frames = parser.feed(raw)
    assert len(frames) == 2000 and not parser.pending
    assert sum(scanned) <= len(raw) * 2


def test_long_single_line_scans_each_chunk_once():
    scanned = []
    class CountedBytes(bytes):
        def __add__(self, other):
            return CountedBytes(super().__add__(other))
        def split(self, *args, **kwargs):
            scanned.append(len(self))
            return super().split(*args, **kwargs)
    raw = b'data: ' + b'x' * 200000 + b'\r\n\r\n'
    parser = _SSEFrames()
    parser.pending = CountedBytes(b'')
    actual = []
    for start in range(0,len(raw),37):
        actual.extend(parser.feed(CountedBytes(raw[start:start+37])))
    assert actual == [b'x'*200000]
    assert sum(scanned) <= len(raw)*2


def test_frame_parser_preserves_multiline_utf8_and_crlf_across_every_split():
    raw = ': 心跳\r\ndata: {"文本":\r\ndata: "中文"}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
    expected = ['{"文本":\n"中文"}'.encode(), b"[DONE]"]
    for split_at in range(len(raw) + 1):
        parser = _SSEFrames()
        assert parser.feed(raw[:split_at]) + parser.feed(raw[split_at:]) == expected


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
def test_heartbeat_role_reasoning_answer_and_terminal_have_separate_timing(protocol):
    if protocol == "chat_completions":
        events = [frame({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}),
                  frame({"choices": [{"index": 0, "delta": {"reasoning_content": "SYNTHETIC-private"}}]}),
                  frame({"choices": [{"index": 0, "delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}),
                  b"data: [DONE]\n\n"]
    elif protocol == "responses":
        events = [frame({"type": "response.created", "response": {}}),
                  frame({"type": "response.reasoning_summary_text.delta", "delta": "SYNTHETIC-private"}),
                  frame({"type": "response.output_text.delta", "delta": '{"ok":true}'}),
                  frame({"type": "response.completed", "response": envelope()})]
    else:
        events = [frame({"type": "message_start", "message": {"id": "synthetic", "model": "claude"}}),
                  frame({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "SYNTHETIC-private"}}),
                  frame({"type": "content_block_delta", "delta": {"type": "text_delta", "text": '{"ok":true}'}}),
                  frame({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                  frame({"type": "message_stop"})]
    chunks = [b": ping\n\n", *events]
    sent = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in chunks:
                await asyncio.sleep(0.015)
                for start in range(0, len(chunk), 7):
                    yield chunk[start:start + 7]
            raise AssertionError("must stop at the complete terminal frame")

    def send(request):
        sent.append(request)
        return httpx.Response(200, stream=Stream(), headers={"content-type": "text/event-stream"})

    result = Gateway(httpx.MockTransport(send)).exchange({**defaults(), "protocol": protocol, "stream": True}, "SYNTHETIC-KEY", b"{}")
    assert result.complete and result.error_code is None and len(sent) == 1
    assert result.raw == b"".join(chunks)
    timing = result.diagnostics
    assert timing["diagnostics_version"] == "model-network/2"
    assert timing["headers_ms"] <= timing["first_byte_ms"] < timing["first_sse_event_ms"]
    assert timing["first_sse_event_ms"] < timing["first_reasoning_delta_ms"] < timing["first_answer_delta_ms"]
    assert timing["first_answer_delta_ms"] < timing["terminal_event_ms"] <= timing["elapsed_ms"]
    assert "SYNTHETIC" not in json.dumps(timing)
    assert extract(result, protocol)["answer"] == '{"ok":true}'


@pytest.mark.parametrize("protocol", ["chat_completions", "responses", "anthropic_messages"])
def test_terminal_only_or_failed_response_does_not_invent_answer_delta_time(protocol):
    raw = (b'data: {"choices":[],"usage":{"prompt_tokens":2}}\n\ndata: [DONE]\n\n'
           if protocol == "chat_completions" else
           frame({"type": "response.failed", "response": {"status": "failed"}})
           if protocol == "responses" else
           frame({"type": "message_stop"}))
    result = Gateway(httpx.MockTransport(lambda request: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))).exchange(
        {**defaults(), "protocol": protocol}, "SYNTHETIC", b"{}")
    assert result.diagnostics["first_answer_delta_ms"] is None
    assert result.diagnostics["first_reasoning_delta_ms"] is None
    assert result.diagnostics["terminal_event_ms"] is not None


def test_non_streaming_response_has_no_stream_delta_timestamps():
    result = Gateway(httpx.MockTransport(lambda request: httpx.Response(200, json=envelope()))).exchange(defaults(), "SYNTHETIC", b"{}")
    assert result.complete
    assert all(result.diagnostics[key] is None for key in ("first_sse_event_ms", "first_reasoning_delta_ms", "first_answer_delta_ms", "terminal_event_ms"))


def test_malformed_events_do_not_change_transport_outcome_or_leak_values():
    raw = b'data: malformed\n\ndata: []\n\ndata: {"choices":[null,{}]}\n\ndata: [DONE]\n\n'
    result = Gateway(httpx.MockTransport(lambda request: httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"}))).exchange(
        {**defaults(), "protocol": "chat_completions"}, "SYNTHETIC", b"{}")
    assert result.complete and result.raw == raw
    assert result.diagnostics["first_answer_delta_ms"] is None
