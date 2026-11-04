"""One explicit three-call extension preserves the previously spent shared budget."""
import threading
from copy import deepcopy

import pytest

from tests.experiments.test_model_selection import case as case, forbid_real_http as forbid_real_http, gateway_for
from tests.experiments.test_economic_search_context import with_context
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import authorize, digest, freeze_sample, run_sample, seal_plan, validate_plan


def extension_case(case):
    flow,root,old,request,answer,credentials,calls,auth=case
    q=with_context(request,mode="native_search_probe")
    p=deepcopy(old)
    p.update(status="DRAFT",purpose="three_model_selection",prompt_version="manual-analysis/20",
        input_revision="economic-context/1",total_call_limit=3,
        external_research={"mode":"native_search_probe","context_path":"SYNTHETIC",
            "context_sha256":digest(q["context"]["economic_context"])},
        shared_budget_extension={"prior_shared_attempts":48,"additional_attempts":3,"cumulative_limit":51,
            "human_authorization_sha256":"a"*64})
    p["registered_samples"][0]["arms"]=list("ABD")
    a={**auth,"max_attempts":3,"sample_ids":["sample-0001"],"human_authorization_sha256":"a"*64,
        "shared_budget_extension_sha256":digest(p["shared_budget_extension"])}
    return q,p,a


def test_spent48_then_three_calls_reach51_without_reset_or_fourth_call(case):
    flow,old_root,old,_,answer,credentials,calls,_=case
    q,p,a=extension_case(case)
    root=old_root.parent/"extension"
    p=seal_plan(root,p);a["plan_sha256"]=digest(p)
    freeze_sample(root,"sample-0001",q,clock=flow.clock)
    with calls.db.write() as tx:
        calls.docs.insert(tx,"experience_experiment_budget",p["experiment_id"],
            {"count":48,"reserved_cost":"0","cost_unknown":True})
    def searched(payload,body):
        payload["output"].append({"type":"web_search_call","id":"ws-"+body["model"],"status":"completed",
            "action":{"sources":[{"url":"https://example.com/source","title":"SYNTHETIC"}]}})
        return payload
    sent=[]
    r=run_sample(root,"sample-0001",authorization=a,gateway=gateway_for(answer,sent,change=searched,
        barrier=threading.Barrier(3)),credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==3 and all(v["state"]=="succeeded" for v in r.values()),r
    with calls.db.read() as tx:
        assert calls.docs.get(tx,"experience_experiment_budget",p["experiment_id"])["count"]==51
    with pytest.raises(DomainError):authorize(p,{**a,"max_attempts":4},1,"sample-0001")
    with pytest.raises((DomainError,FileExistsError)):
        run_sample(root,"sample-0001",authorization=a,gateway=gateway_for(answer,sent),
            credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==3


@pytest.mark.parametrize("change",["missing_hash","wrong_human","wrong_group","missing_extension_hash"])
def test_extension_requires_its_own_human_and_sample_authorization(case,change):
    _,p,a=extension_case(case);p["status"]="FROZEN";a["plan_sha256"]=digest(p)
    if change=="missing_hash":a.pop("human_authorization_sha256")
    if change=="wrong_human":a["human_authorization_sha256"]="b"*64
    if change=="wrong_group":a["sample_ids"]=["sample-other"]
    if change=="missing_extension_hash":a.pop("shared_budget_extension_sha256")
    with pytest.raises(DomainError):authorize(p,a,1,"sample-0001")


@pytest.mark.parametrize("change",["four_calls","changed_sum","normal_route","two_groups","wrong_hash"])
def test_extension_cannot_expand_group_or_existing_route(case,change):
    _,p,_=extension_case(case)
    if change=="four_calls":p["shared_budget_extension"]["additional_attempts"]=4
    if change=="changed_sum":p["shared_budget_extension"]["cumulative_limit"]=52
    if change=="normal_route":p.pop("external_research");p["prompt_version"]="manual-analysis/19";p["input_revision"]="output-constraints/3"
    if change=="two_groups":p["registered_samples"].append(deepcopy(p["registered_samples"][0]))
    if change=="wrong_hash":p["shared_budget_extension"]["human_authorization_sha256"]=""
    with pytest.raises(DomainError):validate_plan(p)
