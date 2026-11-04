"""Deterministic answer validation against frozen request facts."""
import re
from decimal import Decimal, localcontext

from jsonschema import Draft202012Validator, FormatChecker
from xau_next.shared.condition_observation import rule_check
from xau_next.shared.holding import same_price
from xau_next.shared.entry_timing import planned_timing
from xau_next.shared.scalp import effective_entry_range
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from .experience import validate_experience_application


def failure(code, path, message, status=422):
    return DomainError(code, message, status, [{"path": path, "code": code, "message": message}])


def reference_errors(answer, context):
    """Read-only hints against frozen references; ambiguous matches are never guessed."""
    references = answer.get("evidence_refs") if isinstance(answer, dict) else None
    if not isinstance(references, list):
        return [], False
    allowed = set(context.get("evidence_refs", []))
    fields, suspected = [], False
    for index, value in enumerate(references):
        if not isinstance(value, str) or value in allowed:
            continue
        message = "引用必须来自冻结分析包，不能补造来源。"
        matches = [ref for ref in allowed if ref.replace("-", "") == value]
        if len(matches) == 1:
            suspected = True
            message += "疑似字符丢失（连字符）；合法原值：" + matches[0][:700] + "。未自动替换。"
        fields.append({"path": f"/evidence_refs/{index}", "code": "EVIDENCE_REF_INVALID", "message": message})
        if len(fields) >= 50:
            break
    return fields, suspected


def validate_answer(answer, schema, request, context):
    # A legal decimal string can contain 48 digits. Do not let the ambient 28-digit
    # context round modulo operations or turn an input error into a server crash.
    with localcontext() as decimal_context:
        decimal_context.prec = 128
        _validate_answer(answer, schema, request, context)


def implies(first, second):
    """Only prove contradictions on the same observed price; no prose inference."""
    if first["kind"] != second["kind"] or first["kind"] not in {"bar_close", "quote_compare"}:
        return False
    key = "timeframe" if first["kind"] == "bar_close" else "quote_side"
    if first[key] != second[key]:
        return False
    a, b = Decimal(first["price"]), Decimal(second["price"])
    op, other = first["operator"], second["operator"]
    if op[0] != other[0]:
        return False
    return (a > b if op[0] == "g" else a < b) or (a == b and (op == other or len(op) == 2))


def frozen_match(rule, market, max_age):
    return rule_check(rule, market, market["market_cutoff"], max_age)["matched"]


def _validate_answer(answer, schema, request, context):
    references, suspected = reference_errors(answer, context)
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(answer),
                    key=lambda error: str(list(error.absolute_path)))
    if errors:
        fields = []
        for error in errors[:50]:
            message = "字段不符合回答规范：" + error.message[:850]
            if error.validator == "format" and error.validator_value == "date-time" and isinstance(error.instance, str) and re.match(r"^\d{8}T", error.instance):
                suspected = True
                message += "；疑似字符丢失：日期缺少连字符，未自动修补。"
            fields.append({"path": "/" + "/".join(str(item) for item in error.absolute_path),
                           "code": "SCHEMA_INVALID", "message": message})
        # Reserve space for independent frozen-reference failures even when schema fails.
        fields = fields[:50 - len(references)] + references
        detail = "回答结构不符合规范，请按字段提示修正。"
        if suspected:
            detail += "疑似字符丢失；日期与证据引用已同时检查，原文未修改。"
        raise DomainError("SCHEMA_INVALID", detail, 422, fields)
    if references:
        detail = "证据引用不符合冻结分析包。"
        if suspected:
            detail += "疑似字符丢失；原文未修改。"
        raise DomainError("EVIDENCE_REF_INVALID", detail, 422, references)
    for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        if answer[key] != request[key]:
            raise failure("REQUEST_MISMATCH", "/" + key, "回答身份或截止点与原分析请求不一致。")
    required_version = request.get("answer_schema_version")
    if (required_version in {"1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"} or answer["schema_version"] in {"1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}) and answer["schema_version"] != required_version:
        raise failure("ANSWER_VERSION_REQUIRED", "/schema_version", f"本请求要求{required_version}回答；不能退回旧格式或改变冻结请求格式。")
    market = context["market"]
    if answer["schema_version"] in {"1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
        validate_experience_application(answer, context)
    contract = market.get("contract")
    if not contract or market.get("contract_status", {}).get("state") not in ("available", "stale"):
        raise failure("CONTRACT_UNAVAILABLE", "/plans", "缺少已读取的合约规格，无法校验价格与手数网格。")
    tick = Decimal(contract["tick_size"])
    step = Decimal(contract["volume_step"])
    minimum, maximum = Decimal(contract["volume_min"]), Decimal(contract["volume_max"])
    if tick <= 0 or step <= 0 or minimum <= 0 or maximum < minimum:
        raise failure("CONTRACT_INVALID", "/plans", "合约价格或手数网格无效。")

    def price(value, path):
        amount = Decimal(value)
        if amount <= 0:
            raise failure("PRICE_GEOMETRY_INVALID", path, "价格必须大于零。")
        if amount % tick:
            lower = (amount // tick) * tick
            raise failure("PRICE_GRID_INVALID", path, f"价格不符合步进 {tick}；相邻合法值为 {lower}、{lower + tick}。")
        return amount

    def volume(value, path):
        amount = Decimal(value)
        if amount < minimum or amount > maximum or (amount - minimum) % step:
            raise failure("VOLUME_GRID_INVALID", path, f"手数须在 {minimum} 至 {maximum}，按步进 {step} 取值。")
        return amount

    def price_range(value, path):
        low, high = price(value["low"], path + "/low"), price(value["high"], path + "/high")
        if low > high:
            raise failure("PRICE_GEOMETRY_INVALID", path, "价格区间下界不能高于上界。")
        return low, high

    cutoff, start, end = (parse_utc(request[key]) for key in ("market_cutoff", "window_start", "window_end"))
    holding_review = context.get("holding_review")
    if holding_review and answer["plans"]:
        raise failure("HOLDING_ENTRY_FORBIDDEN", "/plans", "持仓专项重评不能生成新入场方案，也不能替换原方案。")
    guided = answer["schema_version"] in {"1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}
    styles = set()
    from datetime import timedelta
    expected = {minutes: start + timedelta(minutes=minutes) for minutes in request["horizon_minutes"]
                if start + timedelta(minutes=minutes) > cutoff}
    supplied = [horizon["horizon_minutes"] for horizon in answer["horizons"]]
    if len(supplied) != len(set(supplied)) or set(supplied) != set(expected):
        raise failure("TIME_WINDOW_INVALID", "/horizons", "回答必须恰好覆盖原窗口中尚未结束的预测周期。")
    for i, horizon in enumerate(answer["horizons"]):
        path = f"/horizons/{i}"
        if answer["schema_version"] in {"1.6.0", "1.7.0"}:
            refs = horizon["forecast_basis"]["evidence_refs"]
            if not set(refs) <= set(context.get("evidence_refs", [])):
                raise failure("EVIDENCE_REF_INVALID", path + "/forecast_basis/evidence_refs", "逐周期依据只能引用冻结来源。")
            evidence = next((item for item in context.get("forecast_evidence", {}).get("horizons", [])
                if item["horizon_minutes"] == horizon["horizon_minutes"]), None)
            if (not evidence or evidence["state"] == "missing") and horizon["confidence"] is not None:
                raise failure("CONFIDENCE_UNSUPPORTED", path + "/confidence", "当前周期没有可核验的闭柱依据，confidence必须为null；不补造信心。")
        horizon_end = parse_utc(horizon["end_at"])
        if horizon_end != expected[horizon["horizon_minutes"]]:
            raise failure("TIME_WINDOW_INVALID", path + "/end_at", "预测端点必须保持原窗口，不能延长或缩短。")
        price_range(horizon["expected_range"], path + "/expected_range")
        cursor = cutoff
        for j, phase in enumerate(horizon["phases"]):
            phase_start, phase_end = parse_utc(phase["start_at"]), parse_utc(phase["end_at"])
            if phase_start != cursor or not phase_start < phase_end <= horizon_end:
                raise failure("TIME_WINDOW_INVALID", path + f"/phases/{j}", "阶段须从截止点连续覆盖，不能重叠、遗漏或改写过去。")
            cursor = phase_end
        if cursor != horizon_end:
            raise failure("TIME_WINDOW_INVALID", path + "/phases", "阶段未覆盖到原预测端点。")
        for j, turning in enumerate(horizon["turning_points"]):
            if not cutoff <= parse_utc(turning["earliest_at"]) <= parse_utc(turning["latest_at"]) <= horizon_end:
                raise failure("TIME_WINDOW_INVALID", path + f"/turning_points/{j}", "转折时间必须位于本次剩余预测窗口。")
            price_range(turning["price_range"], path + f"/turning_points/{j}/price_range")
    roles, keys = set(), set()
    for i, plan in enumerate(answer["plans"]):
        path = f"/plans/{i}"
        if answer["schema_version"] == "1.7.0":
            rule = plan["holding_exit"]["condition"]
            if rule:
                threshold = price(rule["price"], path + "/holding_exit/condition/price")
                expected_ops = ("lt", "lte") if plan["side"] == "long" else ("gt", "gte")
                if rule["operator"] not in expected_ops or (rule["kind"] == "quote_compare" and
                        rule["quote_side"] != ("bid" if plan["side"] == "long" else "ask")):
                    raise failure("HOLDING_RULE_INVALID", path + "/holding_exit", "结构退出须表达多头向下或空头向上破坏，并使用持仓退出报价侧。")
                zone = plan["entry"].get("zone")
                if zone and ((plan["side"] == "long" and threshold >= Decimal(zone["low"])) or
                             (plan["side"] == "short" and threshold <= Decimal(zone["high"]))):
                    side, boundary, direction = (("多头", zone["low"], "低于") if plan["side"] == "long"
                                                 else ("空头", zone["high"], "高于"))
                    raise failure("HOLDING_RULE_INVALID", path + "/holding_exit",
                        f"{side}入场区间为{zone['low']}—{zone['high']}，成交后结构退出线为{rule['price']}；"
                        f"本版直接检查的退出线必须严格{direction}{boundary}。当前不支持先盈利再启用回撤退出。"
                        "请依据真实结构重新定义；没有额外退出依据时condition填null并说明，原止损、目标和期限保留。"
                        "若必要保护依赖无法表达的分阶段过程，应不给该模式候选；不要随意挪价或删除必要保护。原回答未修改。")
        if guided:
            style = plan["trading_style"]
            if style in styles:
                raise failure("STYLE_DUPLICATE", path + "/trading_style", "同一交易周期最多一个候选方案。")
            styles.add(style)
            enabled = request.get("analysis_preferences", {}).get("enabled_styles", ["scalp", "intraday"])
            if style not in enabled:
                raise failure("STYLE_DISABLED", path + "/trading_style", "该模式未在本请求中启用。")
            holding = plan["max_holding_minutes"]
            if (style == "scalp" and not 1 <= holding <= 30) or (style == "intraday" and not 31 <= holding <= 720):
                raise failure("HOLDING_STYLE_INVALID", path + "/max_holding_minutes", "超短线持仓1至30分钟；日内31至720分钟。")
            if any(plan[role]["kind"] == "manual_observation" for role in ("activation", "invalidation")):
                raise failure("AUTOMATIC_RULE_REQUIRED", path, "自动方案的触发和失效条件须可由系统计算。")
            if plan["entry"]["mode"] != "market" and plan["entry"]["price"] is None:
                raise failure("ENTRY_PRICE_REQUIRED", path + "/entry/price", "回踩或突破方案必须提供精确条件价格。")
            for j, rule in enumerate(plan["entry_checks"]):
                price(rule["price"], path + f"/entry_checks/{j}/price")
                if answer["schema_version"] in {"1.6.0", "1.7.0"}:
                    check_path = path + f"/entry_checks/{j}"
                    max_age = context["settings"]["values"]["quote_max_age_ms"]
                    if rule["observation"] == "background" and frozen_match(rule, market, max_age) is not True:
                        raise failure("ENTRY_BACKGROUND_UNVERIFIED", check_path, "持续背景须在冻结时已经成立且可核验；未成立的新条件应按真实用途定义新事件。")
                    def signature(item):
                        return (item["kind"],item.get("timeframe"),item.get("quote_side"),item.get("operator"),
                                Decimal(item["price"]) if "price" in item else None)
                    if any(signature(rule) == signature(other) for other in plan["entry_checks"][:j]):
                        raise failure("ENTRY_CHECK_DUPLICATE", check_path, "同一价格事实不能以不同说明重复确认；每项须有独立用途。")
                    if rule["observation"] == "new_since_available" and signature(rule) == signature(plan["activation"]):
                        raise failure("ENTRY_CHECK_REDUNDANT", check_path, "激活已锁存同一新事实；必要后续确认须明确激活后观察及用途。")
                    for invalid in [plan["invalidation"], plan.get("post_activation_invalidation")]:
                        if invalid and implies(rule, invalid):
                            raise failure("RULE_CONTRADICTION", check_path, "必要入场检查成立必然同时失效，无法进入READY。")
        if plan["role"] in roles or plan["plan_key"] in keys:
            raise failure("PLAN_IDENTITY_INVALID", path, "每个方案角色及 plan_key 必须唯一。")
        roles.add(plan["role"])
        keys.add(plan["plan_key"])
        valid, entry_end, exit_end = (parse_utc(plan[key]) for key in
                                     ("valid_from", "entry_deadline", "exit_deadline"))
        if not cutoff <= valid < entry_end <= exit_end <= end:
            raise failure("TIME_WINDOW_INVALID", path, "方案须满足 截止点≤生效时间<入场截止≤退出截止≤原窗口终点。")
        if plan["invalidation"]["kind"] == "immediate":
            raise failure("RULE_CONTEXT_INVALID", path + "/invalidation", "失效条件不能使用 immediate。")
        for role in ("activation", "invalidation"):
            if "price" in plan[role]:
                price(plan[role]["price"], path + "/" + role + "/price")
        if answer["schema_version"] in {"1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
            post = plan["post_activation_invalidation"]
            if post:
                price(post["price"], path + "/post_activation_invalidation/price")
            for role, rule in (("invalidation", plan["invalidation"]), ("post_activation_invalidation", post)):
                if rule and implies(plan["activation"], rule):
                    raise failure("RULE_CONTRADICTION", path + "/" + role, "触发成立必然同时失效，同事件失效优先；请按真实结构重新定义候选。")
            matched = frozen_match(plan["invalidation"], market, context.get("settings", {}).get("values", {}).get("quote_max_age_ms", 10000))
            if matched is not False:
                raise failure("BACKGROUND_ALREADY_INVALID" if matched else "BACKGROUND_UNVERIFIED", path + "/invalidation",
                              "等待阶段背景条件在冻结事实中已经失效。" if matched else "缺少冻结事实，无法确认等待阶段背景尚未失效。")
            timing = planned_timing(plan, request, context)
            if entry_end > parse_utc(timing["analysis_fresh_until"]) or timing["state"] != "SUFFICIENT":
                raise failure("ENTRY_WINDOW_INSUFFICIENT", path + "/entry_deadline",
                              "入场截止须在分析时效内，并预留生成导入耗时、两次触发闭柱机会及后续入场时间；无法满足时说明无候选原因。")
        stop, target = price(plan["stop_loss"], path + "/stop_loss"), price(plan["take_profit"], path + "/take_profit")
        entry = plan["entry"]
        frozen_scalp = answer["schema_version"] in {"1.5.0", "1.6.0", "1.7.0"} and plan.get("trading_style") == "scalp"
        bounds = []
        if entry["zone"] is not None:
            bounds.extend(price_range(entry["zone"], path + "/entry/zone"))
        if entry["price"] is not None:
            precise = price(entry["price"], path + "/entry/price")
            if bounds and not bounds[0] <= precise <= bounds[1] and not frozen_scalp:
                raise failure("PRICE_GEOMETRY_INVALID", path + "/entry", "精确入场价必须落在声明的入场区间内。")
            bounds.append(precise)
        geometry_ok = stop < target if plan["side"] == "long" else target < stop
        if bounds:
            geometry_ok = all(stop < value < target for value in bounds) if plan["side"] == "long" else all(
                target < value < stop for value in bounds)
        if frozen_scalp:
            effective = effective_entry_range(plan, contract)
            if effective["state"] == "valid":
                bounds = [Decimal(effective[key]) for key in ("low", "high")]
                geometry_ok = all(stop < value < target for value in bounds) if plan["side"] == "long" else all(target < value < stop for value in bounds)
        if not geometry_ok and not frozen_scalp:
            raise failure("PRICE_GEOMETRY_INVALID", path, "止损、入场和目标的方向或价格顺序不成立。")
        if plan["volume_lots"] is not None:
            volume(plan["volume_lots"], path + "/volume_lots")
    if guided:
        for i, item in enumerate(answer["analysis_basis"]["reassessment_conditions"]):
            price(item["condition"]["price"], f"/analysis_basis/reassessment_conditions/{i}/condition/price")
            if answer["schema_version"] in {"1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"} and request.get("prompt_version") in {"manual-analysis/7", "manual-analysis/8", "manual-analysis/9", "manual-analysis/10", "manual-analysis/11", "manual-analysis/12", "manual-analysis/13", "manual-analysis/14", "manual-analysis/15", "manual-analysis/16"}:
                plan = next((p for p in answer["plans"] if p["trading_style"] == item["trading_style"]), None)
                if plan and any(implies(rule, item["condition"]) for rule in [plan["activation"], *plan["entry_checks"]]):
                    raise failure("REASSESSMENT_CONTRADICTION", f"/analysis_basis/reassessment_conditions/{i}/condition",
                                  "候选正常触发或必要入场条件成立会必然触发重评，候选无法按原语义进入READY；请区分正常等待与真实失效。")
    if answer["schema_version"] in {"1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
        assessments = answer["candidate_assessments"]
        enabled = request.get("analysis_preferences", {}).get("enabled_styles", ["scalp", "intraday"])
        supplied_styles = [item["trading_style"] for item in assessments]
        if len(set(supplied_styles)) != len(supplied_styles) or set(supplied_styles) != set(enabled):
            raise failure("CANDIDATE_COVERAGE_INVALID", "/candidate_assessments", "每个启用模式须恰好说明一次候选或缺少条件，不能遗漏、重复或包含未启用模式。")
        by_style = {plan["trading_style"]: plan for plan in answer["plans"]}
        for i, item in enumerate(assessments):
            plan = by_style.get(item["trading_style"])
            if (item["outcome"] == "candidate" and (not plan or item["plan_key"] != plan["plan_key"])) or (
                    item["outcome"] == "no_candidate" and plan):
                raise failure("CANDIDATE_PLAN_MISMATCH", f"/candidate_assessments/{i}", "候选说明必须与该模式实际方案对应；无候选说明不能同时附带该模式方案。")
    orders = {tuple(order["order"][key] for key in ("account_scope_id", "kind", "broker_id")): order
              for order in context["portfolio"].get("orders", [])}
    seen = set()
    for i, advice in enumerate(answer["order_advice"]):
        path = f"/order_advice/{i}"
        assessment = advice.get("target_assessment")
        if holding_review:
            original_target = (holding_review.get("original_plan") or {}).get("take_profit")
            if not assessment or not same_price(assessment["target_price"], original_target):
                raise failure("HOLDING_TARGET_MISMATCH", path + "/target_assessment", "必须评价冻结原方案的目标，不能把新建议价格冒充原目标。")
            if advice["valid_until"] is None:
                raise failure("HOLDING_VALIDITY_REQUIRED", path + "/valid_until", "持仓重评须给出明确建议有效期。")
            if holding_review.get("deadline") and parse_utc(advice["valid_until"]) > parse_utc(holding_review["deadline"]):
                raise failure("HOLDING_DEADLINE", path + "/valid_until", "重评建议不能延长原持仓期限。")
        if assessment:
            if assessment["target_price"] is not None:
                price(assessment["target_price"], path + "/target_assessment/target_price")
            if assessment["state"] == "supported" and not assessment["supporting_evidence"]:
                raise failure("HOLDING_EVIDENCE_REQUIRED", path + "/target_assessment", "原目标仍获支持须提供具体支持依据。")
            if assessment["state"] in ("weakened", "invalidated") and not assessment["opposing_evidence"]:
                raise failure("HOLDING_EVIDENCE_REQUIRED", path + "/target_assessment", "目标依据减弱或失效须说明反对依据。")
            if assessment["state"] == "unknown" and (not assessment["limitations"] or advice["action"] != "observe"):
                raise failure("HOLDING_UNKNOWN_REQUIRED", path + "/target_assessment", "资料不足须说明缺口并使用observe，不得声称继续持有已被证实。")
            if assessment["target_price"] is None and assessment["state"] != "unknown":
                raise failure("HOLDING_TARGET_UNKNOWN", path + "/target_assessment", "原目标未知时不能判断目标可达性。")
        identity = tuple(advice["order"][key] for key in ("account_scope_id", "kind", "broker_id"))
        if identity not in orders or identity in seen:
            raise failure("ORDER_COVERAGE_INVALID", path + "/order", "订单建议包含未知、跨账户或重复身份。")
        seen.add(identity)
        kind, action = identity[1], advice["action"]
        if (kind == "position" and action == "cancel_pending") or (
            kind == "pending_order" and action in ("hold", "close", "reduce")):
            raise failure("ORDER_ACTION_INVALID", path + "/action", "建议动作不适用于该订单种类。")
        for key in ("proposed_stop_loss", "proposed_take_profit"):
            if advice[key] is not None:
                price(advice[key], path + "/" + key)
        if advice["proposed_volume_lots"] is not None:
            proposed = volume(advice["proposed_volume_lots"], path + "/proposed_volume_lots")
            if action == "reduce" and proposed >= Decimal(orders[identity]["volume_lots"]):
                raise failure("ORDER_ACTION_INVALID", path + "/proposed_volume_lots", "减仓数量必须小于当前持仓数量。")
        elif action == "reduce":
            raise failure("ORDER_ACTION_INVALID", path + "/proposed_volume_lots", "减仓建议必须提供明确数量。")
        if action == "modify_sl_tp" and advice["proposed_stop_loss"] is None and advice["proposed_take_profit"] is None:
            raise failure("ORDER_ACTION_INVALID", path, "调整止损止盈须提供至少一个目标价格。")
        if advice["valid_until"] is not None and not cutoff < parse_utc(advice["valid_until"]) <= end:
            raise failure("TIME_WINDOW_INVALID", path + "/valid_until", "订单建议期限须位于本次剩余窗口。")
    if seen != set(orders):
        raise failure("ORDER_COVERAGE_INVALID", "/order_advice", "原分析包中的每个订单都必须恰好有一条建议。")
