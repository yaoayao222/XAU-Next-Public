"""A declared two-DeepSeek engineering group is distinct from four-way selection."""

import threading
from copy import deepcopy

import pytest
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan

from tests.experiments.test_model_selection import case as case  # noqa: PLC0414 - pytest fixture re-export
from tests.experiments.test_model_selection import gateway_for


@pytest.mark.parametrize("arms", [list("CD"), ["C"], ["D"]])
def test_explicit_deepseek_only_group_uses_two_claims_and_keeps_original(case, arms):
    flow, old_root, old_plan, request, answer, credentials, calls, authorization = case
    original_plan_bytes = (old_root / "plan.json").read_bytes()
    plan = deepcopy(old_plan)
    plan.update(status="DRAFT", purpose="deepseek_compatibility")
    plan["registered_samples"][0]["arms"] = arms
    root = old_root.parent / "compatibility"
    plan = seal_plan(root, plan)
    freeze_sample(root, "sample-0001", request, clock=flow.clock)
    authorization = {**authorization, "plan_sha256": digest(plan), "max_attempts": 2, "sample_ids": ["sample-0001"]}
    sent = []
    results = run_sample(root, "sample-0001", authorization=authorization,
                         gateway=gateway_for(answer, sent, barrier=threading.Barrier(len(arms))),
                         credentials=credentials, calls=calls, clock=flow.clock)
    assert set(results) == set(arms)
    assert all(r["state"] == "succeeded" for r in results.values())
    assert {body["model"] for body in sent} == {plan["arms"][arm]["configuration"]["model_id"] for arm in arms}
    assert (old_root / "plan.json").read_bytes() == original_plan_bytes


def test_two_model_group_without_explicit_engineering_purpose_is_rejected(case):
    plan = deepcopy(case[2])
    plan["registered_samples"][0]["arms"] = list("CD")
    with pytest.raises(DomainError):
        validate_plan(plan)


@pytest.mark.parametrize("arms,stage", [(list("AB"), 1), (list("CDD"), 1), (list("CD"), 2)])
def test_engineering_group_cannot_be_cpa_or_followup_selection(case, arms, stage):
    plan = deepcopy(case[2])
    plan["purpose"] = "deepseek_compatibility"
    plan["registered_samples"][0].update(arms=arms, stage=stage)
    with pytest.raises(DomainError):
        validate_plan(plan)
