"""Descriptive forecast counts with disjoint, explicit denominators; no rescoring."""
from collections import defaultdict
from decimal import Decimal, localcontext

from .common import digest

METHOD_FIELDS = ("neutral_band", "price_basis", "bar_timeframe", "turning_left_bars",
                 "turning_right_bars", "execution_model_version", "simulation_delay_ms",
                 "execution_quote_wait_ms")


def metric_bucket(record, name):
    """Only the unprefixed whole-window metric counts; duplicates stay unknown."""
    values = [m for m in record.get("metrics", []) if m.get("name") == name
              and m.get("segment_index") is None]
    if len(values) != 1:
        return "unknown"
    metric = values[0]
    if metric.get("state") == "not_applicable":
        return "not_predicted"
    if metric.get("state") == "available" and isinstance(metric.get("value"), str):
        if metric["value"] == "1":
            return "correct"
        if metric["value"] == "0":
            return "incorrect"
    return "unknown"


def _counts(records, metric):
    counts = {name: 0 for name in ("correct", "incorrect", "not_predicted", "unknown")}
    for record in records:
        counts[metric_bucket(record, metric)] += 1
    denominator = counts["correct"] + counts["incorrect"]
    with localcontext() as context:
        context.prec = 64
        rate = (format((Decimal(counts["correct"]) / denominator).quantize(Decimal("0.000001")), "f")
                if denominator else None)
    return {**counts, "numerator": counts["correct"], "denominator": denominator,
            "rate": rate, "rate_state": "available" if denominator else "no_scorable_samples"}


def _endpoint_age(record):
    values = [m for m in record.get("metrics", []) if m.get("name") == "endpoint_max_age_ms"]
    if len(values) == 1 and values[0].get("state") == "available":
        value = values[0].get("value")
        if isinstance(value, str) and value.isdigit() and int(value) > 0:
            return value
    return None


def forecast_statistics(records):
    """Input is the latest revision per review identity actually available by cutoff."""
    forecasts = [r for r in records if r["target_kind"] == "forecast"]
    groups = defaultdict(list)
    definitions = {}
    for row in forecasts:
        binding = row["inputs"]
        method = {key: binding.get("method_parameters", {}).get(key) for key in METHOD_FIELDS}
        cohort = {"horizon_minutes": binding.get("horizon_minutes"),
                  "method_version": row["method_version"],
                  "evaluation_scope": binding["evaluation_scope"], "coverage": row["coverage"],
                  "source_kind": row.get("source_kind", "unknown"), "method_parameters": method,
                  "endpoint_max_age_ms": _endpoint_age(row)}
        key = digest(cohort)
        definitions[key] = cohort
        groups[key].append(row)
    result = []
    for key, values in groups.items():
        result.append({**definitions[key], "total": len(values),
                       "unique_targets": len({v["target_id"] for v in values}),
                       "unique_versions": len({v["inputs"]["forecast_version_id"] for v in values
                                               if v["inputs"].get("forecast_version_id")}),
                       "direction": _counts(values, "direction_correct"),
                       "range_containment": _counts(values, "range_contained")})
    result.sort(key=lambda row: (row["horizon_minutes"] or 0, row["method_version"],
                                row["evaluation_scope"], row["coverage"],
                                digest([row["method_parameters"],row["endpoint_max_age_ms"]]),
                                row["source_kind"]))
    return {"version": "forecast-period-statistics/1", "record_total": len(forecasts),
            "population": "latest_available_revision_per_review_identity",
            "rows": result,
            "notes": ["每行方向及区间各自满足：正确+错误+未预测/不适用+未知=total；比率分母仅为正确+错误。",
                      "range_containment的correct表示已评价路径落在预测范围内，不代表方向正确或范围足够窄。",
                      "按周期、方法、参数、覆盖、评价范围和资料类型分组；旧M1裁剪与冻结Bid方向不混算。",
                      "比率为六位小数的描述性分数，不是下一次成功概率；零分母为null，未知不算错也不补零。",
                      "多个版本、周期与重叠窗口并非独立样本；未提供独立样本胜率或合成总准确率。"]}
