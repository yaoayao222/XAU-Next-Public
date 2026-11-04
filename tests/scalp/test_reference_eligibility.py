"""Synthetic evidence boundaries; historical quotes alone do not establish entry eligibility."""

import copy
from decimal import Decimal

import pytest
from xau_next.domains.review.public import ReviewService, compute_reference
from xau_next.platform.db.public import Database
from xau_next.shared.errors import DomainError
from xau_next.shared.scalp import DEFAULT_SCALP_PREFERENCES

from tests.review.test_review import at, inputs, metrics, quote, review


def sample(case="compatible", *, frozen=True, style="scalp", staged=True):
    _, plan, market, settings = inputs()
    definition = plan["definition"]
    definition.update(trading_style=style, max_holding_minutes=10 if style == "scalp" else 120,
                      holding_time_basis=None, stop_loss="99", take_profit="103", entry_checks=[],
                      invalidation={"kind": "quote_compare", "quote_side": "bid", "operator": "lt", "price": "98"},
                      entry={"mode": "limit_retest", "price": "100", "zone": {"low": "99.9", "high": "100"}})
    if staged:
        definition["post_activation_invalidation"] = None
    if frozen:
        plan["scalp_preferences"] = copy.deepcopy(DEFAULT_SCALP_PREFERENCES)
    market["coverage"] = {"complete": True}
    market["ticks"] = [quote(0, "99.98", "100"), quote(1, "103", "103.02")]
    if case == "far_target":
        definition["take_profit"] = "111.69"
        market["ticks"][-1] = quote(1, "111.69", "111.71")
    elif case == "exact_outside_zone":
        definition["entry"] = {"mode": "limit_retest", "price": "104", "zone": {"low": "100", "high": "100.1"}}
        market["ticks"][0] = quote(0, "100.08", "100.10")
    return plan, market, settings


@pytest.mark.parametrize("case", ["far_target", "exact_outside_zone", "compatible"])
def test_new_preference_plan_does_not_invent_reference_fill_from_quote_path(case):
    plan, market, settings = sample(case)
    original = copy.deepcopy((plan, market, settings))
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["method_version"] == "bidask_reference/3"
    assert result["inputs"]["method_parameters"]["execution_model_version"] == "bidask_reference/3"
    assert result["outcome"] == "DATA_INCOMPLETE"
    assert result["net_pnl"] is None and result["metrics"] == []
    assert result["coverage"] == "missing"
    assert "资格" in "；".join(result["reasons"])
    assert "报价" in "；".join(result["reasons"])
    assert (plan, market, settings) == original


def test_complete_tick_label_and_caller_assertions_cannot_bypass_eligibility_evidence():
    plan, market, settings = sample()
    market.update(tick_status="available", eligibility_complete=True, all_checks_passed=True)
    plan["scalp_suitability"] = {"state": "compatible", "range_net_check": {"state": "satisfied"}}
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "DATA_INCOMPLETE" and result["net_pnl"] is None
    assert result["metrics"] == []


@pytest.mark.parametrize("staged,method", [(False, "bidask_reference/1"), (True, "bidask_reference/2")])
def test_legacy_plan_keeps_original_method_and_result(staged, method):
    plan, market, settings = sample(frozen=False, staged=staged)
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["method_version"] == method and result["outcome"] == "TP"
    assert metrics(result)["reference_entry_price"]["value"] == "100"
    assert Decimal(result["net_pnl"]["amount"]) == Decimal("2.80")
    assert "scalp_preferences" not in plan


def test_intraday_keeps_staged_reference_even_if_preferences_are_present():
    plan, market, settings = sample(style="intraday")
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["method_version"] == "bidask_reference/2" and result["outcome"] == "TP"
    assert Decimal(result["net_pnl"]["amount"]) == Decimal("2.80")


def test_new_method_has_separate_identity_and_unknown_denominator_preserves_old_record(tmp_path):
    plan, market, settings = sample(frozen=False)
    legacy = compute_reference(plan, market, settings=settings, as_of=at(65))
    new_plan = copy.deepcopy(plan)
    new_plan["scalp_preferences"] = copy.deepcopy(DEFAULT_SCALP_PREFERENCES)
    new = compute_reference(new_plan, market, settings=settings, as_of=at(65))
    db = Database(tmp_path / "reference-eligibility", clock=lambda: at(65))
    db.initialize()
    try:
        service = ReviewService(db, clock=lambda: at(65))
        old_record = service.commit_review(legacy).to_dict()
        new_record = service.commit_review(new).to_dict()
        assert new_record["review_id"] != old_record["review_id"]
        assert service.get_review(old_record["review_id"]).to_dict() == old_record
        assert service.commit_review(legacy).to_dict() == old_record
        context = service.get_context(at(65)).to_dict()
        summary = context["groups"]["reference_plan"]
        assert summary["total"] == 2 and summary["scorable"] == 1 and summary["unknown"] == 1
        assert summary["not_triggered_or_not_entered"] == 0
        assert summary["outcomes"] == {"TP": 1, "DATA_INCOMPLETE": 1}
        assert summary["known_net_totals"]["USD"]["known_count"] == 1
        assert Decimal(summary["known_net_totals"]["USD"]["amount"]) == Decimal("2.80")
        assert summary["net_unknown_or_not_applicable"] == 1
    finally:
        db.close()


def test_new_reference_still_rejects_evaluation_before_availability():
    plan, market, settings = sample()
    plan["available_at"] = at(10)
    with pytest.raises(DomainError) as error:
        compute_reference(plan, market, settings=settings, as_of=at(9))
    assert error.value.code == "REVIEW_NOT_DUE"
