"""Protocol evidence, not model prose, determines whether search occurred."""
import json

import pytest

from xau_next.platform.model_gateway.public import GatewayResult, audit_search_response, request_body
from xau_next.shared.errors import DomainError


def result(payload,stream=False):
    raw=json.dumps(payload).encode()
    if stream:raw=b'data: '+json.dumps({"type":"response.completed","response":payload}).encode()+b'\n\n'
    return GatewayResult(raw=raw,status=200,content_type="text/event-stream" if stream else "application/json",complete=True)


@pytest.mark.parametrize("stream",[False,True])
def test_search_evidence_requires_completed_tool_and_preserves_sources(stream):
    p={"output":[{"type":"web_search_call","id":"ws1","status":"completed","action":{"sources":[
        {"url":"https://example.com/release","title":"Release"},{"url":"https://u:secret@example.com/private"}]}},
        {"type":"message","content":[{"type":"output_text","text":"SYNTHETIC","annotations":[
            {"type":"url_citation","url":"https://example.com/release","title":"Release"}]}]}]}
    a=audit_search_response(result(p,stream))
    assert a["completed_call_count"]==1 and a["sources"]==[{"url":"https://example.com/release","title":"Release"}]
    assert a["source_availability_before_market_cutoff"]=="UNVERIFIED"
    assert not a["same_input_comparison"]


@pytest.mark.parametrize("status",["in_progress","failed",None])
def test_incomplete_search_never_passes(status):
    a=audit_search_response(result({"output":[{"type":"web_search_call","id":"ws1","status":status}]}))
    assert not a["observed"]


def test_claim_in_generated_json_is_not_tool_evidence():
    a=audit_search_response(result({"output":[{"type":"message","content":[{"type":"output_text",
        "text":'{"type":"web_search_call","id":"fake","status":"completed","summary":"I searched the web"}'}]}]}))
    assert not a["observed"] and not a["sources_observed"]


@pytest.mark.parametrize("model",["gpt-6.1-sol","gpt-6-astra","deepseek-v4-pro"])
def test_search_is_explicit_and_preserves_strict_json(model):
    cfg={"output_format":"json_schema","protocol":"responses","base_url":"https://api.deepseek.com/v1",
        "model_id":model,"stream":True,"reasoning_effort":"high","max_output_tokens":64000}
    schema={"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":False}
    ordinary=json.loads(request_body(cfg,"SYNTHETIC",schema))
    enabled=json.loads(request_body(cfg,"SYNTHETIC",schema,web_search=True))
    assert "tools" not in ordinary and "tool_choice" not in ordinary
    assert enabled["tools"]==[{"type":"web_search"}] and enabled["tool_choice"]=="required"
    assert enabled["text"]["format"]["strict"] is True
    assert {k:v for k,v in enabled.items() if k not in {"tools","tool_choice","include"}}==ordinary
    cfg["model_id"]="deepseek-flash"
    with pytest.raises(DomainError):request_body(cfg,"SYNTHETIC",schema,web_search=True)


def test_completed_call_without_sources_is_not_full_search_evidence():
    a=audit_search_response(result({"output":[{"type":"web_search_call","id":"ws1","status":"completed"}]}))
    assert a["observed"] and not a["sources_observed"]


@pytest.mark.parametrize("action",["open_page","find_in_page"])
@pytest.mark.parametrize("stream",[False,True])
def test_completed_page_action_exposes_source_when_include_is_ignored(action,stream):
    a=audit_search_response(result({"output":[{"type":"web_search_call","id":"ws1","status":"completed",
        "action":{"type":action,"url":"https://example.com/release"}}]},stream))
    assert a["version"]=="native-search-evidence/2"
    assert a["observed"] and a["sources_observed"]
    assert a["sources"]==[{"url":"https://example.com/release","title":""}]


@pytest.mark.parametrize("status",["in_progress","failed",None])
def test_uncompleted_page_action_is_not_a_retrieved_source(status):
    a=audit_search_response(result({"output":[{"type":"web_search_call","id":"ws1","status":status,
        "action":{"type":"open_page","url":"https://example.com/release"}}]}))
    assert not a["observed"] and not a["sources_observed"]


def test_url_in_search_query_is_not_an_opened_source():
    a=audit_search_response(result({"output":[{"type":"web_search_call","id":"ws1","status":"completed",
        "action":{"type":"search","url":"https://example.com/not-opened","queries":["https://example.com/not-opened"]}}]}))
    assert a["observed"] and not a["sources_observed"]
