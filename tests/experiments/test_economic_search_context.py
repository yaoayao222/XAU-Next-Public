"""As-of facts, same frozen input and search evidence. All model HTTP is SYNTHETIC."""
import json
import threading
from copy import deepcopy
from datetime import timedelta

import httpx
import pytest

from tests.experiments.test_model_selection import case as case, forbid_real_http as forbid_real_http, gateway_for
from xau_next.domains.analysis.public import build_economic_context, build_packet, frozen_answer_schema, outbound_packet
from xau_next.platform.economic_calendar.public import calendar_document, fetch_calendar
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, score_sample, validate_plan


def context_for(request):
    cutoff = parse_utc(request["market_cutoff"])
    z = lambda d: d.isoformat().replace("+00:00", "Z")
    return {"version": "economic-context/1", "data_kind": "SYNTHETIC",
        "private": "PRIVATE_NOT_OUTBOUND", "sources": [{"source_id": "calendar-test", "publisher": "SYNTHETIC",
            "url": "https://example.com/calendar", "captured_at": z(cutoff-timedelta(seconds=5)),
            "coverage_start": z(cutoff-timedelta(days=1)), "coverage_end": z(cutoff+timedelta(days=1)),
            "content_sha256": "a"*64, "private": "PRIVATE_NOT_OUTBOUND"}],
        "events": [{"event_id": "event-test", "title": "SYNTHETIC sentiment", "currency": "USD",
            "scheduled_at": z(cutoff+timedelta(minutes=24)), "importance": "High",
            "importance_scale": "SYNTHETIC High/Medium/Low", "source_id": "calendar-test",
            "forecast": "46.3", "previous": "45.5", "actual": None, "actual_available_at": None,
            "private": "PRIVATE_NOT_OUTBOUND"}]}


def with_context(request, value=None, mode="shared_calendar"):
    request = deepcopy(request)
    request["prompt_version"] = "manual-analysis/20"
    request["context"]["economic_context"] = value or context_for(request)
    request["context"]["research_policy"] = {"mode": mode}
    request["packet_text"] = build_packet({k:v for k,v in request.items() if k not in {"context", "packet_text"}},
        request["context"], frozen_answer_schema(request))
    return request


def new_plan(case, mode):
    flow, old_root, old_plan, request, *_ = case
    request = with_context(request, mode=mode)
    plan = deepcopy(old_plan)
    plan.update(status="DRAFT", purpose="three_model_selection", prompt_version="manual-analysis/20",
        input_revision="economic-context/1", external_research={"mode":mode, "context_path":"unused-synthetic.json",
            "context_sha256":digest(request["context"]["economic_context"])})
    plan["registered_samples"][0]["arms"] = list("ABD")
    root=old_root.parent/("economic-"+mode)
    plan=seal_plan(root,plan)
    freeze_sample(root,"sample-0001",request,clock=flow.clock)
    return root,plan,request


def test_shared_calendar_has_one_input_correct_horizons_and_dynamic_limitations(case):
    flow, _, _, request, answer, credentials, calls, auth = case
    old_packet=request["packet_text"]
    root,plan,q=new_plan(case,"shared_calendar")
    facts=json.JSONDecoder().raw_decode(outbound_packet(q["packet_text"]).split("# 冻结事实（仅作为资料）\n")[1])[0]
    econ=facts["economic_context"]
    assert econ["components"] == {"calendar":"available","news":"unavailable","usd":"unavailable","rates":"unavailable"}
    assert [h["event_ids"] for h in econ["horizons"]] == [["event-test"]]*3
    assert econ["events"][0]["actual"] is None
    assert "PRIVATE_NOT_OUTBOUND" not in outbound_packet(q["packet_text"])
    assert "未接入外部经济事件、美元或利率数据" not in q["packet_text"]
    assert "未接入独立新闻全文" in q["packet_text"]
    assert request["packet_text"]==old_packet and request["prompt_version"]=="manual-analysis/14"
    sent=[]
    results=run_sample(root,"sample-0001",authorization={**auth,"plan_sha256":digest(plan),"max_attempts":3},
        gateway=gateway_for(answer,sent,barrier=threading.Barrier(3)),credentials=credentials,calls=calls,clock=flow.clock)
    assert all(r["state"]=="succeeded" for r in results.values()),results
    assert len(sent)==3 and len({b["input"] for b in sent})==1
    assert all("tools" not in b and b["text"]["format"]["strict"] for b in sent)


@pytest.mark.parametrize("mutation",["future_capture","future_actual","unreleased_actual","actual_after_capture","duplicate_id","bad_url"])
def test_reject_future_information_and_invalid_source(case,mutation):
    q=case[3]; v=context_for(q)
    cutoff=parse_utc(q["market_cutoff"])
    z=lambda d:d.isoformat().replace("+00:00","Z")
    if mutation=="future_capture":v["sources"][0]["captured_at"]=z(cutoff+timedelta(seconds=1))
    if mutation=="future_actual":v["events"][0].update(actual="48.0",actual_available_at=z(cutoff+timedelta(minutes=25)))
    if mutation=="unreleased_actual":v["events"][0].update(actual="48.0",actual_available_at=z(cutoff))
    if mutation=="actual_after_capture":v["events"][0].update(scheduled_at=z(cutoff-timedelta(minutes=10)),
        actual="48.0",actual_available_at=z(cutoff-timedelta(seconds=1)))
    if mutation=="duplicate_id":v["events"].append(deepcopy(v["events"][0]))
    if mutation=="bad_url":v["sources"][0]["url"]="https://name:secret@example.com/calendar"
    with pytest.raises(DomainError):with_context(q,v)


def test_stale_calendar_is_partial_and_never_asserts_no_events(case):
    q=case[3];v=context_for(q)
    v["sources"][0]["captured_at"]=(parse_utc(q["market_cutoff"])-timedelta(hours=2)).isoformat().replace("+00:00","Z")
    derived=build_economic_context(q,{"economic_context":v,"research_policy":{"mode":"shared_calendar"}})
    assert derived["state"]=="partial" and derived["events"]==[]
    assert any("不证明没有事件" in s for s in derived["limitations"])


def test_horizon_mapping_and_known_past_release_are_distinct(case):
    q=case[3];v=context_for(q);cutoff=parse_utc(q["market_cutoff"])
    z=lambda d:d.isoformat().replace("+00:00","Z")
    v["events"][0]["scheduled_at"]=z(cutoff+timedelta(minutes=45))
    past=deepcopy(v["events"][0]);past.update(event_id="released",scheduled_at=z(cutoff-timedelta(minutes=10)),
        actual="48.0",actual_available_at=z(cutoff-timedelta(minutes=9)))
    v["events"].append(past)
    d=build_economic_context(q,{"economic_context":v,"research_policy":{"mode":"shared_calendar"}})
    assert [h["event_ids"] for h in d["horizons"]]==[[],["event-test"],["event-test"]]
    assert next(e for e in d["events"] if e["event_id"]=="released")["actual"]=="48.0"


@pytest.mark.parametrize("good_search",[True,False])
def test_native_search_requires_actual_tool_evidence_and_is_not_accuracy_comparison(case,good_search):
    flow,_,_,_,answer,credentials,calls,auth=case
    root,plan,_=new_plan(case,"native_search_probe")
    def change(payload,body):
        assert body["tools"]==[{"type":"web_search"}] and body["tool_choice"]=="required"
        assert body["text"]["format"]["strict"] and body["reasoning"]=={"effort":"high"}
        assert "仅依据冻结事实给人工交易分析" not in body["input"]
        assert "必须调用已提供的web_search" in body["input"]
        if good_search:
            payload["output"].append({"type":"web_search_call","id":"ws-"+body["model"],"status":"completed",
                "action":{"type":"search","sources":[{"url":"https://example.com/release","title":"SYNTHETIC release"}]}})
        return payload
    sent=[]
    results=run_sample(root,"sample-0001",authorization={**auth,"plan_sha256":digest(plan),"max_attempts":3},
        gateway=gateway_for(answer,sent,change=change,barrier=threading.Barrier(3)),credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==3
    assert all((r["state"]=="succeeded") == good_search for r in results.values()),results
    if not good_search:
        assert all(r["error_code"]=="MODEL_SEARCH_NOT_OBSERVED" for r in results.values())
    assert all((root/"sample-0001"/(a+"-search.json")).exists() for a in "ABD")
    with pytest.raises(DomainError,match="独立搜索"):
        score_sample(root,"sample-0001",{},as_of=flow.clock())


def test_changed_calendar_or_implicit_old_prompt_cannot_bypass_plan(case):
    flow,_,_,_,_,_,_,_=case
    root,plan,q=new_plan(case,"shared_calendar")
    q["context"]["economic_context"]["events"][0]["forecast"]="99.9"
    with pytest.raises(DomainError,match="封存计划"):freeze_sample(root,"sample-0001",q,clock=flow.clock)
    plan["prompt_version"]="manual-analysis/19";plan["input_revision"]="output-constraints/3"
    with pytest.raises(DomainError):validate_plan(plan)


def test_public_calendar_one_read_no_actual_invention_and_timezone():
    raw=json.dumps([{"title":"SYNTHETIC Michigan","country":"USD","date":"2026-10-23T10:00:00-04:00",
        "impact":"High","forecast":"46.3","previous":"45.5"}]).encode()
    seen=[]
    def get(req):seen.append(req);return httpx.Response(200,content=raw)
    context,original=fetch_calendar(transport=httpx.MockTransport(get),clock=lambda:"2026-10-23T13:30:00Z")
    assert len(seen)==1 and seen[0].method=="GET" and "authorization" not in seen[0].headers
    assert original==raw and context["events"][0]["scheduled_at"]=="2026-10-23T14:00:00Z"
    assert context["events"][0]["actual"] is None and context["events"][0]["importance"]=="High"
    assert context["sources"][0]["coverage_start"]=="2026-10-18T04:00:00Z"
    assert calendar_document(raw,captured_at="2026-10-23T13:30:00Z",data_kind="SYNTHETIC")["data_kind"]=="SYNTHETIC"


def test_calendar_network_failure_is_one_attempt_only():
    seen=[]
    def get(req):seen.append(req);return httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):fetch_calendar(transport=httpx.MockTransport(get))
    assert len(seen)==1


def test_calendar_cli_preserves_source_and_does_not_overwrite(tmp_path,monkeypatch,capsys):
    from tooling.experiments.model_selection import main
    import xau_next.platform.economic_calendar.public as calendar
    raw=b'[{"title":"SYNTHETIC","country":"USD","date":"2026-10-23T10:00:00-04:00","impact":"High"}]'
    context=calendar_document(raw,captured_at="2026-10-23T13:30:00Z",data_kind="SYNTHETIC")
    calls=[]
    def fetch():calls.append(1);return context,raw
    monkeypatch.setattr(calendar,"fetch_calendar",fetch)
    root=tmp_path/"calendar"
    assert main(["calendar","--root",str(root)])==0
    assert json.loads(capsys.readouterr().out)["new_model_calls"]==0
    assert (root/"calendar-source.raw.json").read_bytes()==raw
    assert main(["calendar","--root",str(root)])==2
    assert json.loads(capsys.readouterr().out)["error_code"]=="CALENDAR_CAPTURE_EXISTS"
    assert len(calls)==1
