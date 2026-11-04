import copy
from datetime import timedelta
from decimal import Decimal

from xau_next.domains.analysis.statistics import build_statistics
from xau_next.domains.review.prediction import compute_prediction
from xau_next.shared.values import parse_utc


def market():
    start = parse_utc("2026-10-12T00:00:00Z")
    bars = []
    for i in range(120):
        price = Decimal(100) + Decimal(i) / 100
        bars.append({"open_at": (start + timedelta(minutes=i)).isoformat().replace("+00:00", "Z"),
                     "close_at": (start + timedelta(minutes=i + 1)).isoformat().replace("+00:00", "Z"),
                     "open": str(price), "close": str(price + Decimal(".01")),
                     "high": str(price + Decimal(".02")), "low": str(price - Decimal(".01")), "closed": True})
    return {"snapshot_id": "synthetic-statistics", "market_cutoff": "2026-10-12T02:00:00Z",
            "series": [{"timeframe": "M1", "price_basis": "bid", "status": {"state": "available"}, "bars": bars}]}


def test_statistics_exclude_future_and_do_not_change_original():
    m = market()
    expected = build_statistics(m, [5, 30])
    m["series"][0]["bars"].append({**m["series"][0]["bars"][-1], "open_at": "2026-10-12T02:00:00Z",
                                   "close_at": "2026-10-12T02:01:00Z", "close": "100000"})
    original = copy.deepcopy(m)
    assert build_statistics(m, [5, 30]) == expected
    assert m == original
    five, thirty = expected["non_overlapping_change_distributions"]
    assert five["sample_count"] == 24 and five["delta_quantiles"] == {"p10": "0.05", "p50": "0.05", "p90": "0.05"}
    assert thirty["sample_count"] == 4 and thirty["delta_quantiles"] is None
    baseline = expected["frozen_baselines"][0]
    assert baseline["no_change"] == "101.2" and baseline["fixed_drift"] == "101.25"


def test_statistics_never_bridge_a_missing_bar():
    m = market()
    del m["series"][0]["bars"][40]
    result = build_statistics(m, [5])["non_overlapping_change_distributions"][0]
    assert result["skipped_incomplete_windows"] == 1
    assert result["sample_count"] == 22


def test_new_review_scores_width_and_frozen_baselines_without_changing_v1():
    m = market()
    horizon = {"horizon_minutes": 120, "end_at": "2026-10-12T02:00:00Z", "direction": "long",
               "expected_range": {"low": "99.99", "high": "105"}, "turning_points": [],
               "phases": [{"start_at": "2026-10-12T00:00:00Z", "end_at": "2026-10-12T02:00:00Z", "direction": "long"}]}
    version = {"version_id": "v", "forecast_id": "f", "available_at": "2026-10-12T00:00:05Z",
               "answer": {"schema_version": "1.1.0", "market_cutoff": "2026-10-12T00:00:00Z", "horizons": [horizon]},
               "baselines": [{"horizon_minutes": 120, "end_at": horizon["end_at"], "anchor_price": "100", "no_change": "100", "fixed_drift": "101"}]}
    result = compute_prediction(version, m, horizon_minutes=120, as_of="2026-10-12T02:01:00Z").to_dict()["review"]
    values = {item["name"]: item["value"] for item in result["metrics"]}
    assert result["method_version"] == "prediction_review/2"
    assert Decimal(values["range_boundary_absolute_error"]) == Decimal("3.79")
    assert Decimal(values["baseline.no_change.absolute_error"]) == Decimal("1.2")
    assert Decimal(values["baseline.fixed_drift.absolute_error"]) == Decimal(".2")
    assert values["baseline.no_change.direction_correct"] == "0"
    assert values["baseline.fixed_drift.direction_correct"] == "1"
    version["answer"]["schema_version"] = "1.2.0"
    current = compute_prediction(version, m, horizon_minutes=120, as_of="2026-10-12T02:01:00Z").to_dict()["review"]
    assert current["method_version"] == result["method_version"]
    assert current["metrics"] == result["metrics"]
    version["answer"]["schema_version"] = "1.0.0"
    old = compute_prediction(version, m, horizon_minutes=120, as_of="2026-10-12T02:01:00Z").to_dict()["review"]
    assert old["method_version"] == "prediction_review/1"
    assert not any(item["name"].startswith("baseline.") for item in old["metrics"])
