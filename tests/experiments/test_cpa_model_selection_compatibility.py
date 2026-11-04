"""Explicit CPA engineering retest, never a completed four-way group. SYNTHETIC."""
import threading
from copy import deepcopy

import pytest

from tests.experiments.test_model_selection import case as case
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.experiments.test_model_selection import gateway_for
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan


@pytest.mark.parametrize("arms", [["A"], ["B"], ["A", "B"]])
def test_explicit_cpa_retest_sends_only_authorized_models(case, arms):
    flow, old_root, old_plan, request, answer, credentials, calls, authorization = case
    original = (old_root / "plan.json").read_bytes()
    plan = deepcopy(old_plan)
    plan.update(status="DRAFT", purpose="cpa_compatibility")
    plan["registered_samples"][0]["arms"] = arms
    root = old_root.parent / "cpa-retest"
    plan = seal_plan(root, plan)
    freeze_sample(root, "sample-0001", request, clock=flow.clock)
    auth = {**authorization, "plan_sha256": digest(plan), "max_attempts": len(arms), "sample_ids": ["sample-0001"]}
    sent = []
    original_read = credentials.read
    def read_only_cpa(reference):
        assert reference == plan["arms"]["A"]["credential_ref"]
        return original_read(reference)
    credentials.read = read_only_cpa
    results = run_sample(root, "sample-0001", authorization=auth,
                         gateway=gateway_for(answer, sent, barrier=threading.Barrier(len(arms))),
                         credentials=credentials, calls=calls, clock=flow.clock)
    assert set(results) == set(arms) and len(sent) == len(arms)
    assert all(r["state"] == "succeeded" for r in results.values())
    assert {v["model"] for v in sent} == {plan["arms"][a]["configuration"]["model_id"] for a in arms}
    assert (old_root / "plan.json").read_bytes() == original


@pytest.mark.parametrize("arms,stage", [(["C"], 1), (["D"], 1), (["A", "C"], 1), (list("ABCD"), 1), (["A", "A"], 1), (["A", "B"], 2)])
def test_cpa_retest_cannot_add_deepseek_duplicates_or_stage2(case, arms, stage):
    plan = deepcopy(case[2]); plan["purpose"] = "cpa_compatibility"
    plan["registered_samples"][0].update(arms=arms, stage=stage)
    with pytest.raises(DomainError):
        validate_plan(plan)


def test_two_cpa_arms_do_not_silently_become_four_way_selection(case):
    plan = deepcopy(case[2]); plan["registered_samples"][0]["arms"] = list("AB")
    with pytest.raises(DomainError):
        validate_plan(plan)
