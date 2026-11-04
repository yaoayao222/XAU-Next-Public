"""SYNTHETIC regression cases; no model call, terminal or production writes."""

from copy import deepcopy
from datetime import timedelta

import pytest

from test_review import inputs, at, quote, metrics, review
from xau_next.domains.review.public import compute_prediction
from xau_next.domains.review.alignment import aligned_direction, endpoint_quote
from xau_next.domains.review.sessions import session_diagnosis
from xau_next.domains.review.diagnostics import diagnose_review
from xau_next.shared.values import parse_utc


def sample():
    version, _, market, _ = inputs()
    frozen = {
        "snapshot_id": "frozen",
        "source_kind": "synthetic",
        "account_scope_id": "account-demo",
        "symbol": "XAUUSD",
        "market_cutoff": at(),
        "quote": quote(0, "99", "99.02"),
        "quote_status": {"state": "available", "captured_at": at()},
    }
    market["bars"][-1].update(close="99.5", low="99")
    market["ticks"] = [
        quote(0, "99", "99.02"),
        quote(3, "99.5", "99.52"),
        quote(3, "110", "110.02", seconds=1),
    ]
    basis = {"method_version": "prediction_review/3", "market": frozen, "max_age_ms": 5000}
    return version, market, basis


def test_frozen_anchor_reverses_legacy_label_without_rewriting_legacy():
    v, m, b = sample()
    original = deepcopy((v, m))
    old = review(compute_prediction(v, m, horizon_minutes=3, as_of=at(4)))
    aligned = review(compute_prediction(v, m, horizon_minutes=3, as_of=at(4), review_basis=b))
    assert metrics(old)["direction_correct"]["value"] == "0"
    assert metrics(aligned)["direction_correct"]["value"] == "1"
    assert metrics(aligned)["delta"]["value"] == "0.5"
    assert metrics(aligned)["m1.direction_correct"]["value"] == "0"
    assert metrics(aligned)["endpoint_price"]["value"] == "99.5"  # 110 is future at endpoint
    assert old["review_id"] != aligned["review_id"] and aligned["method_version"] == "prediction_review/3"
    assert (v, m) == original


@pytest.mark.parametrize(
    "failure", ["missing_ticks", "stale", "ambiguous", "future_only", "future_frozen", "incomplete", "future_history", "future_tick"]
)
def test_alignment_unknown_is_not_filled_from_m1(failure):
    v, m, b = sample()
    if failure == "missing_ticks":
        m["ticks"] = []
    elif failure == "stale":
        m["ticks"] = [quote(2, "99.5", "99.52", seconds=54)]
    elif failure == "ambiguous":
        m["ticks"] = [quote(3, "99.5", "99.52"), quote(3, "100", "100.02")]
    elif failure == "future_only":
        m["ticks"] = [quote(3, "110", "110.02", seconds=1)]
    elif failure == "future_frozen":
        b["market"]["quote"] = quote(0, "99", "99.02", seconds=1)
    elif failure == "incomplete":
        m["tick_status"] = "incomplete"
    elif failure == "future_history":
        m["status"]["captured_at"] = at(5)
    elif failure == "future_tick":
        m["ticks"][1]["available_at"] = at(5)
    r = review(compute_prediction(v, m, horizon_minutes=3, as_of=at(4), review_basis=b))
    assert metrics(r)["direction_correct"]["state"] == "unknown"
    assert r["outcome"] == "DATA_INCOMPLETE"


def test_same_millisecond_equivalent_timestamp_is_ambiguous():
    _, m, _ = sample()
    m["ticks"] = [
        quote(3, "99.5", "99.52"),
        {**quote(3, "100", "100.02"), "event_time": at(3).replace(".000Z", "Z")},
    ]
    assert endpoint_quote(m, at(3), 5000)[0] is None


def weekend_sample():
    start = "2026-10-16T20:53:00.000Z"
    end = "2026-10-16T20:57:00.000Z"

    def bar(minute):
        t = parse_utc(start) + timedelta(minutes=minute)
        return {
            "open_at": t.isoformat().replace("+00:00", "Z"),
            "close_at": (t + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "closed": True,
        }

    r = {"window_start": start, "window_end": end}
    m = {"source_kind": "mt5", "symbol": "XAUUSD", "bars": [bar(0), bar(1)]}
    s = {
        "server": "TradeMaxGlobal-Live2",
        "company": "TradeMax Global Limited",
        "broker_timezone": "UTC+03:00",
    }
    return r, m, s, bar


def test_only_closed_minutes_missing_and_broker_timezone_conversion():
    r, m, s, _ = weekend_sample()
    d = session_diagnosis(r, m, s)
    assert d["state"] == "market_closed"
    assert (
        d["expected_minutes"],
        d["observed_minutes"],
        d["closed_missing_minutes"],
        d["unexplained_missing_minutes"],
    ) == (4, 2, 2, 0)


def test_trading_minute_gap_is_not_hidden_by_weekend():
    r, m, s, _ = weekend_sample()
    m["bars"].pop(0)
    d = session_diagnosis(r, m, s)
    assert d["state"] == "mixed" and d["unexplained_missing_minutes"] == 1


def test_wrong_source_and_real_prices_during_closed_time_are_not_declared_closed():
    r, m, s, bar = weekend_sample()
    assert session_diagnosis(r, m, {**s, "server": "another-broker"})["state"] == "unverified"
    m["bars"].append(bar(2))
    assert session_diagnosis(r, m, s)["state"] == "schedule_conflict"


def test_diagnostics_preserve_original_and_keep_observation_time_separate():
    v, m, b = sample()
    old = review(compute_prediction(v, m, horizon_minutes=3, as_of=at(4)))
    original = deepcopy(old)
    d = diagnose_review(old, v, b, {"window": m, "source": {}}, as_of=at(8))
    assert old == original and old["method_version"] == "prediction_review/1"
    assert d["computed_at"] == at(8) and d["direction_basis"] == "legacy_complete_m1"
    assert next(x for x in d["alignment_metrics"] if x["name"] == "direction_correct")["value"] == "1"
