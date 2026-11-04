"""Deterministic Decimal sizing and cost assessments; no market I/O."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

D = Decimal


def assessment(state, *reasons):
    return {"state": state, "reasons": list(reasons)}


def grid(value, step, minimum, maximum, path, *, price=False):
    v, s, low, high = map(D, (value, step, minimum, maximum))
    if s <= 0 or low <= 0 or high < low:
        raise DomainError("CONTRACT_INVALID", "合约的价格或手数网格无效。")
    origin = D(0) if price else low
    if v < low or v > high or (v - origin) % s:
        down = max(low, min(high, origin + ((v - origin) / s).to_integral_value(rounding=ROUND_FLOOR) * s))
        up = max(low, min(high, origin + ((v - origin) / s).to_integral_value(rounding=ROUND_CEILING) * s))
        code = "PRICE_GRID_INVALID" if price else "VOLUME_GRID_INVALID"
        message = f"数值不符合合约网格，可选择附近合法值 {down} 或 {up}，程序不会自动取整。"
        raise DomainError(code, message, 422, [{"path": path, "code": code, "message": message}])
    return v


def calculate(record, market, portfolio, settings, command, *, evaluated_at, margin_fact=None):
    with localcontext() as context:
        context.prec = 120
        return _calculate(
            record, market, portfolio, settings, command, evaluated_at=evaluated_at, margin_fact=margin_fact
        )


def decimal_wire(value):
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    if result == "-0":
        result = "0"
    if len(result) > 48:
        raise DomainError(
            "AMOUNT_RANGE_UNSUPPORTED", "计算结果超过当前金额格式的精度范围，请检查输入数量级。"
        )
    return result


def _calculate(record, market, portfolio, settings, command, *, evaluated_at, margin_fact=None):
    p, cfg = record["definition"], settings["values"]
    contract, quote = market["contract"], market["quote"]
    account = portfolio.get("account") if portfolio else None
    currency = account["currency"] if account else (contract or {}).get("profit_currency", "USD")
    missing = []
    value = {
        "entry_price": command["entry_price_override"] or p["entry"]["price"],
        "volume_lots": None,
        "currency": currency,
        **{
            k: None
            for k in (
                "gross_stop_loss",
                "estimated_stop_cost",
                "planned_loss",
                "planned_net_profit",
                "net_rr",
                "margin_required",
            )
        },
        **{
            k: assessment("unknown")
            for k in ("lifecycle_check", "quote_check", "cost_check", "budget_check", "margin_check")
        },
        "missing_fields": missing,
        "sizing_mode": command["sizing_mode"],
    }
    value["lifecycle_check"] = assessment(
        "satisfied" if record["lifecycle"] == "ACTIVE" else "not_satisfied",
        {
            "ACTIVE": "方案条件已满足，仍需用户在 MT5 人工决定。",
            "WAITING_TRIGGER": "等待触发条件。",
            "INVALIDATED": "方案已失效。",
            "EXPIRED": "方案已过期。",
            "SUPERSEDED": "方案已被新版本替代。",
        }[record["lifecycle"]],
    )
    now = parse_utc(evaluated_at)
    if market["quote_status"]["state"] == "available" and quote:
        age = (now - parse_utc(quote["event_time"])).total_seconds() * 1000
        value["quote_check"] = assessment(
            "satisfied" if 0 <= age <= cfg["quote_max_age_ms"] else "not_satisfied",
            "当前报价可用。" if 0 <= age <= cfg["quote_max_age_ms"] else "报价已过期或时间超前，请刷新事实。",
        )
    else:
        value["quote_check"] = assessment("unknown", "当前报价不可用。")
    entry = value["entry_price"]
    if entry is None:
        missing.append("entry_price")
    if contract is None or market["contract_status"]["state"] != "available":
        missing.append("contract")
        return value
    gross_supported = contract["calculation_mode"] == "linear_usd" and contract["profit_currency"] == "USD"
    supported = gross_supported and account is not None and currency == "USD"
    if not supported:
        missing.append("profit_calculation_or_fx")
    if account is None:
        missing.append("account")
    if entry is not None:
        for path, x in (
            ("entry_price", entry),
            ("stop_loss", p["stop_loss"]),
            ("take_profit", p["take_profit"]),
        ):
            grid(x, contract["tick_size"], contract["tick_size"], "1000000000", "/" + path, price=True)
        e, stop, target = map(D, (entry, p["stop_loss"], p["take_profit"]))
        if not (stop < e < target if p["side"] == "long" else target < e < stop):
            raise DomainError("PRICE_ORDER_INVALID", "新入场价格必须位于止损和目标之间，且方向一致。")
    costs = cfg["cost_assumption"]
    cost_fields = ("commission_roundtrip_per_lot", "slippage_reserve_per_lot", "other_cost_per_lot")
    applicable = costs is not None
    if costs:
        applicable = (
            costs["account_scope_id"] == market["account_scope_id"]
            and costs["symbol"] == market["symbol"]
            and costs["currency"] == currency
            and parse_utc(costs["valid_from"]) <= now
            and (costs["valid_until"] is None or now < parse_utc(costs["valid_until"]))
        )
        if not applicable:
            missing.append("cost_assumption_applicability")
        for f in cost_fields:
            if costs[f] is None:
                missing.append("cost_assumption." + f)
            elif D(costs[f]) < 0:
                raise DomainError("COST_INVALID", "预计成本不能为负数。")
    else:
        missing.extend("cost_assumption." + f for f in cost_fields)
    complete_cost = applicable and all(costs[f] is not None for f in cost_fields)
    per_lot_cost = sum((D(costs[f]) for f in cost_fields), D(0)) if complete_cost else None
    value["cost_check"] = assessment(
        "satisfied" if complete_cost else "unknown",
        "预计费用完整（含用户声明的滑点预留）。" if complete_cost else "费用或适用身份/币种/时间待补充。",
    )
    cap = cfg["per_trade_loss_cap"]
    cap_value = D(cap["amount"]) if cap and cap["currency"] == currency else None
    if cap_value is None:
        missing.append("per_trade_loss_cap")
    if command["sizing_mode"] == "risk_budget":
        if command["volume_lots_override"] is not None:
            raise DomainError("SIZING_MODE_CONFLICT", "按预算试算时，手数覆盖值必须为空。")
        if entry is not None and complete_cost and cap_value is not None and supported:
            risk_one = abs(e - stop) * D(contract["contract_size"]) + per_lot_cost
            limit = min(cap_value / risk_one, D(contract["volume_max"]))
            vmin, step = D(contract["volume_min"]), D(contract["volume_step"])
            candidate = vmin + ((limit - vmin) / step).to_integral_value(rounding=ROUND_FLOOR) * step
            if candidate < vmin:
                value["budget_check"] = assessment("not_satisfied", "最小手数的含费止损金额仍超过预算。")
            else:
                value["volume_lots"] = str(candidate)
        else:
            missing.append("risk_budget_sizing_inputs")
    else:
        value["volume_lots"] = (
            command["volume_lots_override"] or p["volume_lots"] or cfg["default_volume_lots"]
        )
    volume = value["volume_lots"]
    if volume is None:
        missing.append("volume_lots")
        return value
    lots = grid(
        volume, contract["volume_step"], contract["volume_min"], contract["volume_max"], "/volume_lots"
    )
    if entry is None or not gross_supported:
        return value
    with localcontext() as ctx:
        ctx.prec = 120
        gross = abs(e - stop) * D(contract["contract_size"]) * lots
        profit = (target - e) * (1 if p["side"] == "long" else -1) * D(contract["contract_size"]) * lots
        value["gross_stop_loss"] = decimal_wire(gross)
        if complete_cost and supported:
            cost = per_lot_cost * lots
            loss, net = gross + cost, profit - cost
            value.update(
                estimated_stop_cost=decimal_wire(cost),
                planned_loss=decimal_wire(loss),
                planned_net_profit=decimal_wire(net),
            )
            if loss > 0 and net > 0:
                value["net_rr"] = decimal_wire((net / loss).quantize(D("0.00000000000000000001")))
            if cap_value is not None:
                fits = loss <= cap_value
                reasons = ["含费止损金额符合预算。" if fits else "含费止损金额超过预算（按未舍入值比较）。"]
                if net <= 0:
                    fits = False
                    reasons.append("目标不足以覆盖成本。")
                elif cfg["min_net_rr"] and net / loss < D(cfg["min_net_rr"]):
                    fits = False
                    reasons.append("净收益风险比低于设置要求。")
                value["budget_check"] = assessment("satisfied" if fits else "not_satisfied", *reasons)
        if margin_fact is not None:
            required = D(margin_fact["amount"]) if margin_fact["currency"] == currency else None
        else:
            required = None
        if required is not None:
            value["margin_required"] = decimal_wire(required)
            account_current = (
                account is not None and portfolio.get("account_status", {}).get("state") == "available"
            )
            value["margin_check"] = assessment(
                "satisfied"
                if account_current and D(account["free_margin"]) >= required
                else "unknown"
                if not account_current
                else "not_satisfied",
                "保证金按只读计算结果核对可用保证金。",
            )
        else:
            missing.append("margin_calculation")
            value["margin_check"] = assessment("unknown", "缺少与本次手数和入场价匹配的真实保证金计算结果。")
    return value
