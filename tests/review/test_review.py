import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from xau_next.domains.review.public import (
    ReviewService,
    compute_actual,
    compute_prediction,
    compute_reference,
)
from xau_next.http.contracts import Contracts
from xau_next.platform.db.public import Database
from xau_next.shared.errors import DomainError

ROOT = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 10, 12, 2, tzinfo=UTC)


def test_staged_reference_uses_new_method_without_pretrigger_invalidation():
    _version, plan, market, settings = inputs()
    p = plan["definition"]
    p.update(activation={"kind":"quote_compare","quote_side":"bid","operator":"gt","price":"101"},
             invalidation={"kind":"quote_compare","quote_side":"bid","operator":"lt","price":"98"},
             post_activation_invalidation={"kind":"quote_compare","quote_side":"bid","operator":"lt","price":"99.90"},
             entry={"mode":"limit_retest","price":"100","zone":{"low":"99.90","high":"100.10"}})
    market["ticks"] = [quote(0,"99.80","99.82"),quote(1,"101.20","101.22"),
                       quote(2,"99.95","99.97"),quote(3,"103","103.02")]
    result = review(compute_reference(plan,market,settings=settings,as_of=at(65)))
    assert result["method_version"] == "bidask_reference/2"
    assert result["outcome"] == "TP"
    market["ticks"][2] = quote(2,"99.80","99.82")
    result = review(compute_reference(plan,market,settings=settings,as_of=at(65)))
    assert result["outcome"] == "EXPIRED_WITHOUT_ENTRY"


def at(minutes=0, seconds=0):
    return (
        (BASE + timedelta(minutes=minutes, seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def bar(minute, open="100", close="101", high="102", low="99"):
    return {
        "open_at": at(minute),
        "close_at": at(minute + 1),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "closed": True,
        "tick_volume": 10,
    }


def quote(minute, bid, ask, *, seconds=0, seq=None):
    return {"event_time": at(minute, seconds), "bid": bid, "ask": ask, "source_sequence": seq}


def inputs():
    answer = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    answer["horizons"] = [
        {
            "horizon_minutes": 3,
            "end_at": at(3),
            "direction": "long",
            "expected_range": {"low": "99", "high": "104"},
            "phases": [{"start_at": at(), "end_at": at(3), "direction": "long", "note": "合成连续区间"}],
            "turning_points": [],
            "confidence": None,
        }
    ]
    answer["market_cutoff"] = at()
    version = {"version_id": "version-1", "forecast_id": "forecast-1", "available_at": at(), "answer": answer}
    definition = copy.deepcopy(answer["plans"][0])
    definition.update(
        activation={"kind": "immediate"},
        invalidation={"kind": "manual_observation", "instruction": "人工核对"},
        entry={"mode": "market", "price": None, "zone": None},
        valid_from=at(),
        entry_deadline=at(20),
        exit_deadline=at(60),
        max_holding_minutes=30,
        stop_loss="98.8",
        take_profit="103",
        volume_lots="0.01",
    )
    plan = {
        "plan_id": "plan-1",
        "version_id": "version-1",
        "available_at": at(),
        "lifecycle": "WAITING_TRIGGER",
        "lifecycle_at": at(),
        "definition": definition,
        "manual_observations": [],
        "revision": 1,
    }
    market = {
        "snapshot_id": "market-1",
        "source_kind": "synthetic",
        "account_scope_id": "account-demo",
        "symbol": "XAUUSD",
        "from": at(-5),
        "to": at(65),
        "tick_status": "available",
        "price_basis": "bid",
        "status": {"state": "available"},
        "bars": [bar(0), bar(1, close="102", high="103"), bar(2, close="103", high="104")],
        "ticks": [quote(0, "99.98", "100"), quote(30, "101", "101.02")],
        "contract": {"contract_size": "100", "profit_currency": "USD", "calculation_mode": "linear_usd"},
    }
    settings = {
        "revision": 1,
        "values": {
            "default_volume_lots": None,
            "cost_assumption": {
                "commission_roundtrip_per_lot": "20",
                "slippage_reserve_per_lot": "0",
                "other_cost_per_lot": "0",
                "currency": "USD",
                "account_scope_id": "account-demo",
                "symbol": "XAUUSD",
                "source_note": "合成算术费用",
                "valid_from": at(-60),
                "valid_until": None,
            },
        },
    }
    return version, plan, market, settings


def metrics(review):
    return {item["name"]: item for item in review["metrics"] if item["segment_index"] is None}


def review(prepared):
    result = prepared.to_dict()["review"]
    Contracts(ROOT / "contracts").validate("Review", result)
    return result


def test_prediction_bid_m1_direction_range_and_phase():
    version, _, market, _ = inputs()
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))
    assert result["outcome"] == "EVALUATED" and result["coverage"] == "complete"
    values = metrics(result)
    assert values["direction"]["observed_label"] == "long"
    assert values["direction_correct"]["value"] == "1"
    assert values["range_contained"]["value"] == "1"
    assert values["delta"]["value"] == "3"
    assert result["inputs"]["evaluation_scope"] == "version_descriptive"


def test_prediction_gap_unknown_and_non_minute_clipping_partial():
    version, _, market, _ = inputs()
    market["bars"].pop(1)
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))
    assert result["outcome"] == "DATA_INCOMPLETE" and metrics(result)["direction_correct"]["value"] is None
    version, _, market, _ = inputs()
    version["answer"]["market_cutoff"] = at(0, 20)
    version["answer"]["horizons"][0]["phases"][0]["start_at"] = at(0, 20)
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))
    assert result["coverage"] == "partial" and result["evaluated_start"] == at(1)
    assert result["outcome"] == "EVALUATED"


def test_prediction_uncertain_not_wrong_and_future_window_not_due():
    version, _, market, _ = inputs()
    version["answer"]["horizons"][0]["direction"] = "uncertain"
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))
    assert metrics(result)["direction_correct"]["state"] == "not_applicable"
    with pytest.raises(DomainError) as error:
        compute_prediction(version, market, horizon_minutes=3, as_of=at(2))
    assert error.value.code == "REVIEW_NOT_DUE"


def test_turning_point_strict_neighbors_plateau_and_actual_availability():
    version, _, market, _ = inputs()
    horizon = version["answer"]["horizons"][0]
    horizon["turning_points"] = [
        {
            "kind": "local_high",
            "earliest_at": at(2),
            "latest_at": at(2),
            "price_range": {"low": "105", "high": "105"},
        }
    ]
    market["bars"] = [bar(i, high="105" if i == 2 else "102") for i in range(5)]
    with pytest.raises(DomainError, match="右侧"):
        compute_prediction(version, market, horizon_minutes=3, as_of=at(4))
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(5)))
    assert next(item for item in result["metrics"] if item["name"] == "turning_point_hit")["value"] == "1"
    market["bars"][3]["high"] = "105"
    result = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(5)))
    assert (
        next(item for item in result["metrics"] if item["name"] == "turning_point_hit")["state"] == "unknown"
    )


def test_ac14_time_exit_with_complete_cost_is_normal_net_result():
    _, plan, market, settings = inputs()
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "TIME_EXIT"
    assert Decimal(result["net_pnl"]["amount"]) == Decimal("0.80")
    assert result["method_version"] == "bidask_reference/1"


@pytest.mark.parametrize(
    "side,outcome,last_bid,last_ask,expected",
    [
        ("long", "TP", "104", "104.02", "2.80"),
        ("long", "SL", "98.5", "98.52", "-1.70"),
        ("short", "TP", "97.98", "98", "1.80"),
        ("short", "SL", "101.48", "101.5", "-1.70"),
    ],
)
def test_reference_bidask_sides_limit_target_and_gap_stop(side, outcome, last_bid, last_ask, expected):
    _, plan, market, settings = inputs()
    if side == "short":
        plan["definition"].update(side="short", stop_loss="101.2", take_profit="98")
        market["ticks"][0] = quote(0, "100", "100.02")
    market["ticks"][1] = quote(1, last_bid, last_ask)
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == outcome and Decimal(result["net_pnl"]["amount"]) == Decimal(expected)


def test_reference_timeout_and_side_or_coverage_missing_stay_unknown():
    _, plan, market, settings = inputs()
    market["ticks"][1] = quote(30, "101", "101.02", seconds=6)
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "DATA_INCOMPLETE" and result["net_pnl"] is None
    market["ticks"][1] = quote(30, "101", "101.02", seconds=5)
    assert review(compute_reference(plan, market, settings=settings, as_of=at(65)))["outcome"] == "TIME_EXIT"
    market["tick_status"] = "incomplete"
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "DATA_INCOMPLETE" and result["net_pnl"] is None


def test_ac14_ohlc_double_touch_separately_named_and_never_optimistic():
    _, plan, market, settings = inputs()
    market["ticks"] = []
    market["bars"] = [bar(1, low="98", high="104")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "AMBIGUOUS" and result["method_version"] == "ohlc_bounds/1"
    assert result["net_pnl"] is None


def test_ac15_late_answer_never_uses_earlier_entry():
    _, plan, market, settings = inputs()
    plan["available_at"] = at(7)
    market["ticks"] = [quote(3, "99.98", "100"), quote(8, "100.98", "101"), quote(38, "102", "102.02")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert metrics(result)["reference_entry_at"]["observed_label"] == at(8)
    assert metrics(result)["reference_entry_price"]["value"] == "101"


def test_closed_bar_trigger_only_uses_later_quote_and_same_bar_invalidation_wins():
    _, plan, market, settings = inputs()
    plan["available_at"] = at(0, 1)
    plan["definition"]["activation"] = {
        "kind": "bar_close",
        "timeframe": "M5",
        "operator": "gte",
        "price": "101",
    }
    plan["definition"]["entry"] = {"mode": "limit_retest", "price": "100", "zone": None}
    market["series"] = [
        {"timeframe": "M5", "price_basis": "bid", "bars": [{**bar(0, close="101"), "close_at": at(5)}]}
    ]
    market["ticks"] = [
        quote(1, "99.98", "100"),
        quote(5, "99.98", "100"),
        quote(6, "99.9", "99.92"),
        quote(36, "101", "101.02"),
    ]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "TIME_EXIT"
    assert metrics(result)["reference_entry_at"]["observed_label"] == at(6)
    assert metrics(result)["reference_entry_price"]["value"] == "100"
    plan["definition"]["invalidation"] = {
        "kind": "bar_close",
        "timeframe": "M5",
        "operator": "lte",
        "price": "101",
    }
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "NOT_TRIGGERED" and result["net_pnl"] is None


def test_stop_breakout_gap_and_old_crossing_are_distinct():
    _, plan, market, settings = inputs()
    plan["definition"]["entry"] = {"mode": "stop_breakout", "price": "100", "zone": None}
    market["ticks"] = [quote(-1, "99.9", "99.92"), quote(0, "100.3", "100.32"), quote(30, "101", "101.02")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert metrics(result)["reference_entry_price"]["value"] == "100.32"
    market["ticks"][0] = quote(-1, "100.1", "100.12")
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "EXPIRED_WITHOUT_ENTRY" and result["net_pnl"] is None


def test_same_timestamp_source_sequence_and_ambiguity():
    _, plan, market, settings = inputs()
    market["ticks"] = [quote(0, "99.98", "100"), quote(0, "100.48", "100.5"), quote(30, "101", "101.02")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "AMBIGUOUS"
    market["ticks"][0]["source_sequence"], market["ticks"][1]["source_sequence"] = 1, 2
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "TIME_EXIT" and metrics(result)["reference_entry_price"]["value"] == "100"


def test_superseded_ends_new_entry_but_keeps_existing_reference_exit():
    _, plan, market, settings = inputs()
    plan.update(lifecycle="SUPERSEDED", lifecycle_at=at(10))
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "TIME_EXIT"
    plan["definition"]["activation"] = {"kind": "manual_observation", "instruction": "待人工确认"}
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "NOT_TRIGGERED"


def trade(fees=True):
    def money(value):
        return {"amount": value, "currency": "USD"}

    return {
        "trade": {
            "trade_id": "trade-1",
            "account_scope_id": "account-demo",
            "symbol": "XAUUSD",
            "side": "long",
            "state": "closed",
            "opened_at": at(),
            "closed_at": at(10),
            "plan_id": None,
            "known_realized_subtotal": money("1.7"),
            "complete_realized_net": money("1.7") if fees else None,
            "fees_complete": fees,
        },
        "deals": [
            {
                "deal_id": "deal-1",
                "event_time": at(),
                "volume_lots": "0.02",
                "price": "100",
                "realized_profit": money("0"),
                "commission": money("-0.10"),
                "swap": money("0"),
                "other_fee": money("0"),
            },
            {
                "deal_id": "deal-2",
                "event_time": at(10),
                "volume_lots": "0.02",
                "price": "101",
                "realized_profit": money("2"),
                "commission": money("-0.10") if fees else None,
                "swap": money("-0.10"),
                "other_fee": money("0"),
            },
        ],
    }


def test_actual_signed_fees_deduplicated_and_unknown_entry_remains_unknown():
    item = trade()
    item["deals"].append(copy.deepcopy(item["deals"][1]))
    result = review(compute_actual(item, as_of=at(65)))
    assert Decimal(result["net_pnl"]["amount"]) == Decimal("1.70")
    item["trade"].update(opened_at=None, side=None, state="unknown")
    result = review(compute_actual(item, as_of=at(65)))
    assert result["outcome"] == "DATA_INCOMPLETE" and result["net_pnl"] is None
    assert item["trade"]["opened_at"] is None and any(
        "真实开仓时间未知" in reason for reason in result["reasons"]
    )


@pytest.fixture
def env(tmp_path):
    clock = [at(65)]
    db = Database(tmp_path / "review-data", clock=lambda: clock[0])
    db.initialize()
    yield ReviewService(db, clock=lambda: clock[0]), db, clock
    db.close()


def test_ac16_revision_idempotency_and_restart_preserve_original(env):
    service, db, clock = env
    first_input = compute_actual(trade(False), as_of=at(65))
    first = service.commit_review(first_input).to_dict()
    assert first["net_pnl"] is None
    assert service.commit_review(first_input).to_dict() == first
    clock[0] = at(70)
    second = service.commit_review(compute_actual(trade(True), as_of=at(70))).to_dict()
    assert second["review_id"] == first["review_id"] and second["revision"] == 2
    assert service.get_review(first["review_id"], revision=1).to_dict() == first
    restarted = ReviewService(db, clock=lambda: clock[0])
    assert restarted.get_review(first["review_id"]).to_dict() == second
    assert restarted.commit_review(first_input).to_dict() == first
    assert restarted.get_review(first["review_id"]).to_dict() == second
    assert len(restarted.list_reviews(revisions="all").to_dict()["items"]) == 2


def test_ac15_cutoff_uses_available_revision_never_future_repair(env):
    service, _, clock = env
    service.commit_review(compute_actual(trade(False), as_of=at(65)))
    clock[0] = at(70)
    service.commit_review(compute_actual(trade(True), as_of=at(70)))
    before = service.get_context(at(64)).to_dict()
    old = service.get_context(at(66)).to_dict()
    new = service.get_context(at(71)).to_dict()
    assert before["groups"]["actual_trade"]["total"] == 0
    assert old["groups"]["actual_trade"]["unknown"] == 1
    assert old["groups"]["actual_trade"]["known_net_totals"] == {}
    assert new["groups"]["actual_trade"]["scorable"] == 1
    assert old["review_refs"][0].endswith("r00000001")


def test_ac17_complete_denominator_keeps_unknown_and_not_triggered(env):
    service, _, _ = env
    for index in range(10):
        _, plan, market, settings = inputs()
        plan["plan_id"] = f"plan-{index}"
        if index >= 8:
            plan["definition"]["activation"] = {"kind": "manual_observation", "instruction": "缺少当时确认"}
        elif index >= 6:
            market["tick_status"] = "incomplete"
        service.commit_review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    context = service.get_context(at(65)).to_dict()
    summary = context["groups"]["reference_plan"]
    assert summary["total"] == 10 and summary["scorable"] == 6 and summary["unknown"] == 2
    assert summary["not_triggered_or_not_entered"] == 2
    assert summary["known_net_totals"]["USD"]["known_count"] == 6
    assert context["groups"]["actual_trade"]["total"] == 0


def test_source_time_unknown_and_tick_quality_are_independent():
    version, plan, market, settings = inputs()
    market["bar_status"] = {"state": "incomplete", "reasons": ["TIME_BASIS_UNKNOWN"]}
    prediction = review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))
    assert prediction["outcome"] == "DATA_INCOMPLETE" and prediction["metrics"] == []
    market["bar_status"] = {"state": "available", "reasons": []}
    market["tick_status"] = "incomplete"
    assert (
        review(compute_prediction(version, market, horizon_minutes=3, as_of=at(4)))["outcome"] == "EVALUATED"
    )
    assert (
        review(compute_reference(plan, market, settings=settings, as_of=at(65)))["outcome"]
        == "DATA_INCOMPLETE"
    )


def test_reference_market_delay_and_exact_entry_deadline():
    _, plan, market, settings = inputs()
    market["ticks"] = [
        quote(0, "99.98", "100"),
        quote(0, "100.08", "100.1", seconds=0.1),
        quote(0, "100.18", "100.2", seconds=0.2),
        quote(30, "101", "101.02", seconds=0.2),
    ]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65), simulation_delay_ms=150))
    assert metrics(result)["reference_entry_price"]["value"] == "100.2"
    assert Decimal(result["net_pnl"]["amount"]) == Decimal("0.60")
    plan["definition"]["entry"] = {"mode": "limit_retest", "price": "100", "zone": None}
    market["ticks"] = [quote(0, "100.98", "101"), quote(20, "99.98", "100")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert (
        result["outcome"] == "EXPIRED_WITHOUT_ENTRY"
        and metrics(result)["reference_entry_price"]["value"] is None
    )


def test_manual_confirmation_only_from_its_recorded_time():
    _, plan, market, settings = inputs()
    plan["definition"]["activation"] = {"kind": "manual_observation", "instruction": "需人工确认"}
    plan["manual_observations"] = [{"condition_role": "activation", "recorded_at": at(7), "note": "当时确认"}]
    market["ticks"] = [quote(3, "99.98", "100"), quote(8, "100.48", "100.5"), quote(38, "101", "101.02")]
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert metrics(result)["reference_entry_at"]["observed_label"] == at(8)


def test_same_time_bar_invalidation_and_quote_entry_require_order_evidence():
    _, plan, market, settings = inputs()
    plan["available_at"] = at(0, 1)
    plan["definition"].update(
        entry={"mode": "limit_retest", "price": "100", "zone": None},
        invalidation={"kind": "bar_close", "timeframe": "M5", "operator": "lt", "price": "99"},
    )
    market["series"] = [
        {"timeframe": "M5", "price_basis": "bid", "bars": [{**bar(0, close="98"), "close_at": at(5)}]}
    ]
    market["ticks"] = [quote(1, "100.98", "101"), quote(5, "99.98", "100"), quote(35, "101", "101.02")]
    assert review(compute_reference(plan, market, settings=settings, as_of=at(65)))["outcome"] == "AMBIGUOUS"


def test_unknown_fees_keep_reference_gross_and_all_missing_actual_amounts_are_not_zero():
    _, plan, market, settings = inputs()
    settings["values"]["cost_assumption"]["commission_roundtrip_per_lot"] = None
    result = review(compute_reference(plan, market, settings=settings, as_of=at(65)))
    assert result["outcome"] == "TIME_EXIT" and result["net_pnl"] is None
    assert Decimal(metrics(result)["reference_gross_pnl"]["value"]) == Decimal(1)
    item = trade(False)
    for deal in item["deals"]:
        for key in ("realized_profit", "commission", "swap", "other_fee"):
            deal[key] = None
    actual = review(compute_actual(item, as_of=at(65)))
    assert metrics(actual)["known_realized_subtotal"]["value"] is None
    assert metrics(actual)["known_realized_subtotal"]["state"] == "unknown"


def test_pure_calculation_can_run_in_process_pool():
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context

    version, _, market, _ = inputs()
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        prepared = pool.submit(compute_prediction, version, market, horizon_minutes=3, as_of=at(4)).result(
            timeout=15
        )
    assert review(prepared)["outcome"] == "EVALUATED"


def test_context_caps_packet_references_without_truncating_denominator(env):
    service, _, _ = env
    for index in range(82):
        item = trade()
        item["trade"]["trade_id"] = f"trade-{index}"
        service.commit_review(compute_actual(item, as_of=at(65)))
    context = service.get_context(at(65)).to_dict()
    assert context["groups"]["actual_trade"]["total"] == 82
    assert len(context["review_refs"]) == 80 and context["omitted_reference_count"] == 2
    assert context["review_reference_total"] == 82 and len(context["source_set_sha256"]) == 64
