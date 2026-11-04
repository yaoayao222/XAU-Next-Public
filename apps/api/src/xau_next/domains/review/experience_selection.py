"""Deterministic, source-bound retrieval; outcome diversity is not a performance filter."""
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from xau_next.shared.values import parse_utc
from .experience_statistics import metric_bucket


def select_cases(candidates, predictions, target, cutoff, *, limit=8):
    signature = target["market_signature"]
    requested = sorted(set(target["horizon_minutes"]))
    counts = Counter()
    eligible = []
    instant = parse_utc(cutoff)
    for case in candidates:
        if case["target_kind"] != "forecast":
            counts["non_forecast"] += 1
            continue
        version = predictions.get(case["inputs"].get("forecast_version_id"))
        if not version or not signature.get("scope_key") or not version.get("market_signature", {}).get("scope_key"):
            counts["source_unresolved"] += 1
            continue
        try:
            if (parse_utc(case["available_at"]) > instant or parse_utc(case["window_end"]) > instant
                    or parse_utc(version["available_at"]) > instant or parse_utc(version["market_cutoff"]) > instant):
                counts["future_or_invalid_time"] += 1
                continue
        except (TypeError, ValueError, KeyError, AttributeError):
            counts["future_or_invalid_time"] += 1
            continue
        if version["market_signature"]["scope_key"] != signature["scope_key"]:
            counts["other_source"] += 1
            continue
        eligible.append(case)

    def metadata(case):
        return predictions[case["inputs"]["forecast_version_id"]]

    def identity(case):
        return metadata(case)["forecast_id"]

    def similarity(case):
        historical = metadata(case)["market_signature"]
        known = signature["state"] == historical["state"] == "available"
        return historical, known

    def rank(case):
        h = case["inputs"].get("horizon_minutes")
        distance = (min(max(Decimal(h)/n, Decimal(n)/h)-1 for n in requested)
                    if isinstance(h, int) and h > 0 and requested else Decimal("Infinity"))
        historical, known = similarity(case)
        shape = int(signature["shape"] != historical["shape"]) if known else int(signature["state"] == "available") * 2
        direction = int(signature["direction"] != historical["direction"]) if known else 0
        volatility = abs(Decimal(signature["range_bps"])-Decimal(historical["range_bps"])) if known else Decimal(0)
        stamp = (parse_utc(case["available_at"])-datetime(1970,1,1,tzinfo=UTC))//timedelta(microseconds=1)
        return (int(h not in requested), distance, int(case["method_version"] != target["review_method"]),
                shape, direction, volatility, -stamp, case["review_ref"])

    selected, reasons = [], {}
    seen_refs, seen_forecasts, seen_buckets = set(), set(), set()

    def choose(pool, reason):
        available = [c for c in pool if c["review_ref"] not in seen_refs]
        if not available or len(selected) >= limit:
            return False
        case = min(available, key=lambda c: (int(identity(c) in seen_forecasts),
                    int(metric_bucket(c,"direction_correct") in seen_buckets), rank(c)))
        selected.append(case)
        seen_refs.add(case["review_ref"])
        seen_forecasts.add(identity(case))
        seen_buckets.add(metric_bucket(case,"direction_correct"))
        reasons[case["review_ref"]] = reason
        return True

    for horizon in requested:
        choose([c for c in eligible if c["inputs"].get("horizon_minutes") == horizon], "requested_horizon_coverage")
    for bucket in ("incorrect", "correct", "unknown", "not_predicted"):
        if bucket not in seen_buckets:
            choose([c for c in eligible if metric_bucket(c,"direction_correct") == bucket], "outcome_diversity")
    while len(selected) < min(limit, len(eligible)):
        if not choose(eligible, "relevant_diverse_fill"):
            break

    cases = []
    for case in selected:
        historical, known = similarity(case)
        value = deepcopy(case)
        value["selection"] = {"reason": reasons[case["review_ref"]], "horizon_match": case["inputs"].get("horizon_minutes") in requested,
            "method_match": case["method_version"] == target["review_method"], "outcome_bucket": metric_bucket(case,"direction_correct"),
            "market_comparison": "comparable" if known else "unknown", "historical_signature": historical}
        cases.append(value)
    return cases, {"version": "forecast-retrieval/1", "candidate_total": len(candidates),
                   "eligible_forecasts": len(eligible), "excluded": dict(sorted(counts.items())),
                   "requested_horizons": requested, "review_method": target["review_method"],
                   "target_signature": signature, "selected_count": len(cases),
                   "covered_horizons": sorted({c["inputs"]["horizon_minutes"] for c in cases if c["inputs"].get("horizon_minutes") in requested}),
                   "unique_forecasts": len(seen_forecasts), "reused_forecast_count": len(cases)-len(seen_forecasts),
                   "outcome_counts": dict(sorted(Counter(metric_bucket(c,"direction_correct") for c in cases).items())),
                   "notes": ["8例仅为同账户/品种/资料类型的预测检索，不代表全库统计；参考模拟和实际成交仍在groups分别汇总。",
                       "先覆盖本次周期，再保留正确、错误、未知及未判断，优先不同预测身份；复用与重叠不作为独立样本。",
                       "同等条件依次按周期距离、评分方法、10分钟闭合M1形态/方向/波动距离、可用时间及引用排序。",
                       "形态为abs(change)/range>=0.6的directional、<=0.3的two_way及其它mixed；仅作检索，不是预测或交易规则。",
                       "行情缺失时market_comparison=unknown，不伪造相似；旧方法或其它周期回退均显式标记。"]}, eligible
