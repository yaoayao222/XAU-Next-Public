"""Optional26 adds schema-derived document guidance to unmodified25 facts."""
from copy import deepcopy
import json

from xau_next.shared.errors import DomainError
from .json_document import build_shape, project_shape, INSTRUCTIONS

BASE_PROMPT = "manual-analysis/25"
PROMPT_VERSION = "manual-analysis/26"
MARKER = "# 冻结事实（仅作为资料）\n"


def _render(base, shape):
    prefix, tail = base.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    facts["request"]["prompt_version"] = PROMPT_VERSION
    facts["json_document_shape"] = deepcopy(shape)
    text = (prefix.replace(BASE_PROMPT, PROMPT_VERSION) + INSTRUCTIONS + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + tail[end:])
    if len(text) > 250000:
        raise DomainError("PACKET_TOO_LARGE", f"候选26完整资料为{len(text):,}字符，超过250,000字符；未截断或发送。", 422)
    return text


def build_runtime_packet(request, context, schema):
    from .timing_candidate import build_runtime_packet as build25
    from .package import effective_frozen_schema
    source = {**deepcopy(request), "prompt_version": BASE_PROMPT}
    return _render(build25(source, context, schema), build_shape(effective_frozen_schema(schema, request["answer_schema_version"])))


def outbound_runtime_packet(packet):
    from .timing_candidate import outbound_runtime_packet as project25
    prefix, tail = packet.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    if prefix.count(INSTRUCTIONS) != 1:
        raise DomainError("CANDIDATE_PACKET_INVALID", "候选26原结构指导缺失或重复，已停止外发。", 409)
    shape = project_shape(facts.pop("json_document_shape"))
    facts["request"]["prompt_version"] = BASE_PROMPT
    base = (prefix.replace(INSTRUCTIONS, "", 1).replace(PROMPT_VERSION, BASE_PROMPT) + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + tail[end:])
    return _render(project25(base), shape)
