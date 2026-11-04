"""SYNTHETIC HTTP only; exact identities, research cutoff and four-call budget."""
import json
import threading
from copy import deepcopy

import httpx
import jsonschema
import pytest

from tests.experiments.test_model_selection import case as case, forbid_real_http as forbid_real_http, gateway_for
from tests.experiments.test_economic_search_context import context_for
from xau_next.domains.analysis.public import build_packet, bind_shared_research_refs, constrain_case_refs, frozen_answer_schema, outbound_packet
from xau_next.domains.analysis.shared_search import project_research
from xau_next.platform.economic_calendar.search import fetch_search
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan


def research(cutoff):
    return {"version":"shared-search/1","queries":[{"component":c,"query":"SYNTHETIC "+c,"captured_at":cutoff,"content_sha256":"a"*64,
        "private":"PRIVATE_NOT_OUTBOUND","results":[{"source_id":c+"-1","title":"SYNTHETIC source","url":"https://example.com/"+c,
            "published_at":None,"snippet":"SYNTHETIC retrieved facts","content":"SYNTHETIC source content","private":"PRIVATE_NOT_OUTBOUND"}]} for c in ("news","usd","rates")]}


def setup_case(case):
    flow,root,old,q,*_=case
    q=deepcopy(q);q["prompt_version"]="manual-analysis/21"
    value=context_for(q);value["shared_research"]=research(q["market_cutoff"])
    q["context"].update(economic_context=value,research_policy={"mode":"shared_search"})
    q["context"]=bind_shared_research_refs(q,q["context"])
    q["packet_text"]=build_packet({k:v for k,v in q.items() if k not in {"context","packet_text"}},q["context"],frozen_answer_schema(q))
    p=deepcopy(old);p.update(status="DRAFT",purpose="model_selection",prompt_version="manual-analysis/21",input_revision="shared-search/1",total_call_limit=4,
        external_research={"mode":"shared_search","context_path":"SYNTHETIC","context_sha256":digest(value)},
        shared_budget_extension={"prior_shared_attempts":54,"additional_attempts":4,"cumulative_limit":58,"human_authorization_sha256":"b"*64})
    auth={**case[-1],"max_attempts":4,"sample_ids":["sample-0001"],"human_authorization_sha256":"b"*64,"shared_budget_extension_sha256":digest(p["shared_budget_extension"])}
    return q,p,auth


def test_four_shared_search_preserves54_reaches58_and_sends_identical_facts_without_native_tools(case):
    flow,oldroot,_,_,answer,credentials,calls,_=case
    q,p,auth=setup_case(case);root=oldroot.parent/"shared-four"
    p=seal_plan(root,p);auth["plan_sha256"]=digest(p)
    freeze_sample(root,"sample-0001",q,clock=flow.clock)
    with calls.db.write() as tx:
        calls.docs.insert(tx,"experience_experiment_budget",p["experiment_id"],{"count":54,"reserved_cost":"0","cost_unknown":True})
    sent=[];r=run_sample(root,"sample-0001",authorization=auth,gateway=gateway_for(answer,sent,barrier=threading.Barrier(4)),credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==4 and all(x["state"]=="succeeded" for x in r.values()),r
    assert len({b["input"] for b in sent})==1
    assert all("tools" not in b and "include" not in b for b in sent)
    assert all(b["text"]["format"]["strict"] for b in sent)
    assert all("PRIVATE_NOT_OUTBOUND" not in b["input"] for b in sent)
    assert "shared_research" in sent[0]["input"] and "search_results_available" in sent[0]["input"]
    with calls.db.read() as tx:
        assert calls.docs.get(tx,"experience_experiment_budget",p["experiment_id"])["count"]==58
    with pytest.raises((DomainError,FileExistsError)):
        run_sample(root,"sample-0001",authorization=auth,gateway=gateway_for(answer,sent),credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==4


def test_case_ids_are_exact_enums_and_do_not_mutate_authoritative_schema(case):
    q=deepcopy(case[3]);q["prompt_version"]="manual-analysis/21"
    q["context"]["experience"]={"cases":[{"review_ref":"rv-d582a87e41e7f9e254ecb6c417ec5a6a-r00000001"}]}
    schema=frozen_answer_schema(q);before=deepcopy(schema);narrow=constrain_case_refs(schema,q)
    node=narrow["$defs"]["ExperienceCaseAssessment"]["properties"]["review_ref"]
    jsonschema.validate("rv-d582a87e41e7f9e254ecb6c417ec5a6a-r00000001",node)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate("rv-d582a87e41e7f9e254ecb6c4175a6a-r00000001",node)
    assert schema==before
    q["prompt_version"]="manual-analysis/20";assert constrain_case_refs(schema,q)==schema


def test_official_page_anchor_is_preserved_without_allowing_credentials(case):
    cutoff=case[3]["market_cutoff"];v=research(cutoff)
    original="https://www.pmi.spglobal.com/Public/Home/PressRelease/example#1#1"
    v["queries"][0]["results"][0]["url"]=original
    assert project_research(v,cutoff)["queries"][0]["results"][0]["url"]==original
    v["queries"][0]["results"][0]["url"]="https://user:secret@example.com/page#1"
    with pytest.raises(DomainError):project_research(v,cutoff)


def test_registered_sources_are_exact_and_fake_source_still_rejected(case):
    q,_,_=setup_case(case)
    assert {'calendar-test','news-1','usd-1','rates-1'} <= set(q['context']['evidence_refs'])
    schema=constrain_case_refs(frozen_answer_schema(q),q)
    node=schema['properties']['evidence_refs']['items']
    jsonschema.validate('news-1',node)
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate('news-NOT-IN-INPUT',node)


def test_shared_source_registration_is_pre_dispatch_and_does_not_rewrite_old_request(case):
    q,p,_=setup_case(case);flow,oldroot,*_=case
    q['context']['evidence_refs'].remove('news-1')
    q['packet_text']=build_packet({k:v for k,v in q.items() if k not in {'context','packet_text'}},q['context'],frozen_answer_schema(q))
    root=oldroot.parent/'missing-research-refs';seal_plan(root,p)
    with pytest.raises(DomainError,match='合法引用集合'):freeze_sample(root,'sample-0001',q,clock=flow.clock)
    original=deepcopy(q['context']);bound=bind_shared_research_refs(q,original)
    assert original==q['context'] and 'news-1' not in original['evidence_refs'] and 'news-1' in bound['evidence_refs']


@pytest.mark.parametrize("mutation",["future_capture","future_publication","wrong_component","missing_results","bad_url"])
def test_shared_sources_are_not_silently_promoted(case,mutation):
    cutoff=case[3]["market_cutoff"];v=research(cutoff)
    if mutation=="future_capture":v["queries"][0]["captured_at"]="2099-01-01T00:00:00Z"
    if mutation=="future_publication":v["queries"][0]["results"][0]["published_at"]="2099-01-01T00:00:00Z"
    if mutation=="wrong_component":v["queries"][0]["component"]="rates"
    if mutation=="missing_results":v["queries"][0]["results"]=[]
    if mutation=="bad_url":v["queries"][0]["results"][0]["url"]="https://user:password@example.com"
    with pytest.raises(DomainError):project_research(v,cutoff)


@pytest.mark.parametrize("mutation",["wrong_sum","three_calls","native_search","missing_human_hash"])
def test_shared_four_extension_cannot_expand_or_change_route(case,mutation):
    _,p,_=setup_case(case)
    if mutation=="wrong_sum":p["shared_budget_extension"]["cumulative_limit"]=59
    if mutation=="three_calls":p["shared_budget_extension"]["additional_attempts"]=3
    if mutation=="native_search":p["external_research"]["mode"]="native_search_probe"
    if mutation=="missing_human_hash":p["shared_budget_extension"]["human_authorization_sha256"]=""
    with pytest.raises(DomainError):validate_plan(p)


@pytest.mark.parametrize("status",[200,500])
def test_official_search_is_bounded_and_never_retries(status):
    seen=[]
    def http(request):
        seen.append(request)
        assert request.url=="https://api.deepseek.com/v1/tools/search"
        assert json.loads(request.content)=={"query":"SYNTHETIC","count":3,"need_content":True}
        return httpx.Response(status,json={"results":[{"title":"SYNTHETIC","url":"https://example.com","summary":"text","date":"2026-10-23T10:00:00+00:00"}]})
    kwargs={"transport":httpx.MockTransport(http),"clock":lambda:"2026-10-23T11:00:00Z"}
    if status==500:
        with pytest.raises(httpx.HTTPStatusError):fetch_search("SYNTHETIC_KEY","SYNTHETIC","news",**kwargs)
    else:
        value,raw=fetch_search("SYNTHETIC_KEY","SYNTHETIC","news",**kwargs)
        assert value["results"][0]["published_at"]=="2026-10-23T10:00:00Z" and b'SYNTHETIC_KEY' not in raw
    assert len(seen)==1
