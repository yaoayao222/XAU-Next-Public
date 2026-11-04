"""Synthetic adverse cases for current entry decisions, not trading performance."""

import copy
import json
from pathlib import Path

import pytest
from xau_next.domains.plans.advice import entry_advice, position_advice, rule_now
from xau_next.domains.plans.calculation import calculate
from xau_next.domains.plans.lifecycle import advance
from xau_next.domains.settings.public import defaults

ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-10-12T02:07:00Z"


def case():
    answer = json.loads((ROOT / "fixtures/analysis-answer.guided.json").read_text(encoding="utf-8"))
    market = copy.deepcopy(next(e["value"] for e in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8")) if e["schema"] == "MarketSnapshot"))
    status = {"state": "available", "observed_at": NOW, "captured_at": NOW, "reasons": []}
    market.update(market_cutoff=NOW, captured_at=NOW, quote_status=status,
                  quote={"bid": "99.98", "ask": "100.00", "event_time": NOW})
    p = answer["plans"][0]
    p.update(activation={"kind": "immediate"}, invalidation={"kind": "quote_compare", "quote_side": "bid", "operator": "lt", "price": "98.80"})
    record = {"plan_id": "p", "version_id": "v", "definition": p, "available_at": NOW,
              "lifecycle": "WAITING_TRIGGER", "lifecycle_at": NOW, "manual_observations": [], "activation_observed_at": None}
    record = advance(record, market, NOW)
    settings = {"revision": 1, "values": defaults()}
    settings["values"].update(default_volume_lots="0.01", per_trade_loss_cap={"amount": "20", "currency": "USD"},
        cost_assumption={"commission_roundtrip_per_lot": "5", "slippage_reserve_per_lot": "20", "other_cost_per_lot": "0", "currency": "USD",
                         "valid_from": "2026-10-12T00:00:00Z", "valid_until": None, "account_scope_id": market["account_scope_id"], "symbol": "XAUUSD"})
    portfolio = {"snapshot_id": "pf", "account_scope_id": market["account_scope_id"], "account_status": status,
                 "orders_status": status, "orders": [], "account": {"currency": "USD", "free_margin": "1000"}}
    command = {"entry_price_override": "100.00", "volume_lots_override": None, "sizing_mode": "fixed_volume"}
    evaluation = {"evaluation_id": "e", "market_snapshot_id": market["snapshot_id"], "portfolio_snapshot_id": "pf", "settings_revision": 1,
                  **calculate(record, market, portfolio, settings, command, evaluated_at=NOW, margin_fact={"amount": "1", "currency": "USD"})}
    refresh = {"state": "FRESH", "fresh_until": "2026-10-12T04:00:00Z", "reasons": []}
    return record, market, portfolio, settings, evaluation, refresh


def decide(values, now=NOW):
    return entry_advice(*values, now=now)


def test_ready_is_based_on_current_ask_and_exact_money():
    values = case()
    assert decide(values)["state"] == "READY"
    assert decide(values)["entry_price"] == "100.00"
    values[4]["entry_price"] = "99.99"
    assert decide(values)["state"] == "BLOCKED"


@pytest.mark.parametrize("mode", ["market", "stop_breakout"])
def test_latched_trigger_does_not_allow_chasing_above_zone_or_target(mode):
    for ask in ("100.50", "104.00"):
        values = case()
        values[0]["definition"]["entry"]["mode"] = mode
        values[1]["quote"].update(bid=ask, ask=ask)
        assert advance(values[0], values[1], NOW)["lifecycle"] == "ACTIVE"
        assert decide(values)["state"] == "MISSED"


@pytest.mark.parametrize("side", ["long", "short"])
def test_limit_retest_outside_zone_waits_without_permission_to_enter(side):
    values = case()
    p, market, *_ = values
    if side == "short":
        p["definition"].update(side="short", stop_loss="101.2", take_profit="97",
            invalidation={"kind":"quote_compare", "quote_side":"ask", "operator":"gt", "price":"101.2"})
    price = "100.50" if side == "long" else "99.50"
    market["quote"].update(bid=price, ask=price)
    result = decide(values)
    assert result["state"] == "WAITING"
    assert "回踩" in result["title"]


@pytest.mark.parametrize("side,price", [("long","104"),("short","96"),("long","98"),("short","102")])
def test_limit_retest_never_waits_past_stop_or_target(side, price):
    values = case()
    if side == "short":
        values[0]["definition"].update(side="short", stop_loss="101.2", take_profit="97",
            invalidation={"kind":"quote_compare", "quote_side":"ask", "operator":"gt", "price":"101.2"})
    values[1]["quote"].update(bid=price, ask=price)
    assert decide(values)["state"] in {"MISSED", "INVALIDATED"}


def test_invalidation_applies_before_and_after_activation_and_wins_on_same_event():
    for lifecycle in ("WAITING_TRIGGER", "ACTIVE"):
        values = case()
        p, market, *_ = values
        p["lifecycle"] = lifecycle
        p["definition"]["activation"] = {"kind":"quote_compare", "quote_side":"bid", "operator":"gte", "price":"101"}
        p["definition"]["invalidation"] = {"kind":"quote_compare", "quote_side":"bid", "operator":"lt", "price":"100.1"}
        assert advance(p, market, NOW)["lifecycle"] == "INVALIDATED"
    p["lifecycle"] = "WAITING_TRIGGER"
    p["definition"]["activation"]["price"] = "99"
    assert advance(p, market, NOW)["lifecycle"] == "INVALIDATED"


@pytest.mark.parametrize("gap", ["quote", "orders", "account", "margin", "cost", "budget", "position", "pending", "stale_analysis", "reassess", "missing_zone", "old_version"])
def test_every_missing_gate_prevents_ready(gap):
    values = case()
    p, m, pf, _s, e, r = values
    if gap == "quote": m["quote"]["event_time"] = "2026-10-12T02:06:00Z"
    if gap == "orders": pf["orders_status"] = {**pf["orders_status"], "state": "unavailable"}
    if gap == "account": pf["account_status"] = {**pf["account_status"], "captured_at": "2026-10-12T02:00:00Z", "observed_at": "2026-10-12T02:00:00Z"}
    if gap in {"margin", "cost", "budget"}: e[gap + "_check"] = {"state": "unknown", "reasons": ["合成缺失"]}
    if gap in {"position", "pending"}: pf["orders"] = [{"symbol": "XAUUSD", "order": {"kind": "position" if gap == "position" else "pending_order"}}]
    if gap == "stale_analysis": r["state"] = "STALE"
    if gap == "reassess": r["state"] = "REASSESS"
    if gap == "missing_zone": p["definition"]["entry"]["zone"] = None
    if gap == "old_version": p["definition"].pop("trading_style")
    assert decide(values)["state"] not in {"READY", "WAITING"}


def test_wait_for_retest_and_all_current_checks():
    values = case()
    values[0]["definition"]["entry"]["price"] = "99.95"
    assert decide(values)["state"] == "WAITING"
    values[0]["definition"]["entry"]["price"] = "100"
    values[0]["definition"]["entry_checks"] = [{"kind": "quote_compare", "quote_side": "bid", "operator": "gte", "price": "100"}]
    assert decide(values)["state"] == "WAITING"
    values[0]["definition"]["entry_checks"][0]["price"] = "99.98"
    assert decide(values)["state"] == "READY"


def test_deadline_exactly_wins_and_quote_before_import_cannot_enter():
    values = case()
    values[0]["definition"]["entry_deadline"] = NOW
    assert decide(values)["state"] == "EXPIRED"
    values = case()
    values[1]["quote"]["event_time"] = "2026-10-12T02:06:59Z"
    assert decide(values)["state"] == "WAITING"
    assert decide(case(), now="2026-10-12T02:07:10Z")["state"] == "BLOCKED"


def test_late_bar_trigger_cannot_be_backdated_into_ready():
    values = case()
    p, m, *_ = values
    p["available_at"] = "2026-10-12T02:00:00Z"
    p.update(lifecycle="ACTIVE", lifecycle_at="2026-10-12T02:01:00Z", activation_observed_at=NOW)
    p["definition"]["activation"] = {"kind": "bar_close", "timeframe": "M1", "operator": "gte", "price": "99"}
    m["series"] = [{"timeframe": "M1", "price_basis": "bid", "status": {"state": "available", "captured_at": NOW}, "bars": [
        {"open_at": "2026-10-12T02:06:00Z", "close_at": NOW, "closed": True, "close": "100"}]}]
    assert decide(values)["state"] == "MISSED"
    p["activation_observed_at"] = "2026-10-12T02:01:01Z"
    assert decide(values)["state"] == "READY"  # A timely observed trigger may wait for a later retest.


def test_closed_bar_uses_latest_not_any_past_true_bar():
    values = case()
    m = values[1]
    rule = {"kind": "bar_close", "timeframe": "M1", "operator": "gt", "price": "100"}
    m["series"] = [{"timeframe": "M1", "price_basis": "bid", "status": {"state": "available", "captured_at": NOW}, "bars": [
        {"open_at": "2026-10-12T02:05:00Z", "close_at": "2026-10-12T02:06:00Z", "closed": True, "close": "101"},
        {"open_at": "2026-10-12T02:06:00Z", "close_at": NOW, "closed": True, "close": "99"},
    ]}]
    assert rule_now(rule, m, NOW, 10000) is False
    m["series"][0]["bars"][-1]["closed"] = False
    assert rule_now(rule, m, "2026-10-12T02:08:00Z", 10000) is None


def test_short_side_uses_bid_and_price_geometry():
    values = case()
    p, _m, _pf, _s, e, _r = values
    p["definition"].update(side="short", stop_loss="101.2", take_profit="97", invalidation={"kind": "quote_compare", "quote_side": "ask", "operator": "gt", "price": "101.2"})
    p["definition"]["entry"]["price"] = "99.98"
    e.update(entry_price="99.98")
    result = decide(values)
    assert result["state"] == "READY" and result["entry_price"] == "99.98"


def test_position_exit_uses_linked_original_plan_even_when_entry_expired():
    p, m, _, s, *_ = case()
    order = {"order": {"account_scope_id": m["account_scope_id"], "kind": "position", "broker_id": "ticket"},
             "plan_id": "p", "side": "long", "stop_loss": "90", "take_profit": "110"}
    p["lifecycle"] = "EXPIRED"
    p["definition"]["max_holding_minutes"] = 5
    advice = position_advice(order, p, m, s, now=NOW, opened_at="2026-10-12T02:01:00Z")
    assert advice["state"] == "EXIT" and advice["reason_code"] == "HOLDING_LIMIT"
    assert position_advice(order, p, m, s, now=NOW, opened_at=None)["state"] == "UNKNOWN"
