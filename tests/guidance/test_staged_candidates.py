"""Deterministic staged invalidation and actual import-window boundaries."""
import copy

import pytest
from xau_next.domains.analysis.validation import validate_answer
from xau_next.domains.plans.lifecycle import advance
from xau_next.shared.entry_timing import entry_timing
from xau_next.shared.errors import DomainError

from tests.guidance.test_candidates import candidate_case


def staged_record():
    return {"plan_id":"p", "version_id":"v", "available_at":"2026-10-13T19:06:38.298Z",
        "lifecycle":"WAITING_TRIGGER", "lifecycle_at":"2026-10-13T19:06:38.298Z",
        "activation_observed_at":None, "manual_observations":[],
        "definition":{"valid_from":"2026-10-13T18:59:21.590Z", "entry_deadline":"2026-10-13T20:29:21.590Z",
            "activation":{"kind":"bar_close","timeframe":"M15","operator":"gt","price":"4173.21"},
            "invalidation":{"kind":"bar_close","timeframe":"M15","operator":"lt","price":"4155.82"},
            "post_activation_invalidation":{"kind":"bar_close","timeframe":"M15","operator":"lt","price":"4164.34"},
            "trading_style":"intraday"}}


def observed(bars):
    return {"quote":None,"series":[{"timeframe":"M15","bars":[{"closed":True,"close_at":at,"close":price} for at,price in bars]}]}


def test_staged_failure_does_not_apply_to_waiting_or_replayed_pretrigger_bars():
    p = staged_record()
    market = observed([("2026-10-13T19:15:00Z","4163.06")])
    assert advance(p, market, "2026-10-13T19:15:01Z")["lifecycle"] == "WAITING_TRIGGER"
    market["series"][0]["bars"].append({"closed":True,"close_at":"2026-10-13T19:30:00Z","close":"4174.00"})
    active = advance(p, market, "2026-10-13T19:30:01Z")
    assert active["lifecycle"] == "ACTIVE"
    assert advance(active, market, "2026-10-13T19:30:02Z")["lifecycle"] == "ACTIVE"
    market["series"][0]["bars"].append({"closed":True,"close_at":"2026-10-13T19:45:00Z","close":"4164.00"})
    ended = advance(active, market, "2026-10-13T19:45:01Z")
    assert ended["lifecycle"] == "INVALIDATED"
    assert ended["invalidation_phase"] == "post_activation"


def test_background_remains_active_before_trigger_and_legacy_scope_is_not_changed():
    p = staged_record()
    before = advance(p, observed([("2026-10-13T19:15:00Z","4155.00")]), "2026-10-13T19:15:01Z")
    assert before["lifecycle"] == "INVALIDATED"
    assert before["invalidation_phase"] == "background"
    legacy = copy.deepcopy(p)
    legacy["definition"]["invalidation"] = legacy["definition"].pop("post_activation_invalidation")
    assert advance(legacy, observed([("2026-10-13T19:15:00Z","4163.06")]), "2026-10-13T19:15:01Z")["lifecycle"] == "INVALIDATED"


def test_actual_import_window_counts_closes_and_does_not_reset_analysis_time():
    plan = {"trading_style":"scalp", "valid_from":"2026-10-13T18:59:21.590Z", "entry_deadline":"2026-10-13T19:19:21.590Z",
        "entry":{"mode":"limit_retest"}, "activation":{"kind":"bar_close","timeframe":"M5"},
        "entry_checks":[{"kind":"bar_close","timeframe":"M15"}]}
    request = {"market_cutoff":"2026-10-13T18:59:21.590Z", "window_end":"2026-10-14T06:59:21.590Z"}
    report = entry_timing(plan, request, "2026-10-13T19:06:38.298Z")
    assert report["remaining_at_import_seconds"] == 763
    assert report["activation_closes_at_import"] == 2
    assert report["minimum_entry_deadline"] == "2026-10-13T19:20:00Z"
    assert report["state"] == "INSUFFICIENT"
    assert report["analysis_fresh_until"] == "2026-10-13T19:29:21.590000Z"


def new_case():
    answer, schema, request, context = candidate_case()
    answer["schema_version"] = request["answer_schema_version"] = "1.3.0"
    p = answer["plans"][0]
    p["activation"] = {"kind":"quote_compare","quote_side":"bid","operator":"gt","price":"101"}
    p["invalidation"] = {"kind":"quote_compare","quote_side":"bid","operator":"lt","price":"98.80"}
    p["post_activation_invalidation"] = {"kind":"quote_compare","quote_side":"bid","operator":"lt","price":"100.50"}
    context["market"]["quote"].update(bid="100", ask="100.02", event_time=request["market_cutoff"])
    context["market"]["quote_status"]["state"] = "available"
    return answer, schema, request, context


def test_new_import_accepts_post_rule_true_before_trigger_without_changing_original():
    values = new_case()
    before = copy.deepcopy(values)
    validate_answer(*values)
    assert values == before


@pytest.mark.parametrize("defect,code", [("background","BACKGROUND_ALREADY_INVALID"),
    ("unknown","BACKGROUND_UNVERIFIED"), ("contradiction","RULE_CONTRADICTION"),
    ("short_window","ENTRY_WINDOW_INSUFFICIENT"), ("past_freshness","ENTRY_WINDOW_INSUFFICIENT")])
def test_new_import_rejects_contradictory_background_and_unusable_planned_window(defect, code):
    answer, schema, request, context = new_case()
    p = answer["plans"][0]
    if defect == "background": p["invalidation"]["price"] = "100.50"
    if defect == "unknown": context["market"]["quote_status"]["state"] = "unavailable"
    if defect == "contradiction": p["post_activation_invalidation"].update(operator="gte",price="101")
    if defect == "short_window": p["entry_deadline"] = "2026-10-12T02:05:30Z"
    if defect == "past_freshness": p["entry_deadline"] = "2026-10-12T05:00:00Z"; p["exit_deadline"] = "2026-10-12T06:00:00Z"
    with pytest.raises(DomainError) as error:
        validate_answer(answer, schema, request, context)
    assert error.value.code == code


def test_new_stage_fields_are_never_backported_into_legacy_answers():
    answer, schema, request, context = new_case()
    answer["schema_version"] = request["answer_schema_version"] = "1.2.0"
    with pytest.raises(DomainError) as error:
        validate_answer(answer, schema, request, context)
    assert error.value.code == "SCHEMA_INVALID"
    answer["plans"][0].pop("post_activation_invalidation")
    # Even an old background that would now be rejected stays valid under frozen old semantics.
    answer["plans"][0]["invalidation"]["price"] = "100.50"
    validate_answer(answer, schema, request, context)


def test_activation_event_invalidation_wins_and_deadline_close_does_not_activate():
    p = staged_record()
    p["definition"]["post_activation_invalidation"].update(operator="gt", price="4173.00")
    at = "2026-10-13T19:30:00Z"
    result = advance(p, observed([(at,"4174.00")]), at)
    assert result["lifecycle"] == "INVALIDATED" and result["invalidation_phase"] == "post_activation"
    p["definition"]["post_activation_invalidation"] = None
    p["definition"]["entry_deadline"] = at
    result = advance(p, observed([(at,"4174.00")]), at)
    assert result["lifecycle"] == "EXPIRED" and result["activation_observed_at"] is None


def test_observation_budget_uses_strict_deadline_and_future_valid_from():
    p = {"trading_style":"scalp", "valid_from":"2026-10-12T02:00:00Z", "entry_deadline":"2026-10-12T02:15:00Z",
        "entry":{"mode":"limit_retest"},"activation":{"kind":"bar_close","timeframe":"M5"},"entry_checks":[]}
    r = {"market_cutoff":"2026-10-12T02:00:00Z","window_end":"2026-10-12T14:00:00Z"}
    timing = entry_timing(p,r,"2026-10-12T02:00:01Z")
    assert timing["activation_closes_at_import"] == 2 and timing["state"] == "SUFFICIENT"
    p["valid_from"] = "2026-10-12T02:05:01Z"
    future = entry_timing(p,r,"2026-10-12T02:00:01Z")
    assert future["state"] == "INSUFFICIENT"
    assert future["remaining_at_import_seconds"] == 899
    assert future["effective_from"] == "2026-10-12T02:05:01Z"
