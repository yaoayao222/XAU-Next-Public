"""Pure price-scale diagnostics; condition confirmation and net money gates stay separate."""

import re
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation, localcontext

from .values import parse_utc
from .condition_observation import rule_check

DEFAULT_SCALP_PREFERENCES = {
    "version": "scalp-preferences/1",
    "price_unit": "USD_per_ounce",
    "target_min": "2.00",
    "target_max": "5.00",
    "max_entry_distance": "2.00",
    "max_trigger_distance": "2.00",
    "preferred_holding_minutes": 10,
    "max_holding_minutes": 30,
}


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value)
        if len(text) > 48 or not re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", text):
            return None
        result = Decimal(text)
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


def _text(value):
    return format(value, ".2f") if value.as_tuple().exponent >= -2 else format(value, "f")


def _grid(contract):
    if not isinstance(contract, dict):
        return None
    source = contract.get("price_grid") or {}
    step = _decimal(source.get("step", contract.get("tick_size")))
    origin = _decimal(source.get("origin", "0"))
    return (step, origin) if step is not None and step > 0 and origin is not None else None


def _on_grid(value, grid):
    return (value - grid[1]) % grid[0] == 0


def validate_scalp_preferences(preferences):
    if not isinstance(preferences, dict):
        return ["超短线偏好必须是明确的版本化设置。"]
    reasons = []
    if preferences.get("version") != DEFAULT_SCALP_PREFERENCES["version"]:
        reasons.append("超短线偏好版本未知。")
    if preferences.get("price_unit") != DEFAULT_SCALP_PREFERENCES["price_unit"]:
        reasons.append("超短线价差单位必须是金价美元/盎司，不能使用报价点或账户盈利金额。")
    values = {}
    for field in ("target_min", "target_max", "max_entry_distance", "max_trigger_distance"):
        value = _decimal(preferences.get(field))
        if value is None or value < 0 or (field.startswith("target_") and value == 0):
            reasons.append(f"超短线偏好 {field} 必须为有效的{'正数' if field.startswith('target_') else '非负数'}价差。")
        else:
            values[field] = value
    if "target_min" in values and "target_max" in values and values["target_min"] > values["target_max"]:
        reasons.append("目标价差下限不能大于上限。")
    preferred = preferences.get("preferred_holding_minutes")
    maximum = preferences.get("max_holding_minutes")
    if type(preferred) is not int or type(maximum) is not int or not 1 <= preferred <= maximum <= 30:
        reasons.append("成交后优先持仓时长须为正整数且不超过兼容上限，兼容上限不得超过30分钟。")
    return reasons


def effective_entry_range(plan, contract=None):
    with localcontext() as context:
        context.prec = 120
        return _effective_entry_range(plan, contract)


def _effective_entry_range(plan, contract):
    result = {"state": "unknown", "low": None, "high": None, "reasons": [], "codes": []}

    def fail(state, code, reason):
        result.update(state=state, reasons=[reason], codes=[code])
        return result

    entry = plan.get("entry") or {}
    side, mode = plan.get("side"), entry.get("mode")
    if side not in {"long", "short"} or mode not in {"market", "limit_retest", "stop_breakout"}:
        return fail("invalid", "ENTRY_SEMANTICS_INVALID", "入场方向或方式不明确，无法计算有效入场范围。")
    zone = entry.get("zone")
    if not isinstance(zone, dict):
        return fail("unknown", "ENTRY_RANGE_UNKNOWN", "缺少允许入场区间，不能使用区间中点或精确价代替全部允许范围。")
    low, high = _decimal(zone.get("low")), _decimal(zone.get("high"))
    if low is None or high is None or low <= 0 or high <= 0 or low > high:
        return fail("invalid", "ENTRY_RANGE_INVALID", "允许入场区间的价格或上下限顺序无效。")
    exact = _decimal(entry.get("price"))
    if mode != "market" and (exact is None or exact <= 0):
        return fail("invalid", "ENTRY_PRICE_INVALID", "回踩或突破入场缺少有效的精确条件价格。")
    if entry.get("price") is not None and (exact is None or exact <= 0):
        return fail("invalid", "ENTRY_PRICE_INVALID", "入场精确价格无效。")
    grid = _grid(contract)
    if grid and any(not _on_grid(value, grid) for value in (low, high, exact) if value is not None):
        return fail("invalid", "PRICE_GRID_INVALID", "入场区间或精确价格不符合合约价格网格；保留原价，不自动取整。")
    if mode != "market":
        upper_bound = (side == "long") == (mode == "limit_retest")
        if upper_bound:
            high = min(high, exact)
        else:
            low = max(low, exact)
    if low > high:
        return fail("invalid", "ENTRY_RANGE_EMPTY", "精确条件价格与允许区间没有可行交集，原方案入场语义矛盾。")
    result.update(state="valid", low=_text(low), high=_text(high))
    if contract is not None and grid is None:
        result.update(state="unknown", codes=["PRICE_GRID_UNKNOWN"], reasons=["合约价格网格缺失或无效，无法确认全部允许入场价格。"])
    return result


def _quote(market, now, max_age_ms):
    quote = market.get("quote")
    if not quote or (market.get("quote_status") or {}).get("state") != "available":
        return None, "当前Bid/Ask报价不可用，距离保留未知。"
    bid, ask = _decimal(quote.get("bid")), _decimal(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None, "当前Bid/Ask报价无效或倒挂，距离保留未知。"
    try:
        cutoff = parse_utc(market["market_cutoff"])
        current = now if isinstance(now, datetime) else parse_utc(now or market["market_cutoff"])
        at = parse_utc(quote["event_time"])
        if cutoff > current or at > cutoff or not 0 <= (current - at).total_seconds() * 1000 <= max_age_ms:
            return None, "报价过时或时间异常，等待新鲜报价后重算距离。"
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "报价时间或行情截止时间不完整，距离保留未知。"
    return {"bid": bid, "ask": ask}, None


def _trigger(rule, role, market, quote, grid, activated, now, max_age_ms):
    kind = rule.get("kind", "unknown")
    basis = rule.get("quote_side") if kind == "quote_compare" else None
    if kind == "bar_close":
        series = [item for item in market.get("series", []) if item.get("timeframe") == rule.get("timeframe")]
        basis = series[0].get("price_basis") if len(series) == 1 else None
    result = {"role": role, "kind": kind, "price_basis": basis, "threshold": rule.get("price"),
              "distance": None, "state": "unknown", "note": ""}
    if role == "activation" and activated:
        result.update(state="latched", note="触发已由生命周期记录确认；不要求当前价格再次回到触发阈值。")
        return result, None
    threshold = _decimal(rule.get("price"))
    operator = rule.get("operator")
    if kind not in {"bar_close", "quote_compare"}:
        result["note"] = "该条件没有可计算的价格阈值，不能补造距离或触发概率。"
        return result, None
    if threshold is None or threshold <= 0 or operator not in {"gt", "gte", "lt", "lte"}:
        result["note"] = "触发价格或比较方式无效。"
        return result, "TRIGGER_SEMANTICS_INVALID"
    if grid and not _on_grid(threshold, grid):
        result["note"] = "触发阈值不符合合约价格网格；不自动取整。"
        return result, "PRICE_GRID_INVALID"
    if role != "activation" and rule.get("observation") == "background":
        current = now.isoformat().replace("+00:00", "Z") if isinstance(now, datetime) else now
        checked = rule_check(rule, market, current or market["market_cutoff"], max_age_ms)
        if checked["matched"] is None:
            result["note"] = "持续背景资料不足：" + "；".join(checked["reasons"])
        elif checked["matched"]:
            result.update(state="background_satisfied", note=f"持续背景已由{checked['observed_at']}的合法观察核验；入场时重新检查，不锁存，也不要求再穿越阈值或另等新闭柱。")
        else:
            result.update(state="background_waiting", note="持续背景当前不成立，等待符合条件的合法观察；不以当前报价代替闭柱，不允许入场。")
        return result, None
    if quote is None or basis not in {"bid", "ask"} or not grid:
        result["note"] = "缺少新鲜同侧报价、明确K线报价侧或价格网格，阈值距离保留未知。"
        return result, None
    price = quote[basis]
    required = _threshold_boundary(threshold, operator, grid)
    if operator in {"gt", "gte"}:
        distance = max(Decimal(0), required - price)
    else:
        distance = max(Decimal(0), price - required)
    note = f"按当前{basis.title()}计算价格位移；不混用Bid与Ask。"
    if kind == "bar_close":
        note += "即使价格已越过阈值，仍须等待合法的新闭柱；距离为零不能认定条件已触发。"
    else:
        note += "价格条件满足不代表其他触发、观察时点或入场检查已成立。"
    result.update(distance=_text(distance), state="needed" if distance else "price_satisfied", note=note)
    return result, None


def _threshold_boundary(threshold, operator, grid):
    step, origin = grid
    units = (threshold - origin) / step
    if operator == "gt":
        return origin + (units.to_integral_value(rounding=ROUND_FLOOR) + 1) * step
    if operator == "lt":
        return origin + (units.to_integral_value(rounding=ROUND_CEILING) - 1) * step
    return threshold


def _post_activation_path(rule, trigger, result, quote, grid):
    if trigger["state"] not in {"needed", "price_satisfied"} or not result["effective_entry_range"] or not quote or not grid:
        return None
    threshold = _decimal(trigger["threshold"])
    if threshold is None or trigger["price_basis"] not in {"bid", "ask"}:
        return None
    boundary = _threshold_boundary(threshold, rule["operator"], grid)
    assumption = ""
    if trigger["price_basis"] != result["execution_side"]:
        spread = quote["ask"] - quote["bid"]
        boundary += spread if result["execution_side"] == "ask" else -spread
        assumption = f"按当前点差{_text(spread)}美元保持不变换算，"
    low, high = (Decimal(result["effective_entry_range"][key]) for key in ("low", "high"))
    distance = max(low - boundary, boundary - high, Decimal(0))
    movement = "反弹" if boundary < low else "回踩" if boundary > high else "保持在有效入场范围"
    return f"若在触发边界成立，{assumption}执行侧随后需{movement}{_text(distance)}美元到入场范围；该路径仅为边界情景，不是触发概率或实际成交预测"


def _entry_check_contradiction(plan, result, grid):
    if not grid:
        return None
    bounds = {}
    if result["effective_entry_range"]:
        bounds[("quote_compare", result["execution_side"])] = [
            Decimal(result["effective_entry_range"][key]) for key in ("low", "high")]
    for rule in plan.get("entry_checks") or []:
        kind = rule.get("kind")
        if kind not in {"bar_close", "quote_compare"}:
            continue
        threshold, operator = _decimal(rule.get("price")), rule.get("operator")
        if threshold is None or operator not in {"gt", "gte", "lt", "lte"} or not _on_grid(threshold, grid):
            continue
        basis = rule.get("quote_side") if kind == "quote_compare" else rule.get("timeframe")
        pair = bounds.setdefault((kind, basis), [None, None])
        boundary = _threshold_boundary(threshold, operator, grid)
        if operator in {"gt", "gte"}:
            pair[0] = boundary if pair[0] is None else max(pair[0], boundary)
        else:
            pair[1] = boundary if pair[1] is None else min(pair[1], boundary)
        if all(value is not None for value in pair) and pair[0] > pair[1]:
            return "同时要求的入场检查彼此矛盾或与有效入场范围没有交集；激活条件作为先后步骤单独判断。"
    return None


def assess_scalp(plan, market, preferences, *, activated=False, now=None, max_age_ms=10000):
    with localcontext() as context:
        context.prec = 120
        return _assess_scalp(plan, market, preferences, activated=activated, now=now, max_age_ms=max_age_ms)


def _assess_scalp(plan, market, preferences, *, activated, now, max_age_ms):
    if preferences is None or plan.get("trading_style") != "scalp":
        return None
    market = market or {}
    result = {
        "version": "scalp-suitability/1", "preference_version": preferences.get("version") if isinstance(preferences, dict) else None,
        "state": "compatible", "reasons": [], "codes": [],
        "execution_side": "ask" if plan.get("side") == "long" else "bid" if plan.get("side") == "short" else None,
        "basis_quote_at": (market.get("quote") or {}).get("event_time"),
        "entry_distance": None, "effective_entry_range": None, "target_space": None, "stop_space": None,
        "trigger_distances": [], "required_path": "", "whole_range_geometry": None,
        "entry_boundaries": [], "current_target_space": None, "current_stop_space": None,
    }
    incompatible, unknown = [], []

    def record(code, reason, *, uncertain=False):
        (unknown if uncertain else incompatible).append(code)
        if code not in result["codes"]:
            result["codes"].append(code)
        if reason not in result["reasons"]:
            result["reasons"].append(reason)

    preference_errors = validate_scalp_preferences(preferences)
    if preference_errors:
        result.update(state="unknown", codes=["PREFERENCES_INVALID"], reasons=preference_errors,
                      required_path="冻结的超短线偏好无效，不能据此判定适配或允许入场。")
        return result
    contract = market.get("contract")
    grid = _grid(contract)
    if grid is None:
        record("PRICE_GRID_UNKNOWN", "缺少有效合约价格网格，无法完整核验价格边界。", uncertain=True)
    entry_range = effective_entry_range(plan, contract)
    for code, reason in zip(entry_range["codes"], entry_range["reasons"]):
        record(code, reason, uncertain=entry_range["state"] == "unknown")
    quote, quote_error = _quote(market, now, max_age_ms)
    if quote_error:
        record("QUOTE_UNKNOWN", quote_error, uncertain=True)
    stop, target = _decimal(plan.get("stop_loss")), _decimal(plan.get("take_profit"))
    if stop is None or target is None or stop <= 0 or target <= 0:
        record("PRICE_GEOMETRY_INVALID", "止损或目标价格无效。")
    elif grid and (not _on_grid(stop, grid) or not _on_grid(target, grid)):
        record("PRICE_GRID_INVALID", "止损或目标不符合合约价格网格；保留原价，不自动取整。")
    direction = 1 if plan.get("side") == "long" else -1
    if entry_range["low"] is not None and entry_range["high"] is not None:
        low, high = Decimal(entry_range["low"]), Decimal(entry_range["high"])
        result["effective_entry_range"] = {"low": entry_range["low"], "high": entry_range["high"]}
        if stop is not None and target is not None:
            targets = [(target - value) * direction for value in (low, high)]
            stops = [(value - stop) * direction for value in (low, high)]
            result["target_space"] = {"low": _text(min(targets)), "high": _text(max(targets))}
            result["stop_space"] = {"low": _text(min(stops)), "high": _text(max(stops))}
            result["entry_boundaries"] = [{"entry_price": _text(value), "target_space": _text(target_gap), "stop_space": _text(stop_gap)}
                                          for value, target_gap, stop_gap in zip((low, high), targets, stops)]
            result["whole_range_geometry"] = min(targets) > 0 and min(stops) > 0
            if not result["whole_range_geometry"]:
                record("PRICE_GEOMETRY_INVALID", "允许入场范围中存在止损、入场、目标顺序不成立的价格。")
            if min(targets) < Decimal(preferences["target_min"]):
                record("TARGET_SPACE_TOO_SMALL", f"允许入场范围的最小目标价差{_text(min(targets))}美元，小于本次偏好{preferences['target_min']}美元。")
            if max(targets) > Decimal(preferences["target_max"]):
                record("TARGET_SPACE_TOO_LARGE", f"允许入场范围的最大目标价差{_text(max(targets))}美元，超出本次偏好{preferences['target_max']}美元。")
        if quote and result["execution_side"]:
            current = quote[result["execution_side"]]
            distance = max(low - current, current - high, Decimal(0))
            result["entry_distance"] = _text(distance)
            if distance > Decimal(preferences["max_entry_distance"]):
                movement = "反弹" if current < low else "回落"
                record("ENTRY_DISTANCE_EXCEEDED", f"需要先{movement}{_text(distance)}美元到有效入场区间，超出本次超短线偏好{preferences['max_entry_distance']}美元。")
    if quote and result["execution_side"] and stop is not None and target is not None:
        current = quote[result["execution_side"]]
        result.update(current_target_space=_text((target - current) * direction),
                      current_stop_space=_text((current - stop) * direction))
    contradiction = _entry_check_contradiction(plan, result, grid)
    if contradiction:
        record("ENTRY_CHECK_CONTRADICTION", contradiction)
    path = []
    rules = [("activation", plan.get("activation") or {})] + [
        (f"entry_checks[{index}]", rule) for index, rule in enumerate(plan.get("entry_checks") or [])]
    for role, rule in rules:
        if rule.get("kind") == "immediate":
            continue
        trigger, error = _trigger(rule, role, market, quote, grid, activated, now, max_age_ms)
        label = "激活条件" if role == "activation" else f"入场检查{int(role[13:-1]) + 1}"
        result["trigger_distances"].append(trigger)
        if error:
            record(error, trigger["note"])
        elif trigger["state"] == "unknown":
            record("TRIGGER_DISTANCE_UNKNOWN", f"{label}：{trigger['note']}", uncertain=True)
        elif trigger["distance"] is not None and Decimal(trigger["distance"]) > Decimal(preferences["max_trigger_distance"]):
            record("TRIGGER_DISTANCE_EXCEEDED", f"{label}尚需{trigger['price_basis'].title()}价格位移{trigger['distance']}美元，超出本次触发距离偏好{preferences['max_trigger_distance']}美元。")
        if trigger["state"] == "latched":
            path.append("激活条件已锁存")
        elif rule.get("observation") == "background":
            path.append(f"{label}：{trigger['note']}")
        elif rule.get("kind") == "bar_close":
            path.append(f"等待合法的新{rule.get('timeframe', '')}闭柱确认{label}（当前同侧价格位移{trigger['distance'] or '未知'}美元）")
        else:
            path.append(f"核验{label}（当前价格位移{trigger['distance'] or '未知'}美元）")
        if role == "activation":
            subsequent = _post_activation_path(rule, trigger, result, quote, grid)
            if subsequent:
                path.append(subsequent)
    holding = plan.get("max_holding_minutes")
    if type(holding) is not int or holding < 1:
        record("HOLDING_TIME_UNKNOWN", "缺少明确的成交后最大持仓分钟数。", uncertain=True)
    elif holding > preferences["max_holding_minutes"]:
        record("HOLDING_LIMIT_EXCEEDED", f"成交后最长持仓{holding}分钟，超过本次兼容上限{preferences['max_holding_minutes']}分钟。")
    elif holding > preferences["preferred_holding_minutes"] and not str(plan.get("holding_time_basis") or "").strip():
        record("HOLDING_BASIS_MISSING", f"成交后持仓超过优先研究的{preferences['preferred_holding_minutes']}分钟，缺少延长依据。")
    if result["effective_entry_range"]:
        movement = "回踩" if plan.get("side") == "long" else "反弹"
        stage = "激活后继续等待" + movement if (plan.get("entry") or {}).get("mode") == "limit_retest" else "条件成立后等待执行侧进入有效范围"
        path.append(f"{stage}{result['effective_entry_range']['low']}—{result['effective_entry_range']['high']}；距该范围{result['entry_distance'] or '未知'}美元")
    else:
        path.append("有效入场范围未知或无可行交集，不能据此允许入场")
    path.append("实际入场仍须新的合法观察、当前报价、金额及其他必要检查全部通过；价差适配不代表已触发或可入场")
    result["required_path"] = "；".join(path) + "。"
    result["state"] = "incompatible" if incompatible else "unknown" if unknown else "compatible"
    return result
