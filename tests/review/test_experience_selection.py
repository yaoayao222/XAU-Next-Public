"""SYNTHETIC retrieval checks; no model/terminal calls or profitability claims."""
from copy import deepcopy

from xau_next.domains.review.experience_selection import select_cases
from xau_next.shared.experience_similarity import market_signature
from tests.review.test_experience_statistics import row
from tests.review.test_review import at, bar

CUTOFF = at(600)


def signature(shape="directional", direction="up", state="available", scope="same"):
    return {"version":"closed-m1-retrieval/1", "scope_key":scope, "state":state,
            "cutoff":at(0), "window_minutes":10, "price_basis":"bid", "shape":shape,
            "direction":direction, "range_bps":"10.000000", "efficiency":"0.800000"}


def candidate(id, h=30, value="1", *, state="available", available=at(60), forecast=None,
              scope="same", shape="directional", method="prediction_review/3"):
    value_row = row(id,value,state,method=method,horizon=h)
    value_row.update(review_ref=id+"-r00000001", available_at=available, window_start=at(0),
                     window_end=at(h), evaluated_start=at(0), evaluated_end=at(h),reasons=[])
    metadata = {"available_at":at(1), "market_cutoff":at(0), "forecast_id":forecast or id,
                "market_signature":signature(shape=shape,scope=scope),
                "answer":{"summary":"SYNTHETIC", "horizons":[]}}
    return value_row, metadata


def run(values, target=None):
    cases, metadata = zip(*values) if values else ([],[])
    predictions = {c["inputs"]["forecast_version_id"]:m for c,m in zip(cases,metadata)}
    target = target or {"market_signature":signature(),"horizon_minutes":[30,60,180,360,720],"review_method":"prediction_review/3"}
    return select_cases(list(cases),predictions,target,CUTOFF)


def test_requested_horizons_and_outcomes_use_diverse_forecasts_not_only_recent_winners():
    values = [candidate("wrong",30,"0"),candidate("right",60),candidate("missing",180,None,state="unknown"),
              candidate("uncertain",360,None,state="not_applicable"),candidate("long",720)]
    # Current cutoff must follow every due review window.
    target={"market_signature":signature(),"horizon_minutes":[30,60,180,360],"review_method":"prediction_review/3"}
    values += [candidate("repeat"+str(i),30,forecast="wrong") for i in range(20)]
    cases, summary, _ = run(values,target)
    assert len(cases)==8 and summary["covered_horizons"]==[30,60,180,360]
    assert set(summary["outcome_counts"])=={"correct","incorrect","unknown","not_predicted"}
    assert {"wrong","right","missing","uncertain"} <= {c["target_id"].removeprefix("forecast-") for c in cases}
    assert summary["unique_forecasts"]==4 and summary["reused_forecast_count"]==4


def test_old_relevant_case_outside_recent_eighty_can_be_selected():
    values=[candidate("recent"+str(i),360,available=at(500+i)) for i in range(85)]
    values.append(candidate("old-short-error",30,"0",available=at(61)))
    cases, summary, eligible=run(values)
    assert len(eligible)==86 and any(c["review_ref"].startswith("old-short-error-") for c in cases)
    assert 30 in summary["covered_horizons"]


def test_shape_match_beats_recency_when_horizon_method_and_diversity_are_equal():
    a=candidate("older-match",30,available=at(61))
    b=candidate("newer-mismatch",30,available=at(90),shape="two_way")
    cases,_,_=run([b,a])
    assert cases[0]["review_ref"].startswith("older-match-")
    assert cases[0]["selection"]["market_comparison"]=="comparable"


def test_source_future_version_and_future_window_are_excluded_without_fallback():
    values=[candidate("valid"),candidate("other-source",scope="other"),candidate("future",available=at(601)),
            candidate("window-not-ended",720)]
    version_future=candidate("version-future");version_future[1]["available_at"]=at(601);values.append(version_future)
    cases,summary,_=run(values)
    assert len(cases)==1 and cases[0]["review_ref"].startswith("valid-")
    assert summary["excluded"]=={"future_or_invalid_time":3,"other_source":1}


def test_missing_current_market_is_explicit_and_does_not_invent_similarity():
    target={"market_signature":signature(state="unknown",shape=None,direction=None),"horizon_minutes":[30],"review_method":"prediction_review/3"}
    cases,summary,_=run([candidate("old-method",method="prediction_review/2")],target)
    assert summary["target_signature"]["state"]=="unknown"
    assert cases[0]["selection"]["market_comparison"]=="unknown" and not cases[0]["selection"]["method_match"]
    target["market_signature"]["scope_key"]=None
    assert run([candidate("cannot-prove-source")],target)[0]==[]


def test_order_independent_bounded_retrieval_does_not_mutate_cases():
    values=[candidate(str(i),30 if i%2 else 60,"1" if i%3 else "0") for i in range(20)]
    original=deepcopy(values)
    a=run(values);b=run(list(reversed(values)))
    assert a[0]==b[0] and a[1]==b[1] and values==original
    assert len({c["review_ref"] for c in a[0]})==8
    assert run([])[0]==[]


def test_signature_uses_only_ten_closed_minutes_with_matching_source():
    market={"account_scope_id":"synthetic-scope","symbol":"XAUUSD","source_kind":"synthetic",
            "market_cutoff":at(10),"series":[{"timeframe":"M1","price_basis":"bid",
                "status":{"state":"available","captured_at":at(10),"observed_at":at(10),"reasons":[]},
                "bars":[bar(i,open=str(100+i),close=str(101+i),low=str(100+i),high=str(101+i)) for i in range(10)]}]}
    current=market_signature(market)
    assert current["state"]=="available" and current["shape"]=="directional" and current["direction"]=="up"
    newer=deepcopy(market);newer["series"][0]["bars"].append(bar(11,close="1",low="1",high="1000"))
    assert market_signature(newer)==current
    missing=deepcopy(market);missing["series"][0]["bars"].pop(5)
    assert market_signature(missing)["state"]=="unknown"
    changed=deepcopy(market);changed["account_scope_id"]="different"
    assert market_signature(changed)["scope_key"]!=current["scope_key"]
