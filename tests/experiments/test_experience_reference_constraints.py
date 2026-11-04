"""SYNTHETIC reference-copy failure regression; all transport is MockTransport."""
import json
import threading
from copy import deepcopy

import pytest

from tests.experiments.test_model_selection import case as case
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.experiments.test_model_selection import gateway_for
from tests.model_api.test_experience_projection import synthetic_experience
from xau_next.domains.analysis.experience import non_experience_references
from xau_next.domains.analysis.package import build_packet, frozen_answer_schema
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_output


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def linked_case(case):
    request, answer = deepcopy(case[3]), deepcopy(case[4])
    request["prompt_version"] = "manual-analysis/19"
    exp = synthetic_experience()
    ref = exp["cases"][0]["review_ref"]
    request["context"]["experience"] = exp
    request["context"]["evidence_refs"] += [exp["digest_id"], ref]
    answer["experience_application"] = {
        "digest_id": exp["digest_id"], "status": "applied",
        "case_assessments": [{"review_ref": ref, "relevance": "partial",
            "current_evidence_refs": [request["market_snapshot_id"]],
            "similarities": ["SYNTHETIC结构相似"], "differences": ["SYNTHETIC周期不同"],
            "decision": "adopt", "reason": "仅提示适用边界，不推导反向规则。"}],
        "limitations": ["SYNTHETIC，仅验证引用，不证明改善。"],
    }
    answer["prediction_adjustments"] = [{"horizon_minutes": 30, "aspects": ["direction"],
        "related_review_refs": [ref], "action": "retained", "explanation": "当前事实支持保持判断。"}]
    request["packet_text"] = build_packet(
        {k:v for k,v in request.items() if k not in ("context", "packet_text")},
        request["context"], frozen_answer_schema(request))
    return request, answer


def checked(request, answer):
    return validate_output(json.dumps(answer, ensure_ascii=False).encode(),
                           frozen_answer_schema(request), request, request["context"])


def test_reference_whitelists_only_repeat_frozen_identities_and_keep_prediction_facts(case):
    request, _ = linked_case(case)
    before = deepcopy(request)
    manual, api = facts(request["packet_text"]), facts(outbound_packet(request["packet_text"]))
    links = api["output_constraints"]["experience_links"]
    assert links == manual["output_constraints"]["experience_links"]
    exp = request["context"]["experience"]
    assert links["digest_id"] == exp["digest_id"]
    assert links["allowed_case_review_refs"] == [c["review_ref"] for c in exp["cases"]]
    assert links["allowed_current_evidence_refs"] == sorted(non_experience_references(request["context"]))
    assert exp["digest_id"] not in links["allowed_case_review_refs"]
    baseline = deepcopy(request)
    baseline["prompt_version"] = "manual-analysis/18"
    old = facts(outbound_packet(build_packet(
        {k:v for k,v in baseline.items() if k not in ("context", "packet_text")},
        baseline["context"], frozen_answer_schema(baseline))))
    doc = api["output_constraints"].pop("json_document")
    assert doc == old["output_constraints"]["json_document"]
    api.pop("output_constraints"); old.pop("output_constraints")
    api["request"]["prompt_version"] = old["request"]["prompt_version"]
    assert api == old and request == before
    assert "不能重新拼写" in outbound_packet(request["packet_text"])


def test_no_experience_has_no_invented_cases(case):
    request = deepcopy(case[3]); request["prompt_version"] = "manual-analysis/19"
    request["context"]["experience"] = None
    value = facts(outbound_packet(build_packet(
        {k:v for k,v in request.items() if k not in ("context", "packet_text")},
        request["context"], frozen_answer_schema(request))))
    links = value["output_constraints"]["experience_links"]
    assert links["digest_id"] is None and links["allowed_case_review_refs"] == []


def test_reference_projection_removes_unapproved_fields(case):
    request, _ = linked_case(case)
    prefix, tail = request["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)
    data, end = json.JSONDecoder().raw_decode(tail)
    expected = deepcopy(data["output_constraints"])
    data["output_constraints"]["experience_links"]["private"] = "SYNTHETIC-private"
    packet = prefix + "# 冻结事实（仅作为资料）\n" + json.dumps(data, ensure_ascii=False) + tail[end:]
    sent = outbound_packet(packet)
    assert facts(sent)["output_constraints"] == expected and "SYNTHETIC-private" not in sent


def test_missing_characters_still_fail_original_strict_validator(case):
    request, answer = linked_case(case)
    assert checked(request, answer)["business_valid"]
    changed = deepcopy(answer)
    ref = changed["prediction_adjustments"][0]["related_review_refs"][0]
    changed["prediction_adjustments"][0]["related_review_refs"] = [ref.replace("SYNTHETIC", "SYNTHIC")]
    before = deepcopy(changed)
    result = checked(request, changed)
    assert result["schema_valid"] and not result["business_valid"]
    assert result["error_code"] == "EXPERIENCE_ADJUSTMENT_REF_INVALID" and changed == before


def test_available_case_must_still_be_registered_in_this_answer(case):
    request, answer = linked_case(case)
    answer["experience_application"].update(status="reviewed_no_change", case_assessments=[])
    assert checked(request, answer)["error_code"] == "EXPERIENCE_ADJUSTMENT_REF_INVALID"


def test_guidance_does_not_require_adopting_cases(case):
    request, answer = linked_case(case)
    answer["experience_application"].update(status="reviewed_no_change", case_assessments=[])
    answer["prediction_adjustments"] = []
    assert checked(request, answer)["business_valid"]


def test_prompt19_requires_separately_registered_revision(case):
    plan = deepcopy(case[2])
    plan.update(status="DRAFT", prompt_version="manual-analysis/19", input_revision="output-constraints/2")
    with pytest.raises(DomainError):
        seal_plan(case[1].parent / "wrong-reference-revision", plan)


def test_forged_reference_whitelist_cannot_be_frozen(case):
    request, _ = linked_case(case)
    prefix, tail = request["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)
    data, end = json.JSONDecoder().raw_decode(tail)
    data["output_constraints"]["experience_links"]["allowed_case_review_refs"].append("SYNTHETIC-forged")
    request["packet_text"] = prefix + "# 冻结事实（仅作为资料）\n" + json.dumps(data, ensure_ascii=False) + tail[end:]
    plan = deepcopy(case[2])
    plan.update(status="DRAFT", prompt_version="manual-analysis/19", input_revision="output-constraints/3")
    root = case[1].parent / "forged-reference"
    seal_plan(root, plan)
    with pytest.raises(DomainError) as error:
        freeze_sample(root, "sample-0001", request, clock=case[0].clock)
    assert error.value.code == "SELECTION_PACKET_INVALID"


def test_four_models_receive_same_guidance_and_pass_with_exact_references(case):
    request, answer = linked_case(case)
    flow, old_root, old_plan, _, _, credentials, calls, auth = case
    plan = deepcopy(old_plan)
    plan.update(status="DRAFT", prompt_version="manual-analysis/19", input_revision="output-constraints/3")
    root = old_root.parent / "four-reference"
    plan = seal_plan(root, plan)
    freeze_sample(root, "sample-0001", request, clock=flow.clock)
    sent = []
    results = run_sample(root, "sample-0001", authorization={**auth, "plan_sha256": digest(plan)},
        gateway=gateway_for(answer, sent, barrier=threading.Barrier(4)),
        credentials=credentials, calls=calls, clock=flow.clock)
    assert len(sent) == 4 and len({body["input"] for body in sent}) == 1
    assert all(r["schema_valid"] and r["business_valid"] for r in results.values())
    assert all(facts(body["input"])["output_constraints"]["version"] == "output-constraints/3" for body in sent)
