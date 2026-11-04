"""Detached experiment windows; original answers and stored review revisions stay untouched."""

from copy import deepcopy
from decimal import Decimal

from xau_next.shared.values import parse_utc

from .alignment import aligned_direction
from .prediction import compute_prediction, evaluate_segment


def score_selection_window(answer, context, market, *, minutes, start, as_of, scope, available_at):
    horizon = next(h for h in answer["horizons"] if h["horizon_minutes"] == minutes)
    end = horizon["end_at"]
    unknown = {
        "state": "unknown",
        "direction_class": "unknown",
        "metrics": [],
        "range": {"state": "UNKNOWN"},
        "start": start,
        "end": end,
    }
    if start is None:
        return {**unknown, "reason": "NO_COMMON_OR_OWN_AVAILABILITY"}
    if parse_utc(start) < parse_utc(answer["market_cutoff"]):
        return {**unknown, "reason": "START_BEFORE_FROZEN_CUTOFF"}
    if parse_utc(start) >= parse_utc(end):
        return {**unknown, "reason": "NO_REMAINING_WINDOW"}
    if parse_utc(as_of) < parse_utc(end):
        return {**unknown, "state": "pending", "reason": "WINDOW_NOT_ENDED"}
    frozen = context["market"]
    if any(market.get(k) != frozen.get(k) for k in ("source_kind", "account_scope_id", "symbol")):
        return {**unknown, "reason": "SOURCE_IDENTITY_MISMATCH"}
    if market.get("price_basis") != "bid":
        return {**unknown, "reason": "NOT_BID_SOURCE"}
    if (
        market.get("status", {}).get("captured_at")
        and parse_utc(market["status"]["captured_at"]) > parse_utc(as_of)
    ) or any(
        b.get("available_at") and parse_utc(b["available_at"]) > parse_utc(as_of)
        for b in market.get("bars", [])
    ):
        return {**unknown, "reason": "SOURCE_AVAILABLE_AFTER_AS_OF"}
    derived = deepcopy(answer)
    derived["market_cutoff"] = start
    selected = next(h for h in derived["horizons"] if h["horizon_minutes"] == minutes)
    phase_indices = [i for i, p in enumerate(selected["phases"]) if parse_utc(p["end_at"]) > parse_utc(start)]
    selected["phases"] = [
        {**selected["phases"][i], "start_at": max((start, selected["phases"][i]["start_at"]), key=parse_utc)}
        for i in phase_indices
    ]
    point_indices = [
        i for i, p in enumerate(selected["turning_points"]) if parse_utc(p["latest_at"]) >= parse_utc(start)
    ]
    selected["turning_points"] = [
        {
            **selected["turning_points"][i],
            "earliest_at": max((start, selected["turning_points"][i]["earliest_at"]), key=parse_utc),
        }
        for i in point_indices
    ]
    version = {
        "version_id": "selection-detached",
        "forecast_id": answer["forecast_id"],
        "available_at": available_at,
        "answer": derived,
        "baselines": [],
    }
    review = compute_prediction(
        version, market, horizon_minutes=minutes, as_of=as_of, neutral_band="0.00"
    ).to_dict()["review"]
    # Preserve the old M1 diagnostics under their original explicit M1 meaning.
    for metric in review["metrics"]:
        name = metric["name"]
        if name in {
            "direction",
            "delta",
            "direction_correct",
            "phase.direction",
            "phase.delta",
            "phase.direction_correct",
        }:
            metric["name"] = "m1." + name
        index = metric.get("segment_index")
        if index is not None:
            mapping = phase_indices if name.startswith("phase.") else point_indices
            metric["segment_index"] = mapping[index]
    age = context["settings"]["values"]["quote_max_age_ms"]
    metrics = aligned_direction(
        frozen,
        market,
        start,
        end,
        horizon["direction"],
        Decimal(0),
        age,
        frozen_start=start == answer["market_cutoff"],
        as_of=as_of,
    )
    for original_index, phase in zip(phase_indices, selected["phases"]):
        review["metrics"].extend(
            aligned_direction(
                frozen,
                market,
                phase["start_at"],
                phase["end_at"],
                phase["direction"],
                Decimal(0),
                age,
                index=original_index,
                prefix="phase.",
                frozen_start=phase["start_at"] == answer["market_cutoff"],
                as_of=as_of,
            )
        )
    review["metrics"].extend(metrics)
    correct = next(m for m in metrics if m["name"] == "direction_correct")
    category = (
        "unknown"
        if correct["state"] == "unknown"
        else "uncertain"
        if correct["state"] == "not_applicable"
        else "correct"
        if Decimal(correct["value"]) == 1
        else "incorrect"
    )
    # Closed interior M1 only; completeness must never be promoted to full tick extrema.
    bars = [
        b for b in market.get("bars", []) if b.get("closed") and parse_utc(b["close_at"]) <= parse_utc(as_of)
    ]
    segment = evaluate_segment(
        bars, start, end, horizon["direction"], Decimal(0), predicted_range=horizon["expected_range"]
    )
    rg = {
        "state": "UNKNOWN",
        "coverage": segment["coverage"],
        "complete_interior": segment["complete_path"],
        "method": "closed-interior-M1/1",
        "boundary_absolute_error": None,
    }
    if segment["complete_path"]:
        lookup = {m["name"]: m for m in segment["metrics"] if m.get("segment_index") is None}
        low, high = (Decimal(lookup["actual_" + side]["value"]) for side in ("low", "high"))
        pl, ph = (Decimal(horizon["expected_range"][side]) for side in ("low", "high"))
        rg.update(
            state="AVAILABLE",
            actual_low=str(low),
            actual_high=str(high),
            predicted_low=str(pl),
            predicted_high=str(ph),
            lower_error=str(abs(pl - low)),
            upper_error=str(abs(ph - high)),
            boundary_absolute_error=str(abs(pl - low) + abs(ph - high)),
            lower_miss=str(max(Decimal(0), pl - low)),
            upper_miss=str(max(Decimal(0), high - ph)),
            predicted_width=str(ph - pl),
            actual_width=str(high - low),
            contained=int(pl <= low and ph >= high),
        )
    return {
        "state": "scored",
        "direction_class": category,
        "start": start,
        "end": end,
        "metrics": review["metrics"],
        "range": rg,
        "scope": scope,
        "phase_order_and_exact_turn_error": "UNSUPPORTED",
        "coverage": review["coverage"],
    }
