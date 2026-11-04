"""SYNTHETIC regression cases for observed failures; no live model/search/history."""
import json
import threading
from copy import deepcopy
from datetime import timedelta

import jsonschema
import pytest

from tests.experiments.test_model_selection import case as case, forbid_real_http as forbid_real_http, gateway_for
from tests.experiments.test_shared_search_four import setup_case
from xau_next.domains.analysis.forecast_review_input import build_review_input
from xau_next.domains.analysis.public import build_economic_context, build_packet, constrain_case_refs, frozen_answer_schema, outbound_packet
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from xau_next.workflows.model_selection import digest, freeze_sample, run_sample, seal_plan, validate_plan


def candidate(case):
    q, p, auth = setup_case(case)
    q["prompt_version"] = "manual-analysis/22"
    q["packet_text"] = build_packet({k:v for k,v in q.items() if k not in {"context", "packet_text"}}, q["context"], frozen_answer_schema(q))
    p.update(prompt_version="manual-analysis/22", input_revision="shared-search/2")
    p["shared_budget_extension"].update(prior_shared_attempts=58, cumulative_limit=62)
    auth["shared_budget_extension_sha256"] = digest(p["shared_budget_extension"])
    return q, p, auth


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def test_same_four_inputs_keep_schema_budgets_and_no_live_tools(case):
    flow, oldroot, _, _, answer, credentials, calls, _ = case
    q, p, auth = candidate(case)
    root = oldroot.parent/"fixed-candidate-SYNTHETIC"
    p = seal_plan(root, p); auth["plan_sha256"] = digest(p)
    freeze_sample(root, "sample-0001", q, clock=flow.clock)
    with calls.db.write() as tx:
        calls.docs.insert(tx, "experience_experiment_budget", p["experiment_id"], {"count":58,"reserved_cost":"0","cost_unknown":True})
    sent = []
    results = run_sample(root, "sample-0001", authorization=auth,
        gateway=gateway_for(answer, sent, barrier=threading.Barrier(4)), credentials=credentials, calls=calls, clock=flow.clock)
    assert len(sent) == 4 and all(r["state"] == "succeeded" for r in results.values()), results
    assert len({b["input"] for b in sent}) == 1
    assert all("tools" not in b and "include" not in b and b["text"]["format"]["strict"] for b in sent)
    data = facts(sent[0]["input"])
    assert data["forecast_review_input"] == facts(q["packet_text"])["forecast_review_input"]
    assert "PRIVATE_NOT_OUTBOUND" not in sent[0]["input"]
    with pytest.raises((DomainError, FileExistsError)):
        run_sample(root, "sample-0001", authorization=auth, gateway=gateway_for(answer,sent), credentials=credentials, calls=calls, clock=flow.clock)
    assert len(sent) == 4


def test_elapsed_schedule_is_missing_not_unreleased_and_does_not_invent_actual(case):
    q, _, _ = candidate(case); v = q["context"]["economic_context"]
    at = parse_utc(q["market_cutoff"])
    v["events"][0]["scheduled_at"] = (at-timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    before = deepcopy(q)
    e = build_economic_context(q,q["context"])
    result = build_review_input(q,e,q["context"]["market"])
    assert result["event_value_states"][0]["state"] == "scheduled_time_elapsed_actual_missing"
    assert v["events"][0]["actual"] is None and q == before
    v["events"][0].update(actual="48.0",actual_available_at=(at-timedelta(minutes=9)).isoformat().replace("+00:00", "Z"))
    result = build_review_input(q,build_economic_context(q,q["context"]),q["context"]["market"])
    assert result["event_value_states"][0]["state"] == "actual_provided"


def test_fresh_capture_cannot_promote_old_or_undated_content_to_realtime(case):
    q, _, _ = candidate(case)
    v=q["context"]["economic_context"]; at=parse_utc(q["market_cutoff"])
    v["shared_research"]["queries"][1]["results"][0]["published_at"]=(at-timedelta(days=2)).isoformat().replace("+00:00","Z")
    e=build_economic_context(q,q["context"]); result=build_review_input(q,e,q["context"]["market"])
    rows=result["search_source_quality"]
    assert rows[0]["publication_age_screen"]=="publication_time_unknown"
    assert rows[1]["publication_age_screen"]=="published_more_than_24h_ago"
    assert all(r["observation_time_verified"] is False for r in rows)
    assert sum(len(r["results"]) for r in e["shared_research"]["queries"])==3


def synthetic_market(cutoff):
    at=parse_utc(cutoff); z=lambda d:d.isoformat().replace("+00:00","Z")
    bars=[{"open_at":z(at-timedelta(minutes=3-i)),"close_at":z(at-timedelta(minutes=2-i)),
           "closed":True,"high":"4012.21","low":"4010.00","close":"4011.00"} for i in range(3)]
    return {"quote":{"bid":"4012.27","ask":"4012.37"},"series":[{"timeframe":"M1","price_basis":"bid","bars":bars}]}


def test_decimal_relation_does_not_repeat_above_as_below_error(case):
    q, _, _=candidate(case); e=build_economic_context(q,q["context"])
    m=synthetic_market(q["market_cutoff"]); r=build_review_input(q,e,m)["recent_price_relations"][0]
    assert r["state"]=="available" and r["quote_minus_recent_high"]=="0.06"
    assert r["quote_minus_recent_low"]=="2.27" and r["close_changes"]==["0.00","0.00"]
    m["series"][0]["price_basis"]="ask"
    assert build_review_input(q,e,m)["recent_price_relations"][0]["quote_minus_recent_high"]=="0.16"


@pytest.mark.parametrize("mutation",["gap","not_closed","future","duplicate","stale","incomplete"])
def test_defective_bars_do_not_produce_confident_relations(case,mutation):
    q, _, _=candidate(case); e=build_economic_context(q,q["context"]); m=synthetic_market(q["market_cutoff"])
    bars=m["series"][0]["bars"]
    if mutation=="gap": bars[1]["open_at"]=bars[0]["open_at"]
    if mutation=="not_closed": bars[-1]["closed"]=False
    if mutation=="future": bars[-1]["close_at"]="2099-01-01T00:00:00Z"
    if mutation=="duplicate": bars[1]=deepcopy(bars[0])
    if mutation=="stale":
        for b in bars:
            for k in ("open_at","close_at"):b[k]=(parse_utc(b[k])-timedelta(minutes=4)).isoformat().replace("+00:00","Z")
    if mutation=="incomplete":bars[-1]["open_at"]=bars[0]["open_at"]
    r=build_review_input(q,e,m)["recent_price_relations"][0]
    assert r["state"]=="insufficient_or_stale_closed_bars" and "quote_minus_recent_high" not in r


def test_projection_recomputes_quality_instead_of_sending_injected_fields(case):
    q, _, _=candidate(case); t=q["packet_text"]; data=facts(t)
    data["forecast_review_input"]={"injected":"PRIVATE_FAKE_REALTIME"}
    prefix,rest=t.split("# 冻结事实（仅作为资料）\n",1); _,suffix=rest.split("\n# 全部字段说明\n",1)
    forged=prefix+"# 冻结事实（仅作为资料）\n"+json.dumps(data,ensure_ascii=False)+"\n# 全部字段说明\n"+suffix
    wire=outbound_packet(forged)
    assert "PRIVATE_FAKE_REALTIME" not in wire and facts(wire)["forecast_review_input"]["macro_observation_state"]=="synchronized_usd_rates_series_not_provided"


def test_22_rejects_fake_source_and_wrong_revision_without_mutating_schema(case):
    q,p,_=candidate(case); schema=frozen_answer_schema(q); old=deepcopy(schema)
    narrowed=constrain_case_refs(schema,q)
    jsonschema.validate('news-1',narrowed['properties']['evidence_refs']['items'])
    with pytest.raises(jsonschema.ValidationError):jsonschema.validate('fake-news',narrowed['properties']['evidence_refs']['items'])
    assert schema==old
    p['input_revision']='shared-search/1'
    with pytest.raises(DomainError):validate_plan(p)
