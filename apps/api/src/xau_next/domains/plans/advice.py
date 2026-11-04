"""Current entry eligibility. A latched trigger alone is never permission to chase."""

from datetime import timedelta
from decimal import Decimal

from xau_next.shared.analysis_policy import analysis_preferences
from xau_next.shared.closed_bars import PERIODS
from xau_next.shared.condition_observation import rule_check
from xau_next.shared.decision_reason import stopping_reason
from xau_next.shared.errors import DomainError
from xau_next.shared.scalp import assess_scalp
from xau_next.shared.values import parse_utc

from .lifecycle import advance
from .calculation import calculate
from .holding import management

STYLE_LABEL = {"scalp": "超短线", "intraday": "日内"}


def utc(value):
    return value.isoformat().replace("+00:00", "Z")


def fresh_status(status, now, max_age_ms):
    if not status or status.get("state") != "available":
        return False
    observed = status.get("observed_at") or status.get("captured_at")
    return bool(observed) and 0 <= (parse_utc(now) - parse_utc(observed)).total_seconds() * 1000 <= max_age_ms


def rule_now(rule, market, now, max_age_ms, *, after=None):
    return rule_check(rule, market, now, max_age_ms, after=after)["matched"]


def entry_advice(record, market, portfolio, settings, evaluation, refresh, *, now, usage=None):
    p = record["definition"]
    style = p.get("trading_style")
    quote = market.get("quote") if market else None
    entry = quote["ask" if p["side"] == "long" else "bid"] if quote else None
    result = {
        "plan_id": record["plan_id"], "version_id": record["version_id"], "trading_style": style,
        "state": "BLOCKED", "side": p["side"], "title": "暂时不能判断入场", "reasons": [],
        "evaluated_at": now, "market_snapshot_id": market["snapshot_id"] if market else None,
        "quote_at": quote["event_time"] if quote else None, "valid_until": None,
        "entry_price": entry, "allowed_zone": p["entry"]["zone"], "stop_loss": p["stop_loss"],
        "take_profit": p["take_profit"], "volume_lots": (evaluation or {}).get("volume_lots"),
        "entry_deadline": p["entry_deadline"], "max_holding_minutes": p["max_holding_minutes"],
        "evaluation_id": (evaluation or {}).get("evaluation_id"),
    }
    suitability = assess_scalp(p, market, record.get("scalp_preferences"),
        activated=record["lifecycle"] == "ACTIVE", now=now, max_age_ms=settings["values"]["quote_max_age_ms"])
    if suitability:
        result["scalp_suitability"] = suitability

    def finish(state, title, *reasons):
        result.update(state=state, title=STYLE_LABEL.get(style, "历史方案") + " · " + title,
                      reasons=list(reasons))
        return result

    def stop_decision(state, title, code, at, *reasons, condition=None, deadline=None):
        result["stopping_reason"] = stopping_reason(code, at, condition=condition, deadline=deadline)
        return finish(state, title, *reasons)

    if usage and usage["state"] == "USED":
        return finish("USED", "方案已使用，不再入场",
                      "已确认该方案关联真实持仓或成交；平仓、取消关联及重启均不恢复入场资格。已有持仓继续按原方案管理。")
    if style is None or p.get("entry_checks") is None:
        return finish("BLOCKED", "需要新版分析", "旧回答没有明确的周期和自动入场条件，原文与历史仍保留。")
    if style not in analysis_preferences(settings["values"])["enabled_styles"]:
        return finish("OBSERVE", "该模式未启用", "开启该模式并取得相应分析后再判断。")
    current = parse_utc(now)
    plan = advance(record, market, now)
    if suitability and plan["lifecycle"] == "ACTIVE" and record["lifecycle"] != "ACTIVE":
        suitability = assess_scalp(p, market, record["scalp_preferences"], activated=True,
            now=now, max_age_ms=settings["values"]["quote_max_age_ms"])
        result["scalp_suitability"] = suitability
    if plan["lifecycle"] in {"INVALIDATED", "SUPERSEDED"}:
        if plan["lifecycle"] == "SUPERSEDED":
            return stop_decision("INVALIDATED", "已有新版本，原候选停止入场", "SUPERSEDED", plan["lifecycle_at"], "原方案已被新版本替代，已有持仓继续按关联方案管理。")
        post = plan.get("invalidation_phase") == "post_activation"
        return stop_decision("INVALIDATED", "激活后结构已失效" if post else "候选背景已失效",
                    "POST_ACTIVATION_INVALIDATED" if post else "BACKGROUND_INVALIDATED", plan["lifecycle_at"],
                    "原方案的失效条件已成立，停止新入场。", condition=p["post_activation_invalidation"] if post else p["invalidation"])
    if current >= parse_utc(p["entry_deadline"]) or plan["lifecycle"] == "EXPIRED":
        return stop_decision("EXPIRED", "候选入场期限已到", "ENTRY_DEADLINE", p["entry_deadline"],
                    "具体候选的入场期限已到；不会从导入或报价刷新时重新计时。", deadline=p["entry_deadline"])
    if refresh and refresh["state"] == "STALE":
        return stop_decision("REFRESH_REQUIRED", "分析时效已到，停止新入场", "ANALYSIS_EXPIRED", refresh["fresh_until"],
                    "原行情判断已超过支持新入场的时效；已有持仓仍保留原退出依据。", deadline=refresh["fresh_until"])
    timing = record.get("entry_timing")
    if timing and timing["enforced"] and timing["state"] == "INSUFFICIENT":
        return stop_decision("REFRESH_REQUIRED", "导入后观察时间不足", "OBSERVATION_WINDOW_SHORT", record["available_at"],
                    *timing["reasons"], deadline=timing["effective_entry_deadline"])
    if not refresh or refresh["state"] in {"REASSESS", "MISSING"}:
        if refresh and refresh.get("stopping_reason"):
            result["stopping_reason"] = refresh["stopping_reason"]
            return finish("REFRESH_REQUIRED", "重评条件已确认，需更新判断", *refresh["reasons"])
        return finish("REFRESH_REQUIRED", "需要重新分析", *((refresh or {}).get("reasons") or ["没有有效的当前分析。 "]))
    if usage and usage["state"] == "UNKNOWN":
        return finish("BLOCKED", "成交归属待核对",
                      "存在尚未明确归属的成交事实或历史关联证据不完整，无法确认该方案未使用；请核对成交与方案关联。")
    if refresh and refresh["state"] == "WAITING_DATA":
        return finish("BLOCKED", "等待行情资料恢复", *refresh["reasons"])
    if current < max(parse_utc(record["available_at"]), parse_utc(p["valid_from"])):
        return finish("WAITING", "尚未到生效时间", "等待方案真正可用后再观察条件。")
    if any(p[role]["kind"] == "manual_observation" for role in ("activation", "invalidation")):
        return finish("BLOCKED", "条件无法自动判断", "需要模型将自然语言改成明确的行情条件。")
    max_age = settings["values"]["quote_max_age_ms"]
    if not quote or market["quote_status"]["state"] != "available":
        return finish("BLOCKED", "报价不可用", "等待可信的新报价，不能沿用旧买入提示。")
    at = parse_utc(quote["event_time"])
    if at > parse_utc(market["market_cutoff"]) or not 0 <= (current - at).total_seconds() * 1000 <= max_age:
        return finish("BLOCKED", "报价已过时", "需要新鲜的 Bid/Ask 后重新判断。")
    result["valid_until"] = utc(min(at + timedelta(milliseconds=max_age), parse_utc(p["entry_deadline"]),
                                    parse_utc(refresh["fresh_until"])))
    if current >= parse_utc(result["valid_until"]):
        return finish("BLOCKED", "本次报价判断已到期", "等待新的报价后重新核验。")
    if suitability:
        suitability["range_net_check"] = range_net_check(record, market, portfolio, settings, evaluation, suitability, now)
        if suitability["state"] == "incompatible":
            return finish("BLOCKED", "不符合本次超短线偏好", *suitability["reasons"])
        if suitability["state"] == "unknown":
            return finish("BLOCKED", "超短线适配资料不足", *suitability["reasons"])
        if suitability["range_net_check"]["state"] != "satisfied":
            return finish("BLOCKED", "允许区间金额约束未满足", *suitability["range_net_check"]["reasons"])
    if not portfolio or portfolio["account_scope_id"] != market["account_scope_id"]:
        return finish("BLOCKED", "账户事实不匹配", "行情与账户必须属于同一来源。")
    if not all(fresh_status(portfolio.get(k), now, max_age) for k in ("account_status", "orders_status")):
        return finish("BLOCKED", "持仓或账户事实待更新", "读取失败不等于空仓，暂不发出新的入场通知。")
    if any(o["symbol"] == market["symbol"] for o in portfolio["orders"]):
        return finish("BLOCKED", "已有持仓或挂单", "默认不叠加入场；继续管理已有交易。")
    rules = [p["activation"], p["invalidation"]]
    if plan["lifecycle"] == "ACTIVE" and p.get("post_activation_invalidation"):
        rules.append(p["post_activation_invalidation"])
    for rule in rules:
        if rule["kind"] == "bar_close" and rule_now(rule, market, now, max_age) is None:
            return finish("BLOCKED", "执行周期行情不完整", "所需闭柱缺失、过时或来源不完整。")
    if plan["lifecycle"] != "ACTIVE":
        return finish("WAITING", "等待入场条件", "结构化触发条件尚未确认。")
    observed = plan.get("activation_observed_at")
    if not observed:
        return finish("BLOCKED", "触发观察时间缺失", "不把后来补到的行情当作当时可用的买入机会。")
    trigger_at = parse_utc(plan["lifecycle_at"])
    tolerance = PERIODS.get(p["activation"].get("timeframe"), 0) + max_age / 1000
    if (parse_utc(observed) - trigger_at).total_seconds() > tolerance:
        return stop_decision("MISSED", "触发到达过晚，停止追入", "TRIGGER_DELAY", observed,
                    "触发事实未及时可用，需要重新分析。", condition=p["activation"])
    if at < max(trigger_at, parse_utc(record["available_at"]), parse_utc(p["valid_from"])):
        return finish("WAITING", "等待确认后的报价", "不使用触发或导入之前的价格完成入场。")
    zone = p["entry"]["zone"]
    if zone is None:
        return finish("BLOCKED", "缺少允许入场范围", "需要新的明确方案，不能自动取区间中点。")
    price, low, high = Decimal(entry), Decimal(zone["low"]), Decimal(zone["high"])
    stop, target = Decimal(p["stop_loss"]), Decimal(p["take_profit"])
    if not (stop < price < target if p["side"] == "long" else target < price < stop):
        return stop_decision("MISSED", "当前价已越过止损或目标", "PRICE_GEOMETRY", quote["event_time"],
                    f"执行价{entry}，原止损{p['stop_loss']}、目标{p['take_profit']}；入场价格结构已不成立。")
    mode, exact = p["entry"]["mode"], p["entry"]["price"]
    if mode != "market" and exact is None:
        return finish("BLOCKED", "缺少精确条件价格", "回踩或突破方案需要明确的条件价格。")
    if price > high if p["side"] == "long" else price < low:
        if mode == "limit_retest":
            return finish("WAITING", "触发已确认，等待回踩",
                          "执行侧价格尚未回到允许区间；继续等待回踩限价，并持续检查失效条件和入场期限。")
        return stop_decision("MISSED", "当前价格不适合追入", "CHASE_LIMIT", quote["event_time"],
                    f"执行价{entry}已越过允许追入范围{zone['low']}—{zone['high']}。")
    if not low <= price <= high:
        return finish("WAITING", "等待进入允许价格范围", "当前执行侧价格尚未进入入场区间。")
    if mode != "market":
        threshold = Decimal(exact)
        matched = ((price <= threshold if p["side"] == "long" else price >= threshold)
                   if mode == "limit_retest" else
                   (price >= threshold if p["side"] == "long" else price <= threshold))
        if not matched:
            return finish("WAITING", "触发已确认，等待回踩" if mode == "limit_retest" else "等待突破价",
                          "触发确认不代表后续入场价格已经出现，当前仍未达到精确条件价格。")
    for rule in p["entry_checks"]:
        boundary = record["available_at"]
        if "observation" in rule:
            boundary = utc(max(parse_utc(boundary), parse_utc(p["valid_from"])))
        checked = rule_check(rule, market, now, max_age, after=boundary,
                             activation_at=plan["lifecycle_at"] if plan["lifecycle"] == "ACTIVE" else None)
        matched = checked["matched"]
        if matched is None:
            return finish("BLOCKED", "入场检查资料不足", *checked["reasons"])
        if not matched:
            return finish("WAITING", "等待全部入场检查成立", *(checked["reasons"] or [rule.get("purpose", "当前仍有必要条件未满足。")]))
    if (not evaluation or evaluation["market_snapshot_id"] != market["snapshot_id"]
            or evaluation["portfolio_snapshot_id"] != portfolio["snapshot_id"]
            or evaluation["settings_revision"] != settings["revision"]
            or evaluation["entry_price"] is None or Decimal(evaluation["entry_price"]) != price):
        return finish("BLOCKED", "等待当前价金额核验", "旧入场价格的金额不能替代当前价格的判断。")
    failed = [key for key in ("quote_check", "cost_check", "budget_check", "margin_check")
              if evaluation[key]["state"] != "satisfied"]
    if failed:
        reasons = [reason for key in failed for reason in evaluation[key]["reasons"]]
        return finish("BLOCKED", "当前入场约束未满足", *(reasons or ["金额和约束尚未完整核验。"]))
    return finish("READY", "当前可以买入" if p["side"] == "long" else "当前可以卖出",
                  "触发、当前执行价格、全部入场检查和金额约束均已满足；在 MT5 人工操作。")


def range_net_check(record, market, portfolio, settings, evaluation, suitability, now):
    result = {"state": "unknown", "reasons": [], "boundary_results": []}
    bounds = suitability.get("effective_entry_range")
    if not bounds or not suitability.get("whole_range_geometry"):
        result["reasons"] = ["有效入场区间或价格结构不完整，无法核验整个区间金额。"]
        return result
    for price in dict.fromkeys((bounds["low"], bounds["high"])):
        try:
            value = calculate(record, market, portfolio, settings,
                {"entry_price_override": price, "volume_lots_override": (evaluation or {}).get("volume_lots"),
                 "sizing_mode": "fixed_volume"}, evaluated_at=now)
            checks = [value[key]["state"] for key in ("cost_check", "budget_check")]
            state = "not_satisfied" if "not_satisfied" in checks else "unknown" if "unknown" in checks else "satisfied"
            result["boundary_results"].append({"entry_price": price,
                **{key: value[key] for key in ("net_rr", "planned_loss", "planned_net_profit")}, "state": state})
            if state != "satisfied":
                result["reasons"].append("入场边界" + price + "：" + "；".join(
                    reason for key in ("cost_check", "budget_check") if value[key]["state"] != "satisfied" for reason in value[key]["reasons"]))
        except DomainError as error:
            result["reasons"].append("入场边界" + price + "：" + str(error))
    states = [value["state"] for value in result["boundary_results"]]
    result["state"] = ("not_satisfied" if "not_satisfied" in states else "satisfied"
        if states and all(state == "satisfied" for state in states) and not result["reasons"] else "unknown")
    if result["state"] == "satisfied":
        result["reasons"] = ["整个有效入场区间符合原费用、单笔风险预算和最低净盈亏比；当前价保证金另行核验。"]
    return result


def position_advice(order, plan, market, settings, *, now, opened_at=None, reassessment_rules=(),
                    observed_events=(), latest_review=None, pending_request_id=None):
    result = {"order": order["order"], "plan_id": order.get("plan_id"), "state": "HOLD",
              "reason_code": "WITHIN_RULES", "title": "继续观察已有持仓", "reasons": [],
              "evaluated_at": now, "valid_until": None}
    if order["order"]["kind"] != "position":
        result.update(state="UNKNOWN", reason_code="PENDING_ORDER", title="已有挂单，暂停叠加入场",
                      reasons=["挂单不是已成交持仓，不计算持仓时长。"])
        return result
    details, events = management(order, plan, market, settings, now, opened_at,
        reassessment_rules=reassessment_rules, observed_events=observed_events,
        latest_review=latest_review, pending_request_id=pending_request_id)
    result["management"] = details
    result["_holding_events"] = events
    result["_holding_review_keys"] = details.pop("_reassessment_keys")
    p = plan["definition"] if plan else None
    if details["exit_price"] is not None:
        result["valid_until"] = utc(parse_utc(market["quote"]["event_time"]) + timedelta(milliseconds=settings["values"]["quote_max_age_ms"]))
        price = Decimal(details["exit_price"])
        stop = p["stop_loss"] if p else order["stop_loss"]
        target = p["take_profit"] if p else order["take_profit"]
        if stop and (price <= Decimal(stop) if order["side"] == "long" else price >= Decimal(stop)):
            result.update(state="EXIT", reason_code="STOP_REACHED", title="已到原止损条件，建议人工退出",
                          reasons=["这是退出建议，实际成交以 MT5 后续读取为准。"])
            return result
        if target and (price >= Decimal(target) if order["side"] == "long" else price <= Decimal(target)):
            result.update(state="EXIT", reason_code="TARGET_REACHED", title="已到原目标条件，建议人工退出",
                          reasons=["这是退出建议，实际成交以 MT5 后续读取为准。"])
            return result
    # Time and confirmed structure do not wait for a model or a new quote.
    if details["deadline"] and parse_utc(now) >= parse_utc(details["deadline"]):
        code = "HOLDING_LIMIT" if opened_at and p["max_holding_minutes"] and parse_utc(now) >= (
            parse_utc(opened_at) + timedelta(minutes=p["max_holding_minutes"])) else "EXIT_DEADLINE"
        result.update(state="EXIT", reason_code=code, title="原持仓时间限制已到，建议人工退出",
                      reasons=["最长持仓从真实成交起算；新分析和重新关联不会重置计时。"],
                      valid_until=utc(parse_utc(now) + timedelta(seconds=10)))
        return result
    if details["structure_status"] == "triggered":
        result.update(state="EXIT", reason_code="THESIS_INVALIDATED", title="原持仓结构已失效，建议人工退出",
                      reasons=[details["structure_reason"], "这是退出建议，实际成交以 MT5 后续读取为准。"],
                      valid_until=utc(parse_utc(now) + timedelta(seconds=10)))
        return result
    quote = market.get("quote") if market else None
    max_age = settings["values"]["quote_max_age_ms"]
    if not quote or market["quote_status"]["state"] != "available" or not 0 <= (
        parse_utc(now) - parse_utc(quote["event_time"])
    ).total_seconds() * 1000 <= max_age:
        result.update(state="UNKNOWN", reason_code="QUOTE_UNAVAILABLE", title="持仓管理报价待更新",
                      reasons=["无法用旧报价判断当前退出价格。"])
        return result
    result["valid_until"] = utc(parse_utc(quote["event_time"]) + timedelta(milliseconds=max_age))
    stop = p["stop_loss"] if p else order["stop_loss"]
    target = p["take_profit"] if p else order["take_profit"]
    price = Decimal(quote["bid" if order["side"] == "long" else "ask"])
    reason = None
    if stop and (price <= Decimal(stop) if order["side"] == "long" else price >= Decimal(stop)):
        reason = ("STOP_REACHED", "已到原止损条件，建议人工退出")
    elif target and (price >= Decimal(target) if order["side"] == "long" else price <= Decimal(target)):
        reason = ("TARGET_REACHED", "已到原目标条件，建议人工退出")
    elif p and parse_utc(now) >= parse_utc(p["exit_deadline"]):
        reason = ("EXIT_DEADLINE", "原方案退出期限已到，建议人工退出")
    elif p and opened_at and p["max_holding_minutes"] and parse_utc(now) >= parse_utc(opened_at) + timedelta(minutes=p["max_holding_minutes"]):
        reason = ("HOLDING_LIMIT", "最长持仓时间已到，建议人工退出")
    if reason:
        result.update(state="EXIT", reason_code=reason[0], title=reason[1],
                      reasons=["这是退出建议，实际成交以 MT5 后续读取为准。"])
    elif details["structure_status"] == "unknown":
        result.update(state="UNKNOWN", reason_code="STRUCTURE_UNAVAILABLE", title="持仓结构资料待更新",
                      reasons=[details["structure_reason"]])
    elif not plan:
        result["reasons"] = ["没有可靠方案关联，只检查券商事实中的止损和目标，不猜测原方案。"]
    elif not opened_at:
        result["reasons"] = ["成交起点未知，最长持仓时长暂不可核验；其它明确退出条件继续检查。"]
    return result
