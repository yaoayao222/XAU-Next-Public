"""Bounded protocol adapter. No persistence, domain computation, or automatic retry."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from urllib.parse import urlsplit, urlunsplit

import httpx
from jsonschema import Draft202012Validator
from xau_next.platform.model_gateway.deepseek_schema import annotate_union_types
from xau_next.shared.errors import DomainError

PROBE_VERSION = "synthetic-integrity/2"


def integrity_probe():
    """Synthetic constants test decoded values; JSON escapes are equivalent encodings."""
    sample = {
        "data_kind": "SYNTHETIC", "ok": True,
        "date_time": "2000-01-02T03:04:05.006Z",
        "reference": "probe-a_b--01", "signed_number": -12.5, "text": "中文",
    }
    types = {str: "string", bool: "boolean", float: "number"}
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {key: {"type": types[type(value)], "const": value} for key, value in sample.items()},
        "required": list(sample),
    }
    prompt = (
        "SYNTHETIC character integrity test only. Return exactly the following JSON object. "
        "Preserve every value, including date separators, hyphens, underscore, negative sign and Chinese text. "
        "Do not analyze markets.\n" + json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
    )
    return prompt, schema


def normalize_base(value):
    invalid = "API 地址须为 HTTPS；本机仅允许 http://127.0.0.1:端口/v1，且不能含凭据、查询参数或片段。"
    # urlsplit silently removes some controls; reject them before parsing. Never
    # resolve hostnames to decide whether plaintext HTTP is safe.
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise DomainError("MODEL_BASE_INVALID", invalid)
    try:
        url = urlsplit(value.strip())
        port = url.port
        has_credentials = url.username is not None or url.password is not None
    except ValueError as exc:
        raise DomainError("MODEL_BASE_INVALID", invalid) from exc
    if not url.hostname or has_credentials or "?" in value or "#" in value:
        raise DomainError("MODEL_BASE_INVALID", invalid)
    path = url.path.rstrip("/")
    loopback_http = (
        url.scheme == "http" and port is not None and 1 <= port <= 65535
        and url.netloc == f"127.0.0.1:{port}" and path == "/v1"
    )
    if url.scheme != "https" and not loopback_http:
        raise DomainError("MODEL_BASE_INVALID", invalid)
    if path.lower().endswith("/dashboard") or path not in ("", "/v1"):
        raise DomainError("MODEL_BASE_INVALID", "填写服务根地址或 /v1；管理页面不是 API 地址。")
    return urlunsplit((url.scheme, url.netloc, "/v1", "", ""))


def provider_schema(schema):
    """Generate a deliberately weaker supported subset; local original remains authoritative.

    Resolve version-dependent requirements from the frozen schema version, then emit the
    required properties. Optional legacy fields are omitted, not converted to invented nulls.
    """

    def merge(base, overlay):
        result = deepcopy(base)
        for key, value in overlay.items():
            if key == "required":
                result[key] = list(dict.fromkeys(result.get(key, []) + value))
            elif isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    effective = deepcopy(schema)
    version = schema.get("properties", {}).get("schema_version", {}).get("const")
    if version is not None:
        for branch in schema.get("allOf", []):
            if "if" in branch:
                match = Draft202012Validator(branch["if"]).is_valid({"schema_version": version})
                effective = merge(effective, branch.get("then" if match else "else", {}))

    def expand(node, stack=()):
        if isinstance(node, list):
            return [expand(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            reference = node["$ref"]
            if not reference.startswith("#/$defs/") or reference in stack:
                raise DomainError("MODEL_SCHEMA_UNSUPPORTED", "答案契约引用无法派生模型格式。", 500)
            target = schema["$defs"][reference.split("/")[-1]]
            return merge(
                expand(target, stack + (reference,)),
                expand({k: v for k, v in node.items() if k != "$ref"}, stack),
            )
        return {key: expand(value, stack) for key, value in node.items() if key != "$defs"}

    effective = expand(effective)
    allowed = {"type", "enum", "description", "items", "anyOf", "properties"}

    def convert(node):
        if isinstance(node, list):
            return [convert(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {key: value for key, value in node.items() if key in allowed}
        if "const" in node:
            result["enum"] = [node["const"]]
        for field in ("properties",):
            if field in result:
                result[field] = {
                    key: convert(value)
                    for key, value in result[field].items()
                    if key in node.get("required", [])
                }
        if "properties" in result:
            result.update(required=list(result["properties"]), additionalProperties=False)
        if "items" in result:
            result["items"] = convert(result["items"])
        if "anyOf" in result:
            result["anyOf"] = convert(result["anyOf"])
        if "oneOf" in node:
            result["anyOf"] = convert(node["oneOf"])
        return result

    return convert(effective)


def request_body(config, prompt, schema, *, web_search=False):
    fmt = {"type": config["output_format"]}
    if fmt["type"] == "json_schema" and config["protocol"] != "anthropic_messages":
        wire_schema = provider_schema(schema)
        if (
            config["protocol"] == "responses"
            and config.get("base_url", "").rstrip("/") in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
            and config["model_id"] in {"deepseek-flash", "deepseek-v4-pro"}
        ):
            wire_schema = annotate_union_types(wire_schema)
        fmt.update(name="xau_answer", schema=wire_schema, strict=True)
    common = {"model": config["model_id"], "stream": config["stream"]}
    effort = config["reasoning_effort"]
    if config["protocol"] == "responses":
        common.update(
            input=prompt, max_output_tokens=config["max_output_tokens"], text={"format": fmt}, store=False
        )
        if config["output_format"] == "plain_json":
            common.pop("text")
        if effort != "omit":
            common["reasoning"] = {"effort": effort}
    elif config["protocol"] == "chat_completions":
        common.update(
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {k: v for k, v in fmt.items() if k != "type"},
            }
            if fmt["type"] == "json_schema"
            else fmt,
        )
        token_parameter=config.get('chat_token_parameter','max_completion_tokens')
        if token_parameter not in {'max_tokens','max_completion_tokens'}:
            raise DomainError('MODEL_SETTINGS_INVALID','Chat输出上限参数无效。',409)
        common[token_parameter]=config['max_output_tokens']
        if effort != "omit":
            common["reasoning_effort"] = effort
        if config["output_format"] == "plain_json":
            common.pop("response_format")
        if config["stream"]:
            common["stream_options"] = {"include_usage": True}
    elif config["protocol"] == "anthropic_messages":
        # Anthropic Messages uses its own request shape; local schema
        # validation remains authoritative after extracting the text block.
        common = {
            "model": config["model_id"],
            "max_tokens": config["max_output_tokens"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": config["stream"],
        }
    else:
        raise DomainError("MODEL_PROTOCOL_UNSUPPORTED", "未支持的模型调用协议。", 422)
    if type(web_search) is not bool:
        raise DomainError("MODEL_SEARCH_POLICY_INVALID", "搜索开关必须明确为布尔值。", 409)
    if web_search:
        supported = config["model_id"] in {"gpt-6.1-sol", "gpt-6-astra", "deepseek-v4-pro"}
        if config["protocol"] != "responses" or not supported:
            raise DomainError("MODEL_SEARCH_UNSUPPORTED", "此接入仅开放已登记的三模型Responses搜索。", 409)
        common.update(tools=[{"type": "web_search"}], tool_choice="required")
        # DeepSeek currently ignores include. Missing source evidence still fails locally.
        common["include"] = ["web_search_call.action.sources"]
    return json.dumps(common, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class GatewayResult:
    raw: bytes = b""
    status: int | None = None
    request_id: str | None = None
    content_type: str = ""
    complete: bool = False
    error_code: str | None = None
    diagnostics: dict = dataclass_field(default_factory=dict)


class _SSEFrames:
    """Frame bytes before decoding, so split UTF-8 and CRLF chunks stay intact."""

    def __init__(self):
        self.pending = bytearray()
        self.data = []

    def feed(self, chunk):
        # Scan only the new chunk. A very long line split into tiny chunks must
        # not recopy or rescan its entire prefix at every network read.
        if not isinstance(self.pending, bytearray):
            self.pending = bytearray(self.pending)
        lines = chunk.split(b"\n")
        tail = lines.pop()
        frames = []
        for line in lines:
            if self.pending:
                self.pending.extend(line)
                line = bytes(self.pending)
                self.pending.clear()
            line = line.removesuffix(b"\r")
            if line.startswith(b"data:"):
                self.data.append(line[5:].removeprefix(b" "))
            elif not line and self.data:
                frames.append(b"\n".join(self.data))
                self.data = []
        self.pending.extend(tail)
        return frames


def _terminal_frame(frame, protocol):
    if protocol == "chat_completions":
        if frame == b"[DONE]":
            return True
        try:
            return _chat_error(json.loads(frame))
        except (ValueError, UnicodeError, RecursionError):
            return False
    try:
        event = json.loads(frame)
        if not isinstance(event, dict):
            return False
        if protocol == "anthropic_messages":
            return event.get("type") in {"message_stop", "error"}
        return (
            event.get("type") == "error"
            or event.get("type") in {"response.completed", "response.failed", "response.incomplete"}
            and isinstance(event.get("response"), dict)
        )
    except (ValueError, UnicodeError):
        return False


def _chat_error(event):
    # Never interpret provider prose, nested answer text, or an empty error as a failure.
    return isinstance(event, dict) and isinstance(event.get("error"), (dict, str)) and bool(event["error"])


def _chat_failure(chunks):
    if not any(_chat_error(chunk) for chunk in chunks):
        return None
    has_text = any(
        isinstance(choice, dict) and choice.get("index", 0) == 0
        and isinstance(choice.get("delta"), dict)
        and isinstance(choice["delta"].get("content"), str)
        and bool(choice["delta"]["content"].strip())
        for chunk in chunks if isinstance(chunk, dict)
        for choice in (chunk.get("choices") if isinstance(chunk.get("choices"), list) else [])
    )
    received = "已收到部分答案正文，但生成未正常完成" if has_text else "本次未收到答案正文"
    return {"code": "MODEL_UPSTREAM_FAILED", "answer_text_received": has_text,
            "detail": f"渠道报告上游生成失败。{received}，未保存可用预测；原响应已保留，不会自动重发。具体上游原因尚不明确。"}


def stream_failure_diagnosis(raw, protocol):
    """Bounded, read-only diagnosis of saved Chat SSE; never admits or repairs an answer."""
    if protocol != "chat_completions" or len(raw) > 16000000:
        return None
    try:
        frames = _SSEFrames().feed(raw)
        chunks = [json.loads(frame) for frame in frames if frame != b"[DONE]"]
        return _chat_failure(chunks)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return None


def _observe_stream_timing(frame, protocol, diagnostics, elapsed_ms):
    try:
        event = json.loads(frame)
    except (ValueError, UnicodeError, RecursionError):
        return
    if not isinstance(event, dict):
        return
    if diagnostics["first_sse_event_ms"] is None:
        diagnostics["first_sse_event_ms"] = elapsed_ms
    answer, reasoning = [], []
    if protocol == "chat_completions":
        choices = event.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                answer.append(delta.get("content"))
                reasoning.extend((delta.get("reasoning_content"), delta.get("reasoning")))
    elif protocol == "anthropic_messages" and event.get("type") == "content_block_delta":
        delta = event.get("delta")
        if isinstance(delta, dict):
            if delta.get("type") == "text_delta":
                answer.append(delta.get("text"))
            elif delta.get("type") == "thinking_delta":
                reasoning.append(delta.get("thinking"))
    elif event.get("type") == "response.output_text.delta":
        answer.append(event.get("delta"))
    elif event.get("type") in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
        reasoning.append(event.get("delta"))
    for key, values in (("first_answer_delta_ms", answer), ("first_reasoning_delta_ms", reasoning)):
        if diagnostics[key] is None and any(isinstance(value, str) and value.strip() for value in values):
            diagnostics[key] = elapsed_ms


_NETWORK_ERRORS = {
    httpx.ConnectTimeout: "connect_timeout",
    httpx.ReadTimeout: "read_timeout",
    httpx.WriteTimeout: "write_timeout",
    httpx.PoolTimeout: "pool_timeout",
    httpx.RemoteProtocolError: "remote_protocol_error",
    httpx.LocalProtocolError: "local_protocol_error",
    httpx.ConnectError: "connect_error",
    httpx.ReadError: "read_error",
    httpx.WriteError: "write_error",
    httpx.CloseError: "close_error",
    httpx.ProxyError: "proxy_error",
    httpx.DecodingError: "decoding_error",
}

_NETWORK_PHASES = {
    "connection.connect_tcp.started": "tcp_connecting",
    "connection.connect_tcp.complete": "tcp_connected",
    "connection.start_tls.started": "tls_handshake",
    "connection.start_tls.complete": "tls_ready",
}
for _protocol in ("http11", "http2"):
    for _operation, _started, _completed in (
        ("send_request_headers", "sending_request", "sending_request"),
        ("send_request_body", "sending_request", "request_sent"),
        ("receive_response_headers", "waiting_response_headers", "response_headers_received"),
    ):
        _NETWORK_PHASES[f"{_protocol}.{_operation}.started"] = _started
        _NETWORK_PHASES[f"{_protocol}.{_operation}.complete"] = _completed
_NETWORK_EVENTS = set(_NETWORK_PHASES) | {
    event.removesuffix(".started") + ".failed" for event in _NETWORK_PHASES if event.endswith(".started")
}


class Gateway:
    def __init__(self, transport=None):
        self.transport = transport

    def exchange(self, config, key, body, *, stopped=lambda: False, receiving=lambda: None, path=None):
        return asyncio.run(self._exchange(config, key, body, stopped, receiving, path))

    async def _exchange(self, config, key, body, stopped, receiving, path):
        result = GatewayResult()
        started = asyncio.get_running_loop().time()
        milliseconds = lambda: round((asyncio.get_running_loop().time() - started) * 1000)
        result.diagnostics = {
            "diagnostics_version": "model-network/2",
            "headers_ms": None, "first_byte_ms": None, "failure_kind": None,
            "first_sse_event_ms": None, "first_reasoning_delta_ms": None,
            "first_answer_delta_ms": None, "terminal_event_ms": None,
            "network_stage": "unknown", "network_events": [], "network_events_truncated": False,
        }
        raw = bytearray()

        async def trace(event, _info):
            # Never persist request/response objects, endpoints, headers or exceptions.
            if event not in _NETWORK_EVENTS:
                return
            if event in _NETWORK_PHASES:
                result.diagnostics["network_stage"] = _NETWORK_PHASES[event]
            if len(result.diagnostics["network_events"]) < 32:
                result.diagnostics["network_events"].append({"event": event, "elapsed_ms": milliseconds()})
            else:
                result.diagnostics["network_events_truncated"] = True

        async def fetch():
            endpoint = path or (
                "responses"
                if config["protocol"] == "responses"
                else "messages"
                if config["protocol"] == "anthropic_messages"
                else "chat/completions"
            )
            headers = {
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            }
            if config["protocol"] == "anthropic_messages":
                headers.update({
                    "Authorization": "Bearer " + key,
                    "anthropic-version": "2023-06-01",
                })
            else:
                headers["Authorization"] = "Bearer " + key
            async with (
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=httpx.Timeout(config["timeout_seconds"], connect=10),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "GET" if path else "POST",
                    config["base_url"] + "/" + endpoint,
                    headers=headers,
                    content=body if not path else None,
                    extensions={"trace": trace},
                ) as response,
            ):
                result.diagnostics["headers_ms"] = milliseconds()
                result.diagnostics["network_stage"] = "response_headers_received"
                result.status = response.status_code
                result.request_id = (
                    response.headers.get("x-request-id", response.headers.get("request-id", ""))[:200] or None
                )
                result.content_type = response.headers.get("content-type", "")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    result.error_code = "MODEL_ENCODING_UNSUPPORTED"
                    return
                first = True
                frames = _SSEFrames()
                async for chunk in response.aiter_bytes():
                    if first:
                        result.diagnostics["first_byte_ms"] = milliseconds()
                        result.diagnostics["network_stage"] = "receiving_body"
                        receiving()
                        first = False
                    if len(raw) + len(chunk) > config["max_response_bytes"]:
                        result.error_code = "RESPONSE_TOO_LARGE"
                        return
                    raw.extend(chunk)
                    if "text/event-stream" in result.content_type:
                        for frame in frames.feed(chunk):
                            elapsed = milliseconds()
                            _observe_stream_timing(frame, config["protocol"], result.diagnostics, elapsed)
                            if _terminal_frame(frame, config["protocol"]):
                                result.diagnostics["terminal_event_ms"] = elapsed
                                result.complete = True
                                result.diagnostics["network_stage"] = "response_received"
                                return
                result.complete = True
                result.diagnostics["network_stage"] = "response_received"

        task = asyncio.create_task(fetch())
        deadline = asyncio.get_running_loop().time() + config["timeout_seconds"]
        try:
            while not task.done():
                if stopped():
                    result.error_code = "MODEL_CANCELLED"
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    result.error_code = "MODEL_TIMEOUT_UNKNOWN"
                    result.diagnostics["failure_kind"] = "total_timeout"
                    break
                await asyncio.wait({task}, timeout=0.1)
            if task.done():
                await task
        except (httpx.HTTPError, OSError) as error:
            if not result.complete and result.error_code is None:
                result.error_code = "MODEL_CONNECTION_UNKNOWN"
            result.diagnostics["failure_kind"] = next(
                (name for kind, name in _NETWORK_ERRORS.items() if isinstance(error, kind)),
                "os_error" if isinstance(error, OSError) else "http_error",
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            result.raw = bytes(raw)
            result.diagnostics.update(elapsed_ms=milliseconds(), received_bytes=len(result.raw))
        return result


def audit_search_response(result):
    """Inspect public tool evidence only; never expose reasoning or infer a search from prose."""
    if "text/event-stream" in result.content_type:
        frames = [json.loads(frame) for frame in _SSEFrames().feed(result.raw) if frame != b"[DONE]"]
    else:
        frames = [json.loads(result.raw)]
    calls, sources = {}, {}

    def add_source(source):
        if not isinstance(source, dict):
            return
        url = source.get("url")
        if not isinstance(url, str) or len(url) > 2000:
            return
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                return
        except ValueError:
            return
        sources[url] = {"url": url, "title": str(source.get("title") or "")[:300]}

    def visit(value):
        if isinstance(value, list):
            for row in value:
                visit(row)
        elif isinstance(value, dict):
            if value.get("type") == "web_search_call":
                identity = value.get("id")
                if isinstance(identity, str) and identity:
                    old = calls.get(identity, {})
                    calls[identity] = {"id": identity[:200], "status": value.get("status", old.get("status"))}
                action = value.get("action") or {}
                if isinstance(action, dict):
                    for source in action.get("sources", []):
                        add_source(source)
                    # Reasoning providers may expose opened pages only through
                    # action.url, even when include=...sources is ignored.
                    if value.get("status") == "completed" and action.get("type") in {"open_page", "find_in_page"}:
                        add_source({"url": action.get("url"), "title": action.get("title", "")})
            if value.get("type") == "url_citation":
                add_source(value)
            # Restrict traversal to protocol structure. Do not parse generated prose as evidence.
            for key in ("response", "output", "item", "content", "annotations"):
                if key in value:
                    visit(value[key])
    for frame in frames:
        visit(frame)
    completed = [c for c in calls.values() if c.get("status") == "completed"]
    return {"version": "native-search-evidence/2", "completed_call_count": len(completed),
            "calls": list(calls.values()), "sources": list(sources.values()),
            "observed": bool(completed), "sources_observed": bool(sources),
            "source_availability_before_market_cutoff": "UNVERIFIED",
            "same_input_comparison": False}


def response_metadata(payload):
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    def tokens(*names):
        value = next((usage[name] for name in names if name in usage), None)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2147483647
            else None
        )

    return {
        "model_returned": str(payload["model"])[:200] if payload.get("model") else None,
        "provider_response_id": str(payload["id"])[:200] if payload.get("id") else None,
        "input_tokens": tokens("input_tokens", "prompt_tokens"),
        "output_tokens": tokens("output_tokens", "completion_tokens"),
    }


def redact_response(raw, key):
    """Preserve original bytes unless a known secret is found, including JSON string escaping."""
    changed = False

    def sanitize(value, depth=0):
        nonlocal changed
        if isinstance(value, dict):
            return {sanitize(k, depth + 1): sanitize(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(v, depth + 1) for v in value]
        if isinstance(value, str):
            if key in value:
                changed = True
                value = value.replace(key, "[REDACTED]")
            if depth < 12 and value.lstrip().startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                    scrubbed = sanitize(decoded, depth + 1)
                    if scrubbed != decoded:
                        value = json.dumps(scrubbed, ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
        return value

    try:
        decoded = json.loads(raw)
        scrubbed = sanitize(decoded)
        if changed:
            return json.dumps(scrubbed, ensure_ascii=False).encode(), True
    except (ValueError, UnicodeError, RecursionError):
        lines = []
        for line in raw.splitlines(keepends=True):
            if line.startswith(b"data:"):
                try:
                    decoded = json.loads(line[5:])
                    scrubbed = sanitize(decoded)
                    if scrubbed != decoded:
                        line = b"data: " + json.dumps(scrubbed, ensure_ascii=False).encode() + b"\n"
                except (ValueError, UnicodeError, RecursionError):
                    pass
            lines.append(line)
        if changed:
            raw = b"".join(lines)
    for encoded in (key.encode(), json.dumps(key)[1:-1].encode()):
        if encoded in raw:
            raw = raw.replace(encoded, b"[REDACTED]")
            changed = True
    return raw, changed


def extract(result, protocol):
    payload = None
    if result.error_code:
        detail = {
            "MODEL_TIMEOUT_UNKNOWN": "已达到本机设置的总超时，未取得完整响应；结果与计费未知，不会自动重发。",
            "MODEL_CANCELLED": "本机已停止接收；不保证中转站停止生成或计费，不会自动重发。",
            "RESPONSE_TOO_LARGE": "响应超过设置的字节上限，已停止接收且未导入；不会自动重发。",
            "MODEL_ENCODING_UNSUPPORTED": "中转站未按请求返回 identity 编码，响应未导入。",
        }.get(result.error_code, "调用未取得可确认的完整响应；结果与计费未知，不会自动重发。")
        if result.error_code == "MODEL_CONNECTION_UNKNOWN":
            descriptions = {
                "connect_timeout": "建立连接超时",
                "read_timeout": "等待响应数据超时",
                "write_timeout": "发送请求数据超时",
                "pool_timeout": "等待连接资源超时",
                "remote_protocol_error": "中转连接关闭或响应协议异常",
                "local_protocol_error": "本机请求协议异常",
                "connect_error": "建立连接失败",
                "read_error": "读取响应时连接中断",
                "write_error": "发送请求时连接中断",
                "close_error": "关闭响应连接异常",
                "proxy_error": "代理连接异常",
                "decoding_error": "响应编码异常",
                "os_error": "系统网络异常",
                "http_error": "网络请求异常",
            }
            reason = descriptions.get(result.diagnostics.get("failure_kind"), "网络连接异常")
            phase = "尚未收到响应头" if result.status is None else f"已收到 {len(result.raw)} 字节"
            phase = {
                "tcp_connecting": "正在建立 TCP 连接",
                "tcp_connected": "TCP 已连接",
                "tls_handshake": "正在 TLS 握手",
                "tls_ready": "TLS 握手已完成",
                "sending_request": "正在写出请求",
                "request_sent": "请求已写出，等待响应头",
                "waiting_response_headers": "请求已写出，等待响应头",
                "response_headers_received": "响应头已收到，等待正文",
                "receiving_body": f"接收正文中，已收到 {len(result.raw)} 字节",
            }.get(result.diagnostics.get("network_stage"), phase)
            detail = f"{reason}（{phase}）；结果与计费未知，不会自动重发。"
        raise DomainError(result.error_code, detail, 502)
    if not result.complete:
        raise DomainError("MODEL_OUTCOME_UNKNOWN", "响应不完整，结果未知；不会自动重发。", 502)
    if result.status != 200:
        code = {
            401: "MODEL_AUTH_FAILED",
            403: "MODEL_ACCESS_DENIED",
            402: "MODEL_QUOTA_EXHAUSTED",
            429: "MODEL_RATE_LIMITED",
            404: "MODEL_PROTOCOL_UNSUPPORTED",
            405: "MODEL_PROTOCOL_UNSUPPORTED",
        }.get(result.status, "MODEL_HTTP_ERROR")
        # Classify documented error codes only; provider free text is never logged or surfaced.
        try:
            payload = json.loads(result.raw)
            error = payload.get("error", {})
            raw_code = error.get("code") if isinstance(error, dict) else None
            if raw_code in ("insufficient_quota", "QUOTA_EXCEEDED", "INSUFFICIENT_BALANCE"):
                code = "MODEL_QUOTA_EXHAUSTED"
        except (ValueError, AttributeError):
            pass
        raise DomainError(code, f"中转站返回 HTTP {result.status}；未自动更换协议、参数或重试。", 502)
    try:
        if "text/event-stream" in result.content_type:
            events = [frame.decode("utf-8") for frame in _SSEFrames().feed(result.raw)]
            if protocol == "responses":
                finals = [json.loads(item) for item in events if item != "[DONE]"]
                failed = [item for item in finals if item.get("type") in {"response.failed", "error"}]
                if failed:
                    payload = failed[-1].get("response") or {}
                    raise DomainError(
                        "MODEL_UPSTREAM_FAILED",
                        "中转站明确返回上游生成失败（response.failed/error）；收到的文本不作为成功回答，未导入。",
                        502,
                    )
                done = [
                    item["response"]
                    for item in finals
                    if item.get("type") in {"response.completed", "response.incomplete"}
                ]
                if not done:
                    raise DomainError("MODEL_INCOMPLETE", "流式响应未包含完整结束事件，未导入。", 422)
                payload = done[-1]
            elif protocol == "chat_completions":
                chunks = [json.loads(item) for item in events if item != "[DONE]"]
                failure = _chat_failure(chunks)
                if failure:
                    raise DomainError(failure["code"], failure["detail"], 502)
                if not events or events[-1] != "[DONE]":
                    raise DomainError("MODEL_INCOMPLETE", "响应没有正常结束，未取得可确认完整的回答；原响应已保留，不会自动重发。", 422)
                choices = [c for item in chunks for c in item.get("choices", []) if c.get("index", 0) == 0]
                finish = next(
                    (c.get("finish_reason") for c in reversed(choices) if c.get("finish_reason")), None
                )
                payload = {
                    "model": next((i.get("model") for i in reversed(chunks) if i.get("model")), None),
                    "usage": next((i["usage"] for i in reversed(chunks) if i.get("usage")), {}),
                    "choices": [
                        {
                            "finish_reason": finish,
                            "message": {
                                "content": "".join(c.get("delta", {}).get("content") or "" for c in choices),
                                "refusal": any(c.get("delta", {}).get("refusal") for c in choices),
                            },
                        }
                    ],
                }
            elif protocol == "anthropic_messages":
                decoded = [json.loads(item) for item in events if item != "[DONE]"]
                if any(item.get("type") == "error" for item in decoded):
                    raise DomainError(
                        "MODEL_UPSTREAM_FAILED",
                        "中转站明确返回上游生成失败（error）；收到的文本不作为成功回答，未导入。",
                        502,
                    )
                if any(
                    item.get("type") == "content_block_start"
                    and isinstance(item.get("content_block"), dict)
                    and item["content_block"].get("type") == "refusal"
                    for item in decoded
                ):
                    raise DomainError("MODEL_REFUSED", "模型拒绝回答，原响应已保留。", 422)
                if not any(item.get("type") == "message_stop" for item in decoded):
                    raise DomainError("MODEL_INCOMPLETE", "流式响应未包含完整结束事件，未导入。", 422)
                start = next(
                    (item.get("message") for item in decoded if item.get("type") == "message_start"), {}
                )
                if not isinstance(start, dict):
                    start = {}
                deltas = [
                    item.get("delta") for item in decoded if item.get("type") == "content_block_delta"
                ]
                answer = "".join(
                    delta.get("text", "")
                    for delta in deltas
                    if isinstance(delta, dict) and delta.get("type") == "text_delta"
                )
                stop = next(
                    (
                        item.get("delta", {}).get("stop_reason")
                        for item in reversed(decoded)
                        if item.get("type") == "message_delta" and isinstance(item.get("delta"), dict)
                    ),
                    None,
                )
                usage = dict(start.get("usage") or {})
                final_usage = next(
                    (
                        item.get("usage")
                        for item in reversed(decoded)
                        if item.get("type") == "message_delta" and isinstance(item.get("usage"), dict)
                    ),
                    {},
                )
                usage.update(final_usage)
                payload = {
                    **{key: start[key] for key in ("id", "model") if key in start},
                    "content": [{"type": "text", "text": answer}],
                    "stop_reason": stop,
                    "usage": usage,
                }
            else:
                raise DomainError("MODEL_PROTOCOL_UNSUPPORTED", "未支持的模型调用协议。", 422)
        else:
            payload = json.loads(result.raw)
        if protocol == "responses":
            if payload.get("status") == "failed":
                raise DomainError("MODEL_UPSTREAM_FAILED", "中转站明确返回上游生成失败，未导入。", 502)
            if payload.get("status") != "completed":
                if (payload.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                    raise DomainError(
                        "MODEL_OUTPUT_LIMIT",
                        "生成达到输出 token 上限而截断，未导入；请检查输出预算后再明确发起。",
                        422,
                    )
                raise DomainError("MODEL_INCOMPLETE", "模型响应未完成或被截断，未导入。", 422)
            content = [
                part
                for item in payload.get("output", [])
                if item.get("type") == "message"
                for part in item.get("content", [])
            ]
            refused = any(part.get("type") == "refusal" for part in content)
            answer = "".join(part["text"] for part in content if part.get("type") == "output_text")
        elif protocol == "chat_completions":
            choice = payload["choices"][0]
            refused = bool(choice["message"].get("refusal"))
            if choice.get("finish_reason") != "stop" and not refused:
                if choice.get("finish_reason") == "length":
                    raise DomainError("MODEL_OUTPUT_LIMIT", "生成达到输出 token 上限而截断，未导入。", 422)
                raise DomainError("MODEL_INCOMPLETE", "模型响应未完成或被截断，未导入。", 422)
            answer = choice["message"].get("content") or ""
        elif protocol == "anthropic_messages":
            if payload.get("type") == "error" or isinstance(payload.get("error"), dict):
                raise DomainError("MODEL_UPSTREAM_FAILED", "中转站明确返回 Anthropic 上游失败，未导入。", 502)
            stop_reason = payload.get("stop_reason")
            if stop_reason == "refusal":
                raise DomainError("MODEL_REFUSED", "模型拒绝回答，原响应已保留。", 422)
            if stop_reason == "max_tokens":
                raise DomainError("MODEL_OUTPUT_LIMIT", "生成达到输出 token 上限而截断，未导入。", 422)
            if stop_reason not in {"end_turn", "stop_sequence"}:
                raise DomainError("MODEL_INCOMPLETE", "模型响应未完成或被截断，未导入。", 422)
            content = payload.get("content", [])
            refused = any(isinstance(part, dict) and part.get("type") == "refusal" for part in content)
            if refused:
                raise DomainError("MODEL_REFUSED", "模型拒绝回答，原响应已保留。", 422)
            answer = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            raise DomainError("MODEL_PROTOCOL_UNSUPPORTED", "未支持的模型调用协议。", 422)
        if refused:
            raise DomainError("MODEL_REFUSED", "模型拒绝回答，原响应已保留。", 422)
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("empty")
        return {"answer": answer, **response_metadata(payload)}
    except DomainError as exc:
        if isinstance(payload, dict):
            exc.model_metadata = response_metadata(payload)
        raise
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError, RecursionError) as exc:
        raise DomainError(
            "MODEL_RESPONSE_INVALID", "中转站响应结构无法提取完整回答，原响应已保留。", 422
        ) from exc
