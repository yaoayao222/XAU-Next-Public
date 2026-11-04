"""Synthetic price-scale checks; these do not measure trading profitability."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from xau_next.shared.scalp import (
    DEFAULT_SCALP_PREFERENCES,
    assess_scalp,
    effective_entry_range,
    validate_scalp_preferences,
)

NOW = "2026-10-15T00:00:00Z"


def market(bid="4000.00", ask="4000.20"):
    return {
        "market_cutoff": NOW,
        "quote": {"bid": bid, "ask": ask, "event_time": NOW},
        "quote_status": {"state": "available"},
        "contract": {"tick_size": "0.01", "contract_size": "100"},
        "series": [{"timeframe": "M1", "price_basis": "bid"},
                   {"timeframe": "M5", "price_basis": "bid"}],
    }


def plan(side="long", mode="limit_retest"):
    return {
        "trading_style": "scalp", "side": side,
        "entry": {"mode": mode, "price": "4000.20", "zone": {"low": "4000.00", "high": "4000.40"}},
        "stop_loss": "3999.00" if side == "long" else "4001.40",
        "take_profit": "4003.20" if side == "long" else "3997.20",
        "activation": {"kind": "immediate"}, "entry_checks": [], "max_holding_minutes": 10,
    }


def assess(candidate=None, snapshot=None, **kwargs):
    return assess_scalp(candidate or plan(), snapshot or market(), deepcopy(DEFAULT_SCALP_PREFERENCES),
                        now=NOW, **kwargs)


@pytest.mark.parametrize("side,mode,low,high", [
    ("long", "market", "4000.00", "4000.40"),
    ("short", "market", "4000.00", "4000.40"),
    ("long", "limit_retest", "4000.00", "4000.20"),
    ("short", "limit_retest", "4000.20", "4000.40"),
    ("long", "stop_breakout", "4000.20", "4000.40"),
    ("short", "stop_breakout", "4000.00", "4000.20"),
])
def test_effective_range_intersects_directional_order_condition(side, mode, low, high):
    result = effective_entry_range(plan(side, mode), market()["contract"])
    assert result["state"] == "valid"
    assert Decimal(result["low"]) == Decimal(low)
    assert Decimal(result["high"]) == Decimal(high)


def test_three_dollars_is_price_space_not_ticks_or_account_profit():
    candidate = plan()
    candidate["entry"]["zone"] = {"low": "4000.20", "high": "4000.20"}
    candidate["volume_lots"] = "0.01"
    snapshot = market()
    result = assess(candidate, snapshot)
    snapshot["contract"].update(tick_size="0.10", contract_size="1000")
    candidate["volume_lots"] = "2"
    changed = assess(candidate, snapshot)
    assert result["state"] == changed["state"] == "compatible"
    assert Decimal(result["target_space"]["low"]) == Decimal(changed["target_space"]["low"]) == 3


def test_old_requests_and_intraday_do_not_acquire_new_preferences():
    assert assess_scalp(plan(), market(), None, now=NOW) is None
    candidate = plan()
    candidate["trading_style"] = "intraday"
    assert assess(candidate) is None


def test_complete_range_checked_instead_of_most_favorable_price():
    candidate = plan(mode="market")
    candidate["entry"]["zone"] = {"low": "3997.00", "high": "4001.00"}
    candidate.update(stop_loss="3996.00", take_profit="4003.00")
    result = assess(candidate)
    assert result["state"] == "incompatible"
    assert "TARGET_SPACE_TOO_LARGE" in result["codes"]
    assert result["target_space"] == {"low": "2.00", "high": "6.00"}
    assert result["stop_space"] == {"low": "1.00", "high": "5.00"}
    assert result["entry_boundaries"] == [
        {"entry_price": "3997.00", "target_space": "6.00", "stop_space": "1.00"},
        {"entry_price": "4001.00", "target_space": "2.00", "stop_space": "5.00"},
    ]


def test_empty_intersection_is_semantic_error_without_moving_original_price():
    candidate = plan()
    candidate["entry"]["price"] = "3999.99"
    original = deepcopy(candidate)
    result = assess(candidate)
    assert result["state"] == "incompatible"
    assert "ENTRY_RANGE_EMPTY" in result["codes"]
    assert result["effective_entry_range"] is None
    assert candidate == original


@pytest.mark.parametrize("value", ["4000.201", "NaN", "Infinity", "0"])
def test_invalid_or_off_grid_exact_price_cannot_be_compatible(value):
    candidate = plan()
    candidate["entry"]["price"] = value
    assert assess(candidate)["state"] == "incompatible"


@pytest.mark.parametrize("field,value,code", [
    ("take_profit", "4001.99", "TARGET_SPACE_TOO_SMALL"),
    ("take_profit", "4005.01", "TARGET_SPACE_TOO_LARGE"),
    ("stop_loss", "4000.10", "PRICE_GEOMETRY_INVALID"),
])
def test_target_and_stop_boundaries_cover_whole_range(field, value, code):
    candidate = plan()
    candidate[field] = value
    assert code in assess(candidate)["codes"]


def test_inclusive_target_and_entry_distance_limits():
    candidate = plan(mode="market")
    candidate["entry"]["zone"] = {"low": "4000.00", "high": "4003.00"}
    candidate.update(take_profit="4005.00", stop_loss="3999.00")
    result = assess(candidate, market("4004.80", "4005.00"))
    assert result["state"] == "compatible"
    assert result["entry_distance"] == "2.00"
    assert assess(candidate, market("4004.81", "4005.01"))["state"] == "incompatible"


def test_execution_side_and_spread_change_entry_distance():
    candidate = plan()
    candidate["entry"]["zone"] = {"low": "3999.80", "high": "4000.20"}
    first = assess(candidate, market("4000.00", "4000.20"))
    second = assess(candidate, market("4000.00", "4002.21"))
    assert first["execution_side"] == "ask" and first["entry_distance"] == "0.00"
    assert second["entry_distance"] == "2.01" and "ENTRY_DISTANCE_EXCEEDED" in second["codes"]
    short = assess(plan("short"), market("3998.20", "4000.20"))
    assert short["execution_side"] == "bid" and short["entry_distance"] == "2.00"


@pytest.mark.parametrize("change", ["old", "future", "missing", "unavailable", "crossed"])
def test_unusable_quote_is_unknown_not_zero_distance(change):
    snapshot = market()
    if change == "old":
        snapshot["quote"]["event_time"] = "2026-10-14T23:59:49Z"
    elif change == "future":
        snapshot["quote"]["event_time"] = "2026-10-15T00:00:01Z"
    elif change == "missing":
        snapshot["quote"] = None
    elif change == "unavailable":
        snapshot["quote_status"]["state"] = "unavailable"
    else:
        snapshot["quote"]["ask"] = "3999.99"
    result = assess(snapshot=snapshot)
    assert result["state"] == "unknown"
    assert result["entry_distance"] is None
    assert result["current_target_space"] is None


def test_missing_contract_or_range_is_unknown():
    snapshot = market()
    snapshot["contract"] = None
    assert assess(snapshot=snapshot)["state"] == "unknown"
    candidate = plan(mode="market")
    candidate["entry"]["zone"] = None
    result = assess(candidate)
    assert result["state"] == "unknown" and result["effective_entry_range"] is None


@pytest.mark.parametrize("operator,threshold,distance,state", [
    ("gt", "4000.00", "0.01", "needed"),
    ("gte", "4002.00", "2.00", "needed"),
    ("lt", "4000.00", "0.01", "needed"),
    ("lte", "4000.00", "0.00", "price_satisfied"),
])
def test_threshold_distance_uses_real_basis_and_strict_grid(operator, threshold, distance, state):
    candidate = plan()
    candidate["activation"] = {"kind": "bar_close", "timeframe": "M5", "operator": operator, "price": threshold}
    result = assess(candidate)
    trigger = result["trigger_distances"][0]
    assert trigger["price_basis"] == "bid"
    assert trigger["distance"] == distance and trigger["state"] == state
    assert "新闭柱" in trigger["note"]


def test_price_over_threshold_does_not_claim_new_close_activation():
    candidate = plan()
    candidate["activation"] = {"kind": "bar_close", "timeframe": "M5", "operator": "lt", "price": "4000.10"}
    result = assess(candidate)
    trigger = result["trigger_distances"][0]
    assert trigger["state"] == "price_satisfied" and trigger["distance"] == "0.00"
    assert "不能认定" in trigger["note"]
    assert "等待" in result["required_path"]


def test_activated_threshold_no_longer_blocks_normal_retest_wait():
    candidate = plan()
    candidate["activation"] = {"kind": "bar_close", "timeframe": "M5", "operator": "gt", "price": "4005.00"}
    snapshot = market("4001.00", "4001.20")
    waiting = assess(candidate, snapshot)
    active = assess(candidate, snapshot, activated=True)
    assert waiting["state"] == "incompatible" and "TRIGGER_DISTANCE_EXCEEDED" in waiting["codes"]
    assert active["state"] == "compatible"
    assert active["trigger_distances"][0]["state"] == "latched"
    assert active["trigger_distances"][0]["distance"] is None
    assert "回踩" in active["required_path"]


def test_multistep_path_names_post_trigger_move_and_spread_assumption():
    candidate = plan()
    candidate["activation"] = {"kind": "bar_close", "timeframe": "M5", "operator": "gt", "price": "4005.00"}
    result = assess(candidate, market("4001.00", "4001.20"))
    assert "5.01美元" in result["required_path"]
    assert "点差0.20美元保持不变" in result["required_path"]
    assert "触发边界" in result["required_path"]


def test_precise_decimal_grid_does_not_round_boundary_into_compatibility():
    candidate = plan()
    snapshot = market()
    snapshot["contract"]["tick_size"] = "0.000000000000000000000000000001"
    candidate["entry"]["zone"] = {"low": "4000.2", "high": "4000.2"}
    candidate["take_profit"] = "4005.200000000000000000000000000001"
    result = assess(candidate, snapshot)
    assert "TARGET_SPACE_TOO_LARGE" in result["codes"]


def test_entry_checks_are_included_and_unknown_basis_is_not_bid():
    candidate = plan()
    candidate["entry_checks"] = [{"kind": "bar_close", "timeframe": "M1", "operator": "gt", "price": "4000.00"},
                                 {"kind": "quote_compare", "quote_side": "ask", "operator": "gte", "price": "4003.00"}]
    snapshot = market()
    snapshot["series"][0]["price_basis"] = "unknown"
    result = assess(candidate, snapshot)
    assert result["trigger_distances"][0]["state"] == "unknown"
    assert result["trigger_distances"][0]["distance"] is None
    assert result["trigger_distances"][1]["distance"] == "2.80"
    assert "TRIGGER_DISTANCE_EXCEEDED" in result["codes"]


@pytest.mark.parametrize("checks", [
    [{"kind": "quote_compare", "quote_side": "ask", "operator": "gt", "price": "4000.20"}],
    [{"kind": "bar_close", "timeframe": "M1", "operator": "gt", "price": "4000.10"},
     {"kind": "bar_close", "timeframe": "M1", "operator": "lte", "price": "4000.10"}],
])
def test_simultaneous_entry_check_contradictions_are_explicit(checks):
    candidate = plan()
    candidate["entry_checks"] = checks
    result = assess(candidate)
    assert result["state"] == "incompatible"
    assert "ENTRY_CHECK_CONTRADICTION" in result["codes"]


def test_sequential_activation_can_differ_from_later_entry_check():
    candidate = plan()
    candidate["activation"] = {"kind": "quote_compare", "quote_side": "ask", "operator": "gt", "price": "4000.20"}
    candidate["entry_checks"] = [{"kind": "quote_compare", "quote_side": "ask", "operator": "lte", "price": "4000.20"}]
    result = assess(candidate)
    assert result["state"] == "compatible"
    assert "ENTRY_CHECK_CONTRADICTION" not in result["codes"]


def test_holding_compatibility_requires_basis_above_preference():
    candidate = plan()
    candidate["max_holding_minutes"] = 20
    assert "HOLDING_BASIS_MISSING" in assess(candidate)["codes"]
    candidate["holding_time_basis"] = "本次M5局部结构需要两至四根闭柱观察，最长20分钟。"
    assert assess(candidate)["state"] == "compatible"
    candidate["max_holding_minutes"] = 31
    assert "HOLDING_LIMIT_EXCEEDED" in assess(candidate)["codes"]


def test_historical_far_candidate_is_diagnosed_without_mutation():
    candidate = plan("short")
    candidate.update(stop_loss="4087.72", take_profit="4073.67", max_holding_minutes=30,
                     holding_time_basis="历史方案最长30分钟", activation={"kind": "bar_close", "timeframe": "M5", "operator": "lt", "price": "4079.60"})
    candidate["entry"] = {"mode": "limit_retest", "price": "4085.36", "zone": {"low": "4085.36", "high": "4085.61"}}
    original = deepcopy(candidate)
    result = assess(candidate, market("4077.00", "4077.25"), activated=True)
    assert result["entry_distance"] == "8.36"
    assert result["target_space"] == {"low": "11.69", "high": "11.94"}
    assert {"TARGET_SPACE_TOO_LARGE", "ENTRY_DISTANCE_EXCEEDED"} <= set(result["codes"])
    assert result["trigger_distances"][0]["state"] == "latched"
    assert "反弹" in result["required_path"]
    assert candidate == original


@pytest.mark.parametrize("field,value", [
    ("version", "unknown"), ("price_unit", "account_profit"), ("target_min", "0"),
    ("target_max", "1"), ("max_entry_distance", "NaN"), ("max_trigger_distance", "-1"),
    ("preferred_holding_minutes", 31), ("max_holding_minutes", 31),
    ("max_holding_minutes", True),
])
def test_preference_validation_rejects_invalid_or_changed_units(field, value):
    preferences = deepcopy(DEFAULT_SCALP_PREFERENCES)
    preferences[field] = value
    assert validate_scalp_preferences(preferences)
    assert assess_scalp(plan(), market(), preferences, now=NOW)["state"] == "unknown"


def test_defaults_valid_and_assessment_does_not_mutate_frozen_preferences():
    preferences = deepcopy(DEFAULT_SCALP_PREFERENCES)
    original = deepcopy(preferences)
    assert validate_scalp_preferences(preferences) == []
    assess_scalp(plan(), market(), preferences, now=NOW)
    assert preferences == original


def test_diagnostics_follow_authoritative_wire_contract():
    root = Path(__file__).resolve().parents[2]
    document = json.loads((root / "contracts/openapi.json").read_text(encoding="utf-8"))
    answer_path = root / "contracts/analysis-answer.schema.json"
    answer = json.loads(answer_path.read_text(encoding="utf-8"))
    registry = Registry().with_resource(answer_path.as_uri(), Resource.from_contents(answer))
    schema = {"$id": (root / "contracts/openapi.json").as_uri(),
              "$ref": "#/components/schemas/ScalpSuitability", "components": document["components"]}
    validator = Draft202012Validator(schema, registry=registry)
    candidate = plan()
    candidate["activation"] = {"kind": "bar_close", "timeframe": "M5", "operator": "lt", "price": "4000.10"}
    for result in (assess(candidate), assess(candidate, activated=True), assess(snapshot={"quote": None})):
        assert list(validator.iter_errors(result)) == []
