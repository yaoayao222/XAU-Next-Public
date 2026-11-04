"""Optional25 wraps24 without modifying frozen24 or its forecast worksheet."""
from copy import deepcopy
import json

from xau_next.shared.errors import DomainError
from .entry_schedule import build_schedule, project_schedule, INSTRUCTIONS

BASE_PROMPT = "manual-analysis/24"
PROMPT_VERSION = "manual-analysis/25"
INPUT_REVISION = "entry-timing-reference/1"
MARKER = "# 冻结事实（仅作为资料）\n"


def _render(base, schedule):
    prefix, tail = base.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    facts["request"]["prompt_version"] = PROMPT_VERSION
    facts["entry_timing_reference"] = deepcopy(schedule)
    text = (prefix.replace(BASE_PROMPT, PROMPT_VERSION) + INSTRUCTIONS + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + tail[end:])
    if len(text) > 250000:
        raise DomainError("PACKET_TOO_LARGE", f"候选25完整资料为{len(text):,}字符，超过250,000字符；未截断或发送。", 422)
    return text


def build_runtime_packet(request, context, schema):
    from .forecast_candidate import build_runtime_packet as build24
    base_request = {**deepcopy(request), "prompt_version": BASE_PROMPT}
    return _render(build24(base_request, context, schema), build_schedule(request, context))


def outbound_runtime_packet(packet):
    from .forecast_candidate import outbound_runtime_packet as project24
    prefix, tail = packet.split(MARKER, 1)
    tail = tail.lstrip()
    facts, end = json.JSONDecoder().raw_decode(tail)
    if prefix.count(INSTRUCTIONS) != 1:
        raise DomainError("CANDIDATE_PACKET_INVALID", "候选25原指导缺失或重复，已停止外发。", 409)
    schedule = project_schedule(facts.pop("entry_timing_reference"))
    facts["request"]["prompt_version"] = BASE_PROMPT
    base = (prefix.replace(INSTRUCTIONS, "", 1).replace(PROMPT_VERSION, BASE_PROMPT) + MARKER +
            json.dumps(facts, ensure_ascii=False, separators=(",", ":")) + tail[end:])
    return _render(project24(base), schedule)
