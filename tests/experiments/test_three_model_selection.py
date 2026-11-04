"""Explicit three-arm predictive comparison; synthetic HTTP only."""
import threading
from copy import deepcopy

import pytest

from tests.experiments.test_model_selection import case as case
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.experiments.test_model_selection import gateway_for
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan


def test_three_models_use_identical_input_only_three_claims_and_no_flash(case):
    flow, old_root, old_plan, request, answer, credentials, calls, authorization = case
    original = (old_root/'plan.json').read_bytes()
    plan = deepcopy(old_plan)
    plan.update(status='DRAFT', purpose='three_model_selection')
    plan['registered_samples'][0]['arms'] = list('ABD')
    root = old_root.parent/'three-models'
    plan = seal_plan(root, plan)
    freeze_sample(root, 'sample-0001', request, clock=flow.clock)
    assert not (root/'sample-0001/C-body.json').exists()
    auth = {**authorization, 'plan_sha256':digest(plan), 'max_attempts':3, 'sample_ids':['sample-0001']}
    sent = []
    results = run_sample(root, 'sample-0001', authorization=auth,
        gateway=gateway_for(answer,sent,barrier=threading.Barrier(3)),
        credentials=credentials,calls=calls,clock=flow.clock)
    assert set(results)==set('ABD') and len(sent)==3
    assert all(r['state']=='succeeded' for r in results.values())
    assert {b['model'] for b in sent} == {'gpt-6.1-sol','gpt-6-astra','deepseek-v4-pro'}
    assert (old_root/'plan.json').read_bytes()==original
    with pytest.raises((DomainError,FileExistsError)):
        run_sample(root,'sample-0001',authorization=auth,gateway=gateway_for(answer,sent),
            credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==3


@pytest.mark.parametrize('arms,stage', [(list('ABCD'),1),(list('ABC'),1),(list('ADD'),1),
    (list('AB'),1),(list('BD'),1),(list('AD'),2),(list('ABD'),2)])
def test_three_model_purpose_cannot_change_models_add_flash_or_followup(case,arms,stage):
    plan=deepcopy(case[2]);plan['purpose']='three_model_selection'
    plan['registered_samples'][0].update(arms=arms,stage=stage)
    with pytest.raises(DomainError):validate_plan(plan)


def test_default_four_arm_plan_cannot_silently_shrink_to_three(case):
    plan=deepcopy(case[2]);plan['registered_samples'][0]['arms']=list('ABD')
    with pytest.raises(DomainError):validate_plan(plan)
