from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

from .common import (
    data,
    dec,
    due,
    get_series,
    inputs,
    market_ids,
    metric,
    parameters,
    prepare,
    review_base,
)

MINUTE = timedelta(minutes=1)


def floor_minute(at):
    return at.replace(second=0, microsecond=0)


def evaluate_segment(
    bars, start, end, direction, neutral_band, *, predicted_range=None, index=None, prefix=""
):
    """Closed Bid M1 only; non-minute edge clipping is explicit, interior gaps are unknown."""
    start_at, end_at = parse_utc(start), parse_utc(end)
    aligned_start = floor_minute(start_at) + (MINUTE if start_at != floor_minute(start_at) else timedelta())
    aligned_end = floor_minute(end_at)
    selected = [
        bar
        for bar in bars
        if parse_utc(bar["open_at"]) >= aligned_start and parse_utc(bar["close_at"]) <= aligned_end
    ]
    selected.sort(key=lambda bar: parse_utc(bar["open_at"]))
    expected_count = max(0, int((aligned_end - aligned_start).total_seconds() // 60))
    expected = [aligned_start + i * MINUTE for i in range(expected_count)]
    observed = [parse_utc(bar["open_at"]) for bar in selected]
    full = (
        expected_count > 0
        and observed == expected
        and all(parse_utc(bar["close_at"]) == parse_utc(bar["open_at"]) + MINUTE for bar in selected)
    )
    clipped = start_at != aligned_start or end_at != aligned_end
    coverage = "partial" if clipped or not full else "complete"
    if not selected:
        coverage = "missing"
    result = {
        "coverage": coverage,
        "start": selected[0]["open_at"] if selected else None,
        "end": selected[-1]["close_at"] if selected else None,
        "complete_path": full,
        "metrics": [],
    }
    names = ["direction", "delta", "direction_correct"]
    if predicted_range is not None:
        names += ["actual_low", "actual_high", "lower_excess", "upper_excess", "range_contained"]
    if not full:
        result["metrics"] = [
            metric(prefix + name, state="unknown", reason="Bid M1 闭柱缺失、重复或时间不连续。", index=index)
            for name in names
        ]
        return result
    first, last = selected[0], selected[-1]
    delta = dec(last["close"]) - dec(first["open"])
    actual_direction = "long" if delta > neutral_band else "short" if delta < -neutral_band else "neutral"
    metrics = [
        metric(prefix + "direction", observed=actual_direction, unit="label", index=index),
        metric(prefix + "delta", delta, unit="price", index=index),
    ]
    metrics.append(
        metric(
            prefix + "direction_correct",
            None if direction == "uncertain" else int(direction == actual_direction),
            state="not_applicable" if direction == "uncertain" else "available",
            reason="模型方向为 uncertain，不计正确或错误。" if direction == "uncertain" else None,
            index=index,
        )
    )
    if predicted_range is not None:
        low, high = min(dec(bar["low"]) for bar in selected), max(dec(bar["high"]) for bar in selected)
        lower = max(dec(predicted_range["low"]) - low, Decimal(0))
        upper = max(high - dec(predicted_range["high"]), Decimal(0))
        metrics += [
            metric("actual_low", low, unit="price"),
            metric("actual_high", high, unit="price"),
            metric("lower_excess", lower, unit="price"),
            metric("upper_excess", upper, unit="price"),
            metric("range_contained", int(lower == 0 and upper == 0)),
        ]
    result["metrics"] = metrics
    return result


def turning_metric(point, bars, index, start, end, as_of):
    start_at, end_at = parse_utc(start), parse_utc(end)
    earliest, latest = parse_utc(point["earliest_at"]), parse_utc(point["latest_at"])
    final_center = min(floor_minute(latest), floor_minute(end_at) - MINUTE)
    if final_center >= max(earliest, start_at) and final_center + 3 * MINUTE > parse_utc(as_of):
        raise DomainError("REVIEW_NOT_DUE", "转折评价需要等待右侧两根 M1 闭柱实际可用。", 409)
    indexed = {}
    duplicate = False
    for bar in bars:
        at = parse_utc(bar["open_at"])
        if at in indexed:
            duplicate = True
        indexed[at] = bar
    if duplicate:
        return metric(
            "turning_point_hit",
            state="unknown",
            index=index,
            observed=point["kind"],
            reason="相同时点存在重复闭柱，不能确定唯一转折路径。",
        )
    candidates = [
        bar
        for bar in bars
        if max(earliest, start_at) <= parse_utc(bar["open_at"]) <= latest
        and parse_utc(bar["open_at"]) < end_at
    ]
    unknown = duplicate or not candidates
    field = "high" if point["kind"] == "local_high" else "low"
    for bar in candidates:
        at, price = parse_utc(bar["open_at"]), dec(bar[field])
        in_range = dec(point["price_range"]["low"]) <= price <= dec(point["price_range"]["high"])
        if not in_range:
            continue
        neighbors = [indexed.get(at + distance * MINUTE) for distance in (-2, -1, 1, 2)]
        if any(neighbor is None for neighbor in neighbors):
            unknown = True
            continue
        values = [dec(neighbor[field]) for neighbor in neighbors]
        is_extremum = (
            all(price > value for value in values)
            if field == "high"
            else all(price < value for value in values)
        )
        if is_extremum and in_range:
            return metric("turning_point_hit", 1, index=index, observed=point["kind"])
        if any(price == value for value in values):
            unknown = True
    return metric(
        "turning_point_hit",
        None if unknown else 0,
        index=index,
        state="unknown" if unknown else "available",
        observed=point["kind"],
        reason="存在等高/等低平台或缺少完整左右邻柱，无法确定转折。" if unknown else None,
    )


def _compute_legacy(version, market, *, horizon_minutes, as_of, neutral_band="0.00"):
    version, market = data(version), data(market)
    answer = version["answer"]
    horizon = next((item for item in answer["horizons"] if item["horizon_minutes"] == horizon_minutes), None)
    if horizon is None:
        raise DomainError("HORIZON_NOT_FOUND", "指定观察窗口不属于该回答版本。", 404)
    start, end = answer["market_cutoff"], horizon["end_at"]
    due(end, as_of)
    due(version["available_at"], as_of)
    band = dec(neutral_band)
    if band < 0:
        raise DomainError("REVIEW_INPUT_INVALID", "中性带必须大于或等于零。")
    refs = market_ids(market)
    binding = inputs(
        version_id=version["version_id"],
        horizon=horizon_minutes,
        scope="version_descriptive",
        market_ids=refs,
        method=parameters(
            neutral_band=neutral_band,
            price_basis="bid",
            bar_timeframe="M1",
            turning_left_bars=2,
            turning_right_bars=2,
        ),
    )
    result = review_base(
        "forecast",
        version["forecast_id"],
        "prediction_review/2" if answer["schema_version"] in {"1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"} else "prediction_review/1",
        start,
        end,
        binding,
        refs + [version["version_id"]],
    )
    status = next(
        (row.get("status", {}) for row in market.get("series", []) if row["timeframe"] == "M1"),
        market.get("bar_status", {}),
    )
    if not refs or status.get("state", "available") != "available":
        result["reasons"] = ["缺少固定行情引用，或 Bid M1 来源/时间基准尚未确认为完整。"] + status.get(
            "reasons", []
        )
        return prepare(result, {"version": version, "market": market, "neutral_band": neutral_band}, as_of)
    bars, basis = get_series(market)
    bars = [
        bar
        for bar in bars
        if bar.get("closed") and parse_utc(bar.get("available_at", bar["close_at"])) <= parse_utc(as_of)
    ]
    if basis != "bid":
        result["reasons"] = ["prediction_review/1 需要 Bid M1 闭柱，当前价格侧不足。"]
        return prepare(result, {"version": version, "market": market, "neutral_band": neutral_band}, as_of)
    segment = evaluate_segment(
        bars, start, end, horizon["direction"], band, predicted_range=horizon["expected_range"]
    )
    result.update(
        coverage=segment["coverage"],
        evaluated_start=segment["start"],
        evaluated_end=segment["end"],
        metrics=segment["metrics"],
    )
    unknown = not segment["complete_path"]
    if answer["schema_version"] in {"1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
        result["metrics"].append(metric("predicted_direction", observed=horizon["direction"], unit="label"))
        result["metrics"].extend([metric("predicted_low", dec(horizon["expected_range"]["low"]), unit="price"),
                                  metric("predicted_high", dec(horizon["expected_range"]["high"]), unit="price")])
        if segment["complete_path"]:
            metrics = {m["name"]: m["value"] for m in segment["metrics"]}
            low, high = dec(metrics["actual_low"]), dec(metrics["actual_high"])
            predicted_low, predicted_high = dec(horizon["expected_range"]["low"]), dec(horizon["expected_range"]["high"])
            result["metrics"].extend([
                metric("range_boundary_absolute_error", abs(predicted_low - low) + abs(predicted_high - high), unit="price"),
                metric("predicted_range_width", predicted_high - predicted_low, unit="price"),
                metric("actual_range_width", high - low, unit="price"),
            ])
            last = next((bar for bar in bars if bar["close_at"] == segment["end"]), None)
            baseline = next((b for b in version.get("baselines", []) if b["horizon_minutes"] == horizon_minutes
                             and b["end_at"] == end), None)
            if baseline and last:
                for method in ("no_change", "fixed_drift"):
                    result["metrics"].append(metric("baseline." + method + ".absolute_error",
                        abs(dec(last["close"]) - dec(baseline[method])), unit="price"))
                    change = dec(baseline[method]) - dec(baseline["anchor_price"])
                    predicted = "long" if change > band else "short" if change < -band else "neutral"
                    actual_delta = dec(metrics["delta"])
                    actual = "long" if actual_delta > band else "short" if actual_delta < -band else "neutral"
                    result["metrics"].append(metric("baseline." + method + ".direction_correct", int(predicted == actual)))
    for index, phase in enumerate(horizon["phases"]):
        phase_result = evaluate_segment(
            bars, phase["start_at"], phase["end_at"], phase["direction"], band, index=index, prefix="phase."
        )
        result["metrics"].extend(phase_result["metrics"])
        result["metrics"].append(
            metric("phase.coverage", index=index, observed=phase_result["coverage"], unit="label")
        )
        result["metrics"].append(
            metric(
                "phase.evaluated_start",
                index=index,
                observed=phase_result["start"],
                unit="utc",
                state="available" if phase_result["start"] else "unknown",
            )
        )
        result["metrics"].append(
            metric(
                "phase.evaluated_end",
                index=index,
                observed=phase_result["end"],
                unit="utc",
                state="available" if phase_result["end"] else "unknown",
            )
        )
        unknown = unknown or not phase_result["complete_path"]
    for index, point in enumerate(horizon["turning_points"]):
        item = turning_metric(point, bars, index, start, end, as_of)
        result["metrics"].append(item)
        unknown = unknown or item["state"] == "unknown"
    delay = max(
        Decimal(0), Decimal(str((parse_utc(version["available_at"]) - parse_utc(start)).total_seconds()))
    )
    result["metrics"].append(metric("import_delay", delay, unit="seconds"))
    result["outcome"] = "DATA_INCOMPLETE" if unknown else "EVALUATED"
    if unknown:
        result["reasons"].append("相关全窗口指标或转折证据不完整，未将未知结果补为零。")
    if segment["coverage"] == "partial":
        result["reasons"].append("实际评价区间与请求区间不同；非分钟边界裁剪或缺柱情况已单列。")
    if delay > 0:
        result["reasons"].append("这是该版本的描述性预测评价；导入前路径不构成可执行方案表现。")
    return prepare(result, {"version": version, "market": market, "neutral_band": neutral_band}, as_of)


def compute_prediction(version, market, *, horizon_minutes, as_of, neutral_band="0.00", review_basis=None):
    """Old requests keep their frozen method; only explicitly frozen v3 requests align."""
    prepared = _compute_legacy(version, market, horizon_minutes=horizon_minutes,
                               as_of=as_of, neutral_band=neutral_band)
    if not review_basis or review_basis.get('method_version') != 'prediction_review/3':
        return prepared
    from .alignment import aligned_direction
    version, market = data(version), data(market)
    result = prepared.to_dict()['review']
    frozen = review_basis['market']
    horizon = next(h for h in version['answer']['horizons'] if h['horizon_minutes']==horizon_minutes)
    start,end = result['window_start'],result['window_end']
    result['method_version']='prediction_review/3'
    result['inputs']['market_snapshot_ids']=sorted(set(result['inputs']['market_snapshot_ids']+[frozen['snapshot_id']]))
    result['source_refs']=sorted(set(result['source_refs']+[frozen['snapshot_id']]))
    result['review_id']=review_base('forecast',result['target_id'],result['method_version'],start,end,
                                    result['inputs'],result['source_refs'])['review_id']
    # Retain closed-M1 and baseline results under explicit names, never relabel them as tick results.
    for m in result['metrics']:
        if m['name'] in {'direction','delta','direction_correct'} or m['name'].startswith('baseline.'):
            m['name']='m1.'+m['name']
        elif m['name'] in {'phase.direction','phase.delta','phase.direction_correct'}:
            m['name']='m1.'+m['name']
    age=review_basis['max_age_ms']
    aligned=aligned_direction(frozen,market,start,end,horizon['direction'],dec(neutral_band),age,as_of=as_of)
    result['metrics'].extend(aligned)
    result['metrics'].append(metric('endpoint_max_age_ms',age,unit='milliseconds'))
    for index,phase in enumerate(horizon['phases']):
        result['metrics'].extend(aligned_direction(frozen,market,phase['start_at'],phase['end_at'],
            phase['direction'],dec(neutral_band),age,index=index,prefix='phase.',
            frozen_start=parse_utc(phase['start_at'])==parse_utc(start),as_of=as_of))
    if any(m['state']=='unknown' for m in result['metrics'] if m['name'] in
           {'direction','delta','direction_correct','phase.direction','phase.delta','phase.direction_correct'}):
        result['outcome']='DATA_INCOMPLETE'
    result['reasons'].append('方向从本请求冻结Bid至原端点前最后唯一新鲜Bid评价；未使用未来报价或下一根M1开盘替换起点。')
    result['reasons'].append('区间与转折仍按已声明的完整M1裁剪方法；m1.*为保留的旧口径诊断，不与新方向混算。')
    return prepare(result,{'version':version,'market':market,'review_basis':review_basis,
                           'neutral_band':neutral_band},as_of)
