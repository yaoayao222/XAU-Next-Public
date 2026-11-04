"""Cutoff-safe, per-horizon comparisons; never a forecast or entry selector."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext

from xau_next.shared.values import parse_utc
from .entry_selection import quote_context
from .quality import forecast_evidence

VERSION = "forecast-reference/1"


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite reference")
    return result


def seconds(delta):
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / 1000000


def empty_horizon(minutes, end, remaining):
    return {"horizon_minutes": minutes, "end_at": end, "remaining_seconds": str(remaining),
            "state": "unknown", "reason": None, "history": None,
            "median_extrema_reference": None, "outer_extrema_reference": None, "baseline": None}


def range_reference(bid, low, high):
    low, high = number(low), number(high)
    if low > 0 or high < 0 or bid + low <= 0 or low > high:
        raise ValueError("Invalid excursion reference")
    return {"low": str(bid + low), "high": str(bid + high), "width": str(high - low),
            "low_offset_from_bid": str(low), "high_offset_from_bid": str(high)}


def baseline_reference(bid, baseline, cutoff):
    if not baseline:
        return None
    anchor_at = parse_utc(baseline["anchor_at"])
    anchor, unchanged, drift = (number(baseline[k]) for k in ("anchor_price", "no_change", "fixed_drift"))
    if anchor_at > cutoff or min(anchor, unchanged, drift) <= 0 or anchor != unchanged:
        raise ValueError("Invalid closed-anchor baseline")
    shift = drift - anchor
    if bid + shift <= 0:
        raise ValueError("Invalid reanchored diagnostic")
    return {"anchor_at": baseline["anchor_at"], "anchor_price": str(anchor),
            "age_seconds": str(seconds(cutoff - anchor_at)), "lookback_minutes": baseline["lookback_minutes"],
            "old_no_change": str(unchanged), "old_fixed_drift": str(drift),
            "current_bid_minus_anchor": str(bid - anchor), "reanchored_no_change": str(bid),
            "reanchored_fixed_drift": str(bid + shift), "drift_change": str(shift)}


def build_forecast_reference(request, context):
    """Use only the frozen request/context; verify cached statistics against raw closed data.

    Original evidence and baselines remain unchanged. Reanchoring is a separate
    descriptive comparison, not a correction to an old forecast or a probability.
    """
    with localcontext() as precision:
        precision.prec = 64
        return _build(request, context)


def _build(request, context):
    market = context["market"]
    cutoff = parse_utc(request["market_cutoff"])
    quote = quote_context(request, context)
    result = {"version": VERSION, "snapshot_id": market["snapshot_id"],
              "market_cutoff": request["market_cutoff"], "price_unit": "USD_per_ounce",
              "role": "descriptive_comparison_not_prediction", "quote":
              {k: quote[k] for k in ("state", "bid", "event_time", "reason")}, "horizons": [],
              "notes": ["逐周期比较原端点；不以更长周期、持仓时间或基准方向代替本周期判断。",
                        "历史中位极值参考可能漏掉实际波动；较宽参考也不保证覆盖，不是当前概率区间。",
                        "旧基准来自已完成K线；重新锚定只用于比较，不改旧值，也不选择模型方向或入场方式。",
                        "统计窗口按原forecast_evidence的整分钟长度；含秒剩余期限单列，不冒充完全同长窗口。"]}
    valid_identity = False
    try:
        valid_identity = (parse_utc(market["market_cutoff"]) == cutoff
                          and parse_utc(market["captured_at"]) <= cutoff
                          and request.get("market_snapshot_id", market["snapshot_id"]) == market["snapshot_id"])
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    if not valid_identity:
        result["quote"] = {"state": "unknown", "bid": None, "event_time": None,
                           "reason": "行情身份或采集截止不合格，未提供本次价格参照。"}
    evidence = context.get("forecast_evidence") or {}
    cached_matches = (evidence.get("version") == "forecast-evidence/1"
                      and evidence.get("market_snapshot_id") == market["snapshot_id"]
                      and evidence.get("market_cutoff") == request["market_cutoff"])
    checked = {}
    if valid_identity and cached_matches and quote["state"] == "available":
        try:
            recomputed = forecast_evidence(market, request["horizon_minutes"], window_start=request["window_start"])
            checked = {r["horizon_minutes"]: r for r in recomputed["horizons"]}
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            pass
    for minutes in request["horizon_minutes"]:
        end = parse_utc(request["window_start"]) + timedelta(minutes=minutes)
        remaining = seconds(end - cutoff)
        row = empty_horizon(minutes, end.isoformat().replace("+00:00", "Z"), remaining)
        result["horizons"].append(row)
        if remaining <= 0:
            row.update(state="ended", reason="原端点已结束，不提供新的剩余窗口参照。")
            continue
        if not valid_identity or quote["state"] != "available":
            row["reason"] = "行情身份不符，或缺少截止前新鲜且有效的Bid。"
            continue
        candidates = [h for h in evidence.get("horizons", []) if h.get("horizon_minutes") == minutes]
        if not cached_matches or len(candidates) != 1 or candidates[0] != checked.get(minutes):
            row["reason"] = "冻结统计与截止前原始资料不能唯一核对；不生成替代值。"
            continue
        source = candidates[0]
        if source.get("state") != "available" or source.get("sample_count", 0) < 20:
            row["reason"] = "历史完整窗口少于20或资料不足；不补数值或概率。"
            continue
        try:
            if source["price_basis"] != "bid" or parse_utc(source["end_at"]) > cutoff:
                raise ValueError("Unsafe statistical source")
            quantiles = source["quantiles"]
            for values in quantiles.values():
                if not number(values["p10"]) <= number(values["p50"]) <= number(values["p90"]):
                    raise ValueError("Unordered quantiles")
            bid = number(quote["bid"])
            median = range_reference(bid, quantiles["low_excursion"]["p50"], quantiles["high_excursion"]["p50"])
            outer = range_reference(bid, quantiles["low_excursion"]["p10"], quantiles["high_excursion"]["p90"])
            baseline = baseline_reference(bid, source.get("baseline"), cutoff)
            row.update(state="available", reason=None,
                history={k: deepcopy(source[k]) for k in ("timeframe", "sample_count", "remaining_minutes", "start_at", "end_at")},
                median_extrema_reference=median, outer_extrema_reference=outer, baseline=baseline)
            row["history"]["endpoint_delta"] = deepcopy(quantiles["endpoint_delta"])
            row["history"]["path_width"] = deepcopy(quantiles["path_width"])
        except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
            row["reason"] = "历史参照或价格不合格；保持未知，不改变原统计。"
    return result


def project_forecast_reference(value):
    """Explicit nested allowlist: unapproved extensions cannot enter model input."""
    def fields(item, names):
        return {k: deepcopy(item[k]) for k in names.split() if k in item}
    result = fields(value, "version snapshot_id market_cutoff price_unit role notes")
    result["quote"] = fields(value.get("quote") or {}, "state bid event_time reason")
    result["horizons"] = []
    for item in value.get("horizons", []):
        row = fields(item, "horizon_minutes end_at remaining_seconds state reason")
        history = item.get("history")
        row["history"] = fields(history, "timeframe sample_count remaining_minutes start_at end_at") if history else None
        if history:
            for name in ("endpoint_delta", "path_width"):
                row["history"][name] = fields(history.get(name) or {}, "p10 p50 p90")
        for name in ("median_extrema_reference", "outer_extrema_reference"):
            row[name] = fields(item[name], "low high width low_offset_from_bid high_offset_from_bid") if item.get(name) else None
        baseline = item.get("baseline")
        row["baseline"] = fields(baseline, "anchor_at anchor_price age_seconds lookback_minutes old_no_change old_fixed_drift current_bid_minus_anchor reanchored_no_change reanchored_fixed_drift drift_change") if baseline else None
        result["horizons"].append(row)
    return result
