"""SYNTHETIC document guidance and strict failure preservation; no real HTTP."""
import json
import threading
from copy import deepcopy

import pytest

from tests.experiments.test_model_selection import case as case
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.experiments.test_model_selection import gateway_for
from xau_next.domains.analysis.package import build_packet, frozen_answer_schema
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_output


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def document_request(request):
    value = deepcopy(request)
    value["prompt_version"] = "manual-analysis/18"
    value["packet_text"] = build_packet(
        {k: v for k, v in value.items() if k not in ("context", "packet_text")},
        value["context"], frozen_answer_schema(request))
    return value


def prepare(case, name):
    flow, old_root, old_plan, request, *_ = case
    value = document_request(request)
    plan = deepcopy(old_plan)
    plan.update(status="DRAFT", prompt_version="manual-analysis/18", input_revision="output-constraints/2")
    root = old_root.parent / name
    plan = seal_plan(root, plan)
    freeze_sample(root, "sample-0001", value, clock=flow.clock)
    return root, plan, value


def test_root_members_come_from_authority_and_prediction_facts_unchanged(case):
    original = deepcopy(case[3])
    value = document_request(original)
    api = facts(outbound_packet(value["packet_text"]))
    manual = facts(value["packet_text"])
    c = api["output_constraints"]
    schema = frozen_answer_schema(original)
    assert c == manual["output_constraints"]
    assert c["version"] == "output-constraints/2"
    doc = c["json_document"]
    assert doc["root_type"] == schema["type"] == "object"
    assert doc["additional_properties"] is schema["additionalProperties"] is False
    assert doc["required"] == schema["required"]
    assert [row["name"] for row in doc["root_members"]] == schema["required"]
    kinds = {row["name"]: row["types"] for row in doc["root_members"]}
    assert kinds["candidate_assessments"] == ["array"]
    assert kinds["experience_application"] == ["object"]
    assert kinds["prediction_adjustments"] == ["array"]
    assert all(kinds.values())
    baseline = facts(outbound_packet(original["packet_text"]))
    api.pop("output_constraints")
    api["request"]["prompt_version"] = baseline["request"]["prompt_version"]
    assert api == baseline
    assert original == case[3]
    assert frozen_answer_schema(value) == schema
    assert "不能先结束根对象再追加" in outbound_packet(value["packet_text"])


def test_prompt18_requires_its_own_plan_revision(case):
    plan = deepcopy(case[2])
    plan.update(status="DRAFT", prompt_version="manual-analysis/18", input_revision="output-constraints/1")
    with pytest.raises(DomainError):
        seal_plan(case[1].parent / "wrong-revision", plan)
    with pytest.raises(DomainError):
        freeze_sample(case[1], "sample-0001", document_request(case[3]), clock=case[0].clock)


def test_forged_document_structure_is_not_frozen(case):
    root, plan, value = prepare(case, "tamper-document")
    prefix, tail = value["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)
    data, end = json.JSONDecoder().raw_decode(tail)
    data["output_constraints"]["json_document"]["root_members"][0]["types"] = ["array"]
    value["packet_text"] = prefix + "# 冻结事实（仅作为资料）\n" + json.dumps(data, ensure_ascii=False) + tail[end:]
    # Use a fresh path; rejection must be semantic, not duplicate-sample protection.
    second = root.parent / "tamper-document-fresh"
    seal_plan(second, {**plan, "status": "DRAFT"})
    with pytest.raises(DomainError) as error:
        freeze_sample(second, "sample-0001", value, clock=case[0].clock)
    assert error.value.code == "SELECTION_PACKET_INVALID"


def test_document_projection_drops_unapproved_nested_fields(case):
    value = document_request(case[3])
    prefix, tail = value["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)
    data, end = json.JSONDecoder().raw_decode(tail)
    expected = deepcopy(data["output_constraints"])
    doc = data["output_constraints"]["json_document"]
    doc["private"] = "SYNTHETIC-private"
    doc["root_members"][0]["private"] = "SYNTHETIC-private"
    packet = prefix + "# 冻结事实（仅作为资料）\n" + json.dumps(data, ensure_ascii=False) + tail[end:]
    sent = outbound_packet(packet)
    assert facts(sent)["output_constraints"] == expected
    assert "SYNTHETIC-private" not in sent


def test_four_models_receive_identical_document_guidance(case):
    root, plan, value = prepare(case, "four-document")
    flow, _, _, _, answer, credentials, calls, auth = case
    sent = []
    results = run_sample(root, "sample-0001", authorization={**auth, "plan_sha256": digest(plan)},
                         gateway=gateway_for(answer, sent, barrier=threading.Barrier(4)),
                         credentials=credentials, calls=calls, clock=flow.clock)
    assert len(sent) == 4 and len({body["input"] for body in sent}) == 1
    assert all(r["schema_valid"] and r["business_valid"] for r in results.values())
    assert all(body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] for body in sent)


def test_original_failure_shape_still_fails_without_partial_json_repair(case):
    value = document_request(case[3])
    answer = deepcopy(case[4])
    experience = answer.pop("experience_application")
    adjustments = answer.pop("prediction_adjustments")
    raw = (json.dumps(answer, ensure_ascii=False) + ',"experience_application":'
           + json.dumps(experience, ensure_ascii=False) + ',"prediction_adjustments":'
           + json.dumps(adjustments, ensure_ascii=False) + "}").encode()
    before = bytes(raw)
    result = validate_output(raw, frozen_answer_schema(value), value, value["context"])
    assert result["schema_valid"] is False and result["business_valid"] is False
    assert result["error_code"] == "SCHEMA_INVALID" and raw == before
