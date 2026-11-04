"""SYNTHETIC: exact denominators and frozen-cutoff revision semantics."""
from copy import deepcopy

from xau_next.domains.review.experience_statistics import forecast_statistics
from xau_next.domains.review.public import ReviewService, compute_prediction
from xau_next.platform.db.public import Database
from tests.review.test_review import inputs, at


def row(id, value="1", state="available", *, method="prediction_review/2", horizon=30,
        coverage="partial", outcome="EVALUATED"):
    return {"review_id": id, "target_id": "forecast-" + id, "target_kind": "forecast",
            "method_version": method, "coverage": coverage, "outcome": outcome, "source_kind": "synthetic",
            "inputs": {"horizon_minutes": horizon, "forecast_version_id": "version-" + id,
                       "evaluation_scope": "version_descriptive", "method_parameters": {"neutral_band": "0.00", "price_basis": "bid"}},
            "metrics": [{"name": "direction_correct", "state": state, "value": value, "segment_index": None},
                        {"name": "range_contained", "state": "available", "value": "0", "segment_index": None}]}


def test_disjoint_denominators_do_not_treat_uncertain_or_missing_as_incorrect():
    rows = [row("a"), row("b", "0"), row("c", None, "not_applicable"),
            row("d", None, "unknown", outcome="DATA_INCOMPLETE")]
    original = deepcopy(rows)
    result = forecast_statistics(rows)
    assert result["record_total"] == 4 and len(result["rows"]) == 1
    value = result["rows"][0]
    assert value["direction"] == {"correct": 1, "incorrect": 1, "not_predicted": 1, "unknown": 1,
                                  "numerator": 1, "denominator": 2, "rate": "0.500000", "rate_state": "available"}
    assert value["range_containment"]["denominator"] == 4
    assert value["range_containment"]["correct"] == 0
    assert rows == original


def test_method_coverage_parameters_and_horizons_are_not_pooled():
    rows = [row("old"), row("new", method="prediction_review/3"), row("hour", horizon=60),
            row("complete", coverage="complete"), row("band")]
    rows[-1]["inputs"]["method_parameters"]["neutral_band"] = "1.00"
    assert len(forecast_statistics(rows)["rows"]) == 5


def test_frozen_endpoint_age_is_separate_and_output_is_order_independent():
    values = [row("a", method="prediction_review/3"), row("b", method="prediction_review/3")]
    for value, age in zip(values, ("5000", "10000")):
        value["metrics"].append({"name":"endpoint_max_age_ms", "value":age, "state":"available"})
    expected = forecast_statistics(values)
    assert len(expected["rows"]) == 2
    assert forecast_statistics(list(reversed(values))) == expected


def test_zero_denominator_and_metric_corruption_remain_unknown():
    a = row("unknown", None, "unknown")
    b = row("duplicate")
    b["metrics"].append(deepcopy(b["metrics"][0]))
    c = row("malformed", "2")
    for r in (a, b, c):
        r["metrics"] += [{"name": "m1.direction_correct", "state": "available", "value": "1"},
                         {"name": "phase.direction_correct", "state": "available", "value": "1", "segment_index": 0}]
    result = forecast_statistics([a, b, c])["rows"][0]["direction"]
    assert result["unknown"] == 3 and result["denominator"] == 0 and result["rate"] is None


def test_known_direction_can_count_while_other_review_dimensions_are_unknown():
    r = row("mixed", "0", method="prediction_review/3", outcome="DATA_INCOMPLETE")
    r["metrics"][1].update(state="unknown", value=None)
    value = forecast_statistics([r])["rows"][0]
    assert value["direction"]["incorrect"] == 1 and value["direction"]["denominator"] == 1
    assert value["range_containment"]["unknown"] == 1


def test_counts_cover_full_population_and_exclude_non_forecasts():
    values = [row(str(i)) for i in range(101)]
    values.append({**row("trade"), "target_kind": "actual_trade"})
    assert forecast_statistics(values)["record_total"] == 101
    assert forecast_statistics([])["rows"] == []


def test_statistics_projection_and_compaction_preserve_all_counts_without_private_extensions():
    from xau_next.domains.analysis.experience import project_experience, compact_experience, expand_experience
    value = {"schema_version": "experience/2", "forecast_statistics": forecast_statistics([row(str(i), horizon=i) for i in range(30)])}
    original = deepcopy(value)
    value["forecast_statistics"]["terminal_path"] = "SYNTHETIC-PRIVATE"
    value["forecast_statistics"]["rows"][0]["method_parameters"]["credential_ref"] = "SYNTHETIC-PRIVATE"
    projected = project_experience(value)
    assert projected == original
    compact = compact_experience(projected)
    assert expand_experience(compact) == original
    assert expand_experience(compact_experience({"schema_version": "experience/1", "cases": []})) == {"schema_version": "experience/1", "cases": []}


def test_service_statistics_use_only_revision_known_by_cutoff(tmp_path):
    clock = [at(65)]
    db = Database(tmp_path / "synthetic", clock=lambda: clock[0]); db.initialize()
    service = ReviewService(db, clock=lambda: clock[0])
    version, _, market, _ = inputs()
    try:
        first = compute_prediction(version, market, horizon_minutes=3, as_of=at(65)).to_dict()
        service.commit_review(first)
        clock[0] = at(70)
        changed = deepcopy(first); changed["input_fingerprint"] = "changed-synthetic-evidence"
        for metric in changed["review"]["metrics"]:
            if metric["name"] == "direction_correct": metric.update(state="unknown", value=None)
        service.commit_review(changed)
        old = service.get_context(at(66)).to_dict()["forecast_statistics"]
        new = service.get_context(at(71)).to_dict()["forecast_statistics"]
        assert old["record_total"] == new["record_total"] == 1
        assert old["rows"][0]["direction"]["denominator"] == 1
        assert new["rows"][0]["direction"]["unknown"] == 1
    finally:
        db.close()
