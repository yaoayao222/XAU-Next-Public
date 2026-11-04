import copy
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from xau_next.domains.plans.calculation import calculate
from xau_next.domains.plans.lifecycle import advance
from xau_next.domains.plans.public import PlansService
from xau_next.platform.db import Database
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-10-12T02:07:00Z"


def inputs():
    answer = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    market = next(
        e["value"]
        for e in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8"))
        if e["schema"] == "MarketSnapshot"
    )
    market["quote"]["event_time"] = NOW
    plan = {
        "plan_id": "p1",
        "version_id": "v1",
        "available_at": NOW,
        "lifecycle": "WAITING_TRIGGER",
        "lifecycle_at": NOW,
        "definition": answer["plans"][0],
        "revision": 1,
        "manual_observations": [],
    }
    from xau_next.domains.settings.public import defaults

    settings = {"revision": 1, "updated_at": NOW, "values": defaults()}
    settings["values"].update(
        default_volume_lots="0.01",
        per_trade_loss_cap={"amount": "2", "currency": "USD"},
        cost_assumption={
            "commission_roundtrip_per_lot": "20",
            "slippage_reserve_per_lot": "20",
            "other_cost_per_lot": "0",
            "currency": "USD",
            "valid_from": "2026-10-12T00:00:00Z",
            "valid_until": None,
            "account_scope_id": "account-demo",
            "symbol": "XAUUSD",
            "source_note": "合成固定算术费用",
        },
    )
    portfolio = {
        "snapshot_id": "portfolio1",
        "account_scope_id": "account-demo",
        "account": {"currency": "USD", "free_margin": "100"},
    }
    command = {
        "market_snapshot_id": market["snapshot_id"],
        "settings_revision": 1,
        "entry_price_override": None,
        "volume_lots_override": None,
        "sizing_mode": "fixed_volume",
    }
    return plan, market, portfolio, settings, command


@pytest.mark.parametrize("index", [0, 1])
def test_ac11_money_vectors(index):
    vector = json.loads((ROOT / "fixtures/acceptance-cases.json").read_text(encoding="utf-8"))[
        "money_vectors"
    ][index]
    p, m, a, s, c = inputs()
    x = vector["input"]
    p["definition"].update(
        side=x["side"], stop_loss=x["stop"], take_profit=x["target"], volume_lots=x["lots"]
    )
    p["definition"]["entry"]["price"] = x["entry"]
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    for k, v in vector["expected"].items():
        assert Decimal(result[k]) == Decimal(v)


def test_ac11_grid_and_wrong_direction_rejected():
    p, m, a, s, c = inputs()
    c["entry_price_override"] = "100.001"
    with pytest.raises(DomainError, match="网格") as err:
        calculate(p, m, a, s, c, evaluated_at=NOW)
    assert err.value.code == "PRICE_GRID_INVALID"
    c["entry_price_override"] = "98.00"
    with pytest.raises(DomainError) as err:
        calculate(p, m, a, s, c, evaluated_at=NOW)
    assert err.value.code == "PRICE_ORDER_INVALID"


def test_ac12_missing_commission_keeps_gross_and_no_midpoint():
    p, m, a, s, c = inputs()
    s["values"]["cost_assumption"]["commission_roundtrip_per_lot"] = None
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    assert result["gross_stop_loss"] is not None
    assert result["planned_loss"] is None
    assert result["budget_check"]["state"] == "unknown"
    p["definition"]["entry"]["price"] = None
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    assert result["entry_price"] is None and result["planned_loss"] is None


def test_ac11_exact_budget_comparison_and_risk_sizing():
    p, m, a, s, c = inputs()
    p["definition"]["stop_loss"] = "98.00"
    s["values"]["cost_assumption"].update(commission_roundtrip_per_lot="0.4", slippage_reserve_per_lot="0")
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    assert Decimal(result["planned_loss"]) == Decimal("2.004")
    assert result["budget_check"]["state"] == "not_satisfied"
    s["values"]["cost_assumption"]["commission_roundtrip_per_lot"] = "0"
    assert calculate(p, m, a, s, c, evaluated_at=NOW)["budget_check"]["state"] == "satisfied"
    c["sizing_mode"] = "risk_budget"
    s["values"]["default_volume_lots"] = "9"
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    assert result["volume_lots"] == "0.01" and result["margin_check"]["state"] == "unknown"
    s["values"]["per_trade_loss_cap"]["amount"] = "1.99"
    result = calculate(p, m, a, s, c, evaluated_at=NOW)
    assert result["volume_lots"] is None and result["budget_check"]["state"] == "not_satisfied"


def test_ac10_no_old_trigger_invalidation_priority_and_terminal_latch():
    p, m, _, _, _ = inputs()
    p["definition"]["activation"] = {
        "kind": "bar_close",
        "timeframe": "M5",
        "operator": "gte",
        "price": "100",
    }
    p["definition"]["invalidation"] = {
        "kind": "bar_close",
        "timeframe": "M5",
        "operator": "lte",
        "price": "100",
    }
    bar = {
        "open_at": "2026-10-12T02:00:00Z",
        "close_at": "2026-10-12T02:05:00Z",
        "closed": True,
        "close": "100",
    }
    m["series"] = [{"timeframe": "M5", "bars": [bar]}]
    assert advance(p, m, NOW)["lifecycle"] == "WAITING_TRIGGER"
    bar["close_at"] = "2026-10-12T02:10:00Z"
    changed = advance(p, m, "2026-10-12T02:10:01Z")
    assert changed["lifecycle"] == "INVALIDATED"
    bar["close"] = "200"
    assert advance(changed, m, "2026-10-12T02:15:00Z")["lifecycle"] == "INVALIDATED"
    p["definition"]["activation"] = {
        "kind": "quote_compare",
        "quote_side": "ask",
        "operator": "gte",
        "price": "100",
    }
    m["series"] = []
    m["quote"]["event_time"] = p["definition"]["entry_deadline"]
    assert advance(p, m, p["definition"]["entry_deadline"])["lifecycle"] == "EXPIRED"


def test_ac18_saved_evaluation_keeps_settings_snapshot(tmp_path):
    db = Database(tmp_path, clock=lambda: NOW)
    db.initialize()
    svc = PlansService(db, clock=lambda: NOW)
    p, m, a, s, c = inputs()
    with db.write() as uow:
        svc.register([p], uow=uow)
    first = svc.evaluate_plan("p1", c, market=m, portfolio=a, settings=s).to_dict()
    first_inputs = svc.get_evaluation_inputs(first["evaluation_id"]).to_dict()
    assert first_inputs["settings_snapshot"] == s
    changed = copy.deepcopy(s)
    changed["revision"] = 2
    changed["values"]["cost_assumption"]["commission_roundtrip_per_lot"] = "100"
    c["settings_revision"] = 2
    second = svc.evaluate_plan("p1", c, market=m, portfolio=a, settings=changed).to_dict()
    db.close()
    db = Database(tmp_path)
    db.initialize()
    svc = PlansService(db)
    records = svc.list_evaluations("p1").to_dict()["items"]
    assert {r["settings_revision"] for r in records} == {1, 2}
    assert first["planned_loss"] != second["planned_loss"]
    assert next(r for r in records if r["evaluation_id"] == first["evaluation_id"]) == first
    assert svc.get_evaluation_inputs(first["evaluation_id"]).to_dict() == first_inputs
    db.close()


def market_with_2480_bars(market):
    """Synthetic full market input matching the recurring collector's four-series size."""
    value = copy.deepcopy(market)
    value["snapshot_id"] = "snapshot-synthetic-2480"
    value["market_cutoff"] = "2026-10-12T02:10:00Z"
    value["captured_at"] = "2026-10-12T02:10:01Z"
    value["quote"]["event_time"] = value["market_cutoff"]
    value["quote_status"].update(observed_at=value["market_cutoff"], captured_at=value["captured_at"])
    end = parse_utc(value["market_cutoff"])
    value["series"] = []
    for timeframe, minutes, count in (("H1", 60, 240), ("M15", 15, 400), ("M5", 5, 640), ("M1", 1, 1200)):
        bars = []
        series_end = end.replace(minute=(end.minute // minutes) * minutes)
        for offset in reversed(range(count)):
            closed = series_end - timedelta(minutes=minutes * offset)
            opened = closed - timedelta(minutes=minutes)
            bars.append(
                {
                    "open_at": opened.isoformat().replace("+00:00", "Z"),
                    "close_at": closed.isoformat().replace("+00:00", "Z"),
                    "open": "100.00",
                    "high": "101.20",
                    "low": "99.00",
                    "close": "100.00",
                    "tick_volume": 100,
                    "closed": True,
                }
            )
        if timeframe == "M5":
            bars[-1]["close"] = "101.20"
        value["series"].append(
            {
                "timeframe": timeframe,
                "price_basis": "bid",
                "status": copy.deepcopy(value["quote_status"]),
                "bars": bars,
            }
        )
    return value


def test_evaluation_stores_header_and_exact_reference_without_repeated_series(tmp_path, monkeypatch):
    db = Database(tmp_path, clock=lambda: NOW)
    db.initialize()
    try:
        evaluated_at = "2026-10-12T02:10:01Z"
        service = PlansService(db, clock=lambda: evaluated_at)
        plan, market, portfolio, settings, command = inputs()
        market = market_with_2480_bars(market)
        original = copy.deepcopy(market)
        portfolio["account_status"] = copy.deepcopy(market["quote_status"])
        margin_fact = {"amount": "7.5", "currency": "USD"}
        command["market_snapshot_id"] = market["snapshot_id"]
        with db.write() as uow:
            service.register([plan], uow=uow)

        from xau_next.domains.plans import public as plans_public

        full_input_counts = []
        original_calculate = plans_public.calculate

        def calculate_with_full_market(record, supplied_market, *args, **kwargs):
            full_input_counts.append(sum(len(series["bars"]) for series in supplied_market["series"]))
            return original_calculate(record, supplied_market, *args, **kwargs)

        monkeypatch.setattr(plans_public, "calculate", calculate_with_full_market)
        evaluations = [
            service.evaluate_plan(
                "p1", command, market=market, portfolio=portfolio, settings=settings, margin_fact=margin_fact
            ).to_dict()
            for _ in range(2)
        ]
        assert full_input_counts == [2480, 2480]
        assert service.get_plan("p1").to_dict()["lifecycle"] == "ACTIVE"
        assert service.get_plan("p1").to_dict()["lifecycle_at"] == market["market_cutoff"]
        assert market == original
        with db.read() as scope:
            for wire in evaluations:
                record = db.documents("plans").get(scope, "evaluation", wire["evaluation_id"])
                assert record["storage_format"] == "plan-evaluation/2"
                assert "market_snapshot" not in record
                assert record["market_snapshot_header"] == {
                    key: value for key, value in market.items() if key != "series"
                }
                assert record["market_snapshot_ref"] == {
                    "domain": "market",
                    "kind": "snapshot",
                    "snapshot_id": market["snapshot_id"],
                    "revision": market["revision"],
                    "account_scope_id": market["account_scope_id"],
                    "symbol": market["symbol"],
                    "source_kind": market["source_kind"],
                }
                assert record["market_snapshot_ref"]["snapshot_id"] == wire["market_snapshot_id"]
                assert record["wire"] == wire
                assert record["input"] == command
                assert record["portfolio_snapshot"] == portfolio
                assert record["settings_snapshot"] == settings
                assert record["plan_snapshot"]["lifecycle"] == "ACTIVE"
                assert Decimal(wire["planned_loss"]) == Decimal("1.6")
                assert Decimal(wire["planned_net_profit"]) == Decimal("2.6")
                assert Decimal(wire["net_rr"]) == Decimal("1.625")
                assert wire["margin_required"] == "7.5"
                assert wire["margin_check"]["state"] == "satisfied"
                serialized = json.dumps(record, ensure_ascii=False).encode("utf-8")
                assert b'"series"' not in serialized
                assert len(serialized) < 10000
                assert len(serialized) < len(json.dumps(market).encode()) / 20
        # Returned saved inputs are detached: callers cannot modify frozen quote/settings facts.
        copied = service.get_evaluation_inputs(evaluations[0]["evaluation_id"]).to_dict()
        copied["market_snapshot_header"]["quote"]["bid"] = "1"
        assert (
            service.get_evaluation_inputs(evaluations[0]["evaluation_id"]).to_dict()[
                "market_snapshot_header"
            ]["quote"]["bid"]
            == market["quote"]["bid"]
        )
    finally:
        db.close()


def test_legacy_evaluation_inputs_are_read_without_rewriting_history(tmp_path):
    db = Database(tmp_path, clock=lambda: NOW)
    db.initialize()
    try:
        service = PlansService(db, clock=lambda: NOW)
        plan, market, portfolio, settings, command = inputs()
        market = market_with_2480_bars(market)
        command["market_snapshot_id"] = market["snapshot_id"]
        with db.write() as uow:
            service.register([plan], uow=uow)
        wire = service.evaluate_plan(
            "p1", command, market=market, portfolio=portfolio, settings=settings
        ).to_dict()
        legacy_wire = {**wire, "evaluation_id": "legacy-evaluation"}
        legacy_payload = {
            "wire": legacy_wire,
            "input": command,
            "plan_snapshot": plan,
            "portfolio_snapshot": portfolio,
            "settings_snapshot": settings,
            "market_snapshot": market,
        }
        with db.write() as uow:
            db.documents("plans").insert(
                uow, "evaluation", "legacy-evaluation", legacy_payload, parent_id="p1", available_at=NOW
            )
        with db.read() as scope:
            before = db.documents("plans").get(scope, "evaluation", "legacy-evaluation")
            history_before = db.documents("plans").history(scope, "evaluation", "legacy-evaluation")
            saved = service.get_evaluation_inputs("legacy-evaluation", scope=scope).to_dict()
        assert saved["storage_format"] == "plan-evaluation/1"
        assert saved["market_snapshot_ref"]["snapshot_id"] == market["snapshot_id"]
        assert saved["market_snapshot_header"] == {
            key: value for key, value in market.items() if key != "series"
        }
        assert saved["wire"] == legacy_wire
        assert saved["settings_snapshot"] == settings
        assert saved["input"] == command
        records = service.list_evaluations("p1").to_dict()["items"]
        assert (
            next(value for value in records if value["evaluation_id"] == "legacy-evaluation") == legacy_wire
        )
        assert next(value for value in records if value["evaluation_id"] == wire["evaluation_id"]) == wire
        with db.read() as scope:
            assert db.documents("plans").get(scope, "evaluation", "legacy-evaluation") == before
            assert db.documents("plans").history(scope, "evaluation", "legacy-evaluation") == history_before
        assert len(before["market_snapshot"]["series"]) == 4
    finally:
        db.close()
