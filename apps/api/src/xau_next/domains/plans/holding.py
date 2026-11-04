"""Two-layer holding advice. Model opinions never replace frozen exit rules."""

from decimal import Decimal, localcontext

from xau_next.shared.condition_observation import rule_check
from xau_next.shared.holding import holding_deadline, holding_fingerprint, rule_key
from xau_next.shared.values import parse_utc


def management(order, plan, market, settings, now, opened_at, *, reassessment_rules=(),
               observed_events=(), latest_review=None, pending_request_id=None):
    p = plan["definition"] if plan else {}
    deadline = holding_deadline(plan, opened_at)
    limit = settings["values"]["quote_max_age_ms"]
    quote = (market or {}).get("quote")
    fresh = bool(quote and market["quote_status"]["state"] == "available" and 0 <= (
        parse_utc(now) - parse_utc(quote["event_time"])).total_seconds() * 1000 <= limit)
    exit_price = quote["bid" if order["side"] == "long" else "ask"] if fresh else None
    target = p.get("take_profit")
    with localcontext() as ctx:
        ctx.prec = 128
        distance = (Decimal(target) - Decimal(exit_price)) * (1 if order["side"] == "long" else -1) if target and exit_price else None
    rules = [r for r in (p.get("invalidation"), p.get("post_activation_invalidation"),
                        (p.get("holding_exit") or {}).get("condition")) if r]
    events = []
    checks, structure_checks, reassessment_checks = [], [], []
    known = {e["rule_key"]: e for e in observed_events}
    for rule in rules:
        saved = known.get("exit:" + rule_key(rule))
        check = rule_check(rule, market, now, limit, after=opened_at) if opened_at else {
            "matched": None, "reasons": ["成交起点未知，无法核验成交后的结构退出条件。"]}
        # A pre-entry or exactly-at-entry candle cannot prove a post-entry change.
        if check.get("observed_at") and opened_at and parse_utc(check["observed_at"]) <= parse_utc(opened_at):
            check = {"matched": False, "reasons": ["等待真实成交之后的新观察。"]}
        if saved or check["matched"] is True:
            checks.append(True)
            if not saved:
                events.append({"rule_key": "exit:" + rule_key(rule), "kind": "exit",
                               "rule": rule, "observed_at": check["observed_at"]})
        else:
            checks.append(check["matched"])
        structure_checks.append({"condition": rule, "matched": True if saved else check["matched"],
            "observed_at": saved["observed_at"] if saved else check.get("observed_at"),
            "latched": bool(saved), "reasons": ["已确认的结构失效记录保留。"] if saved else check.get("reasons", [])})
    structure = "triggered" if True in checks else "unknown" if None in checks else "clear" if rules else "not_defined"
    notes, review_keys = [], []
    missing = False
    for rule in reassessment_rules:
        key = "review:" + rule_key(rule)
        saved = known.get(key)
        check = rule_check(rule, market, now, limit, after=opened_at) if opened_at else {
            "matched": None, "reasons": ["成交起点未知，无法核验成交后的重评条件。"]}
        if check.get("observed_at") and opened_at and parse_utc(check["observed_at"]) <= parse_utc(opened_at):
            check = {"matched": False, "reasons": []}
        if saved or check["matched"] is True:
            event = saved or {"rule_key": key, "kind": "review", "rule": rule, "observed_at": check["observed_at"]}
            # An opinion covers only the already observed events frozen into its request.
            covered = (latest_review or {}).get("covered_events", [])
            if key not in covered:
                review_keys.append(key)
                notes.append("原关联分析中的结构化重评条件已出现；可手动重评当前持仓，原退出规则继续有效。")
            if not saved:
                events.append(event)
        elif check["matched"] is None:
            missing = True
        reassessment_checks.append({"condition": rule, "matched": True if saved else check["matched"],
            "observed_at": saved["observed_at"] if saved else check.get("observed_at"),
            "latched": bool(saved), "reasons": ["原持仓已观察到的重评事实保留。"] if saved else check.get("reasons", [])})
    review_view = None
    if latest_review:
        view = latest_review["view"]
        reasons = []
        if holding_fingerprint(order, opened_at) != latest_review["fingerprint"]:
            reasons.append("持仓数量、关联或止损止盈等关键事实已变化。")
        if not view["valid_until"] or parse_utc(now) >= parse_utc(view["valid_until"]):
            reasons.append("该次目标重评已到有效期限。")
        if not fresh:
            reasons.append("当前报价不可核验。")
        if structure == "triggered":
            reasons.append("原退出条件已触发，重评意见不能覆盖退出依据。")
        if structure == "unknown" or missing:
            reasons.append("当前结构资料不足，不能确认旧意见仍适用。")
        if notes:
            reasons.append("出现该次重评未覆盖的结构变化。")
        review_view = {**view, "current": not reasons, "stale_reasons": reasons}
    return {
        "opened_at": opened_at, "deadline": deadline,
        "original_stop": p.get("stop_loss"), "original_target": target,
        "broker_stop": order.get("stop_loss"), "broker_target": order.get("take_profit"),
        "exit_price": exit_price, "target_distance": format(distance, "f") if distance is not None else None,
        "remaining_seconds": int((parse_utc(deadline) - parse_utc(now)).total_seconds()) if deadline else None,
        "structure_status": structure,
        "structure_reason": {"triggered": "原方案成交后的结构退出条件已确认；价格返回不撤销已确认事实。",
            "unknown": "结构退出所需资料不足，不能确认原结构仍成立。",
            "clear": "尚未观察到原结构退出条件；这不代表保证到达目标。",
            "not_defined": "原方案没有可核验的结构退出条件，未补写历史规则。"}[structure],
        "reassessment_state": "required" if notes else "unknown" if missing else "none",
        "reassessment_reasons": list(dict.fromkeys(notes)) or (["原重评条件所需行情待更新。"] if missing else []),
        "structure_checks": structure_checks, "reassessment_checks": reassessment_checks,
        "pending_request_id": pending_request_id, "latest_review": review_view,
        "_reassessment_keys": review_keys,
    }, events
