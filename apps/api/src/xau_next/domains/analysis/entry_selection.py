"""Frozen comparison context for supported entry modes, never an entry selector."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from xau_next.shared.entry_timing import planned_timing, timing_reference
from xau_next.shared.values import parse_utc

VERSION = "entry-selection/1"
MODES = ("market", "limit_retest", "stop_breakout")


def quote_context(request, context):
    market = context["market"]
    quote = market.get("quote") or {}
    status = market.get("quote_status") or {}
    result = {"state": "unknown", "bid": None, "ask": None, "spread": None,
              "event_time": None, "reason": "缺少截止前新鲜且同次采集的有效Bid/Ask。"}
    try:
        cutoff = parse_utc(request["market_cutoff"])
        event, captured = parse_utc(quote["event_time"]), parse_utc(status["captured_at"])
        bid, ask = Decimal(quote["bid"]), Decimal(quote["ask"])
        age = context["settings"]["values"]["quote_max_age_ms"]
        if (status["state"] != "available" or not event <= captured <= cutoff
                or not 0 <= (cutoff - event).total_seconds() * 1000 <= age
                or not bid.is_finite() or not ask.is_finite() or not 0 < bid <= ask):
            return result
        result.update(state="available", bid=str(bid), ask=str(ask), spread=str(ask-bid),
                      event_time=quote["event_time"], reason=None)
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        pass
    return result


def build_entry_selection_context(request, context):
    """Compute baseline timing through the same pure policy used by import checks.

    Proposed prices/direction/mode remain the model's responsibility. The table
    assumes no extra checks and valid_from <= estimated availability; later starts
    or additional observations require a new calculation, not extending old plans.
    """
    reference = context.get("timing_reference") or timing_reference()
    base = {"valid_from": request["market_cutoff"], "entry_deadline": request["window_end"],
            "entry_checks": [], "trading_style": "scalp", "activation": {"kind": "quote_compare"},
            "entry": {"mode": "market"}}
    cases = []
    for timeframe in (None, "M1", "M5", "M15"):
        activation = {"kind": "bar_close", "timeframe": timeframe} if timeframe else {"kind": "quote_compare"}
        for mode in MODES:
            plan = {**base, "activation": activation, "entry": {"mode": mode}}
            timing = planned_timing(plan, request, context)
            cases.append({"activation_kind": activation["kind"], "timeframe": timeframe,
                          "entry_mode": mode, "minimum_entry_deadline": timing["minimum_entry_deadline"],
                          "required_activation_closes": timing["required_activation_closes"],
                          "post_trigger_reserve_seconds": timing["post_trigger_reserve_seconds"]})
    first = planned_timing(base, request, context)
    styles = request.get("analysis_preferences", {}).get("enabled_styles", ["scalp", "intraday"])
    windows = [{"trading_style": style,
                "analysis_fresh_until": planned_timing({**base, "trading_style": style}, request, context)["analysis_fresh_until"]}
               for style in styles if style in {"scalp", "intraday"}]
    return {"version": VERSION, "snapshot_id": context["market"]["snapshot_id"],
            "market_cutoff": request["market_cutoff"], "price_unit": "USD_per_ounce",
            "quote": quote_context(request, context), "planning_delay_seconds": reference["planning_delay_seconds"],
            "estimated_available_at": first["available_at"], "style_windows": windows, "minimum_timing": cases,
            "assumptions": "时间表假设valid_from不晚于预计可用时刻且entry_checks为空；若生效更晚或有额外检查，须重算更晚的最低截止。"
                           "表中两次闭柱是机会预算，不是必须等满两根才触发。",
            "meaning": "三种方式均可选择，但须比较当前路径、触发后价格可达性及剩余时效；不会自动选择方向、方式或生成新价位。",
            "limits": "报价只代表原截止点；近端波动/结构引用near_term和decision_context，不是未来回踩概率或成交保证。"
                      "市价意图仍须明确允许区间、激活和全部检查；每个模式最多一个候选，允许无候选，不按配额强迫突破或市价。"}


def project_entry_selection_context(value):
    def fields(item, names):
        return {k: deepcopy(item[k]) for k in names.split() if k in item}
    result = fields(value, "version snapshot_id market_cutoff price_unit planning_delay_seconds estimated_available_at assumptions meaning limits")
    result["quote"] = fields(value.get("quote") or {}, "state bid ask spread event_time reason")
    result["style_windows"] = [fields(row, "trading_style analysis_fresh_until") for row in value.get("style_windows", [])]
    result["minimum_timing"] = [fields(row, "activation_kind timeframe entry_mode minimum_entry_deadline required_activation_closes post_trigger_reserve_seconds")
                                for row in value.get("minimum_timing", [])]
    return result
