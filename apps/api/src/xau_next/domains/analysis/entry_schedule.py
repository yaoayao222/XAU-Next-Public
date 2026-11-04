"""Frozen timing arithmetic and read-only explanations, never a plan repair."""
from copy import deepcopy
from decimal import Decimal

from xau_next.shared.entry_timing import planned_timing, timing_reference
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

VERSION = "entry-timing-reference/1"
MODES = ("market", "stop_breakout", "limit_retest")
PERIODS = (None, "M1", "M5", "M15")

INSTRUCTIONS = (
    "\n# 候选25：先核对具体观察时间，再提交候选\n"
    "entry_timing_reference由本机原入场时间函数计算，所有时刻为UTC；不是交易建议。"
    "style_windows列原cutoff起算的硬上限；cases列各触发方式、入场方式及单项新闭柱检查的最低观察截止。"
    "先选符合真实结构的方式，再找对应时间行；无闭柱触发的quote_compare行也适用于immediate。"
    "表中基准valid_from不晚于estimated_available_at，不能把延迟生效的方案直接套用基准行。"
    "先取effective_from=max(estimated_available_at,valid_from)。bar_close触发从该时刻起找第一合法整点收盘，"
    "恰好落在收盘时刻可计该次，再加一个触发周期，预留两次机会；这不是要求实际等满两根才触发。"
    "额外闭柱检查也按原策略计算；多项检查取较晚时刻，不能相加或忽略。"
    "最低截止=max(触发机会预算完成,检查可用时刻)+post_trigger_reserve_seconds。"
    "市场入场余量10秒、突破60秒、回踩为触发周期且至少60秒。"
    "延后valid_from或检查语义不同必须重算；表中单项检查为new_since_available基准，不能当作任意顺序规则的通行证。"
    "最终entry_deadline须同时不早于最低观察截止、不晚于该模式硬上限，且不晚于exit_deadline及原窗口终点。"
    "例如本包若M5回踩最低截止为15:30，填写15:29仍不合格，不能因两次收盘都在15:29前就漏掉回踩余量。"
    "具体数值以本包UTC表为准，不复制示例时刻。"
    "entry_deadline是停止新入场的时刻，max_holding_minutes是成交后最长持仓，两者独立；"
    "不能机械用预测终点减持仓分钟来替代观察预算。"
    "不得为通过检查随意挪截止、换M1、删除必要保护或编造更快条件。"
    "结构不能在原期限内留足时间时，给该模式no_candidate并解释缺口；预测部分仍按原格式完成。"
    "实际返回可能比预留更晚，表不保证真实可入场；本机会按真实可用时刻继续检查，不重置时效。"
    "不输出自检过程、不增加答案字段，原Schema、身份、预测端点和所有交易校验保持。\n"
)


def build_schedule(request, context):
    base = {"valid_from": request["market_cutoff"], "entry_deadline": request["window_end"],
            "trading_style": "scalp", "entry_checks": [],
            "activation": {"kind": "quote_compare"}, "entry": {"mode": "market"}}
    first = planned_timing(base, request, context)
    reference = context.get("timing_reference") or timing_reference()
    windows = []
    for style in request.get("analysis_preferences", {}).get("enabled_styles", ["scalp", "intraday"]):
        t = planned_timing({**base, "trading_style": style}, request, context)
        windows.append({"trading_style": style, "max_entry_deadline": t["analysis_fresh_until"]})
    cases = []
    for period in PERIODS:
        activation = {"kind": "bar_close", "timeframe": period} if period else {"kind": "quote_compare"}
        for mode in MODES:
            for check in PERIODS:
                checks = [{"kind": "bar_close", "timeframe": check, "observation": "new_since_available"}] if check else []
                t = planned_timing({**base, "activation": activation, "entry": {"mode": mode}, "entry_checks": checks}, request, context)
                cases.append({"activation_kind": activation["kind"], "timeframe": period,
                              "entry_mode": mode, "extra_closed_check": check,
                              "minimum_entry_deadline": t["minimum_entry_deadline"],
                              "required_activation_closes": t["required_activation_closes"],
                              "post_trigger_reserve_seconds": t["post_trigger_reserve_seconds"]})
    return {"version": VERSION, "market_cutoff": request["market_cutoff"], "window_end": request["window_end"],
            "answer_schema_version": request["answer_schema_version"], "policy_version": first["policy_version"],
            "planning_delay_seconds": reference["planning_delay_seconds"], "estimated_available_at": first["available_at"],
            "baseline_effective_from": first["effective_from"], "style_windows": windows, "cases": cases,
            "assumptions": "基准valid_from≤预计可用时刻；extra_closed_check为零或一项new_since_available闭柱检查。延后生效、多项或不同观察角色须按原策略重算，不能静默放宽。",
            "limits": "仅为原策略的计划预算；不延长截止，不选择交易方式，不保证真实返回速度或成交资格。"}


def project_schedule(value):
    if value.get("version") != VERSION:
        raise DomainError("TIMING_REFERENCE_INVALID", "候选25时间参照修订无法识别，已停止外发。", 409)
    def pick(item, names):
        return {k: deepcopy(item[k]) for k in names.split()}
    result = pick(value, "version market_cutoff window_end answer_schema_version policy_version planning_delay_seconds estimated_available_at baseline_effective_from assumptions limits")
    result["style_windows"] = [pick(r, "trading_style max_entry_deadline") for r in value["style_windows"]]
    result["cases"] = [pick(r, "activation_kind timeframe entry_mode extra_closed_check minimum_entry_deadline required_activation_closes post_trigger_reserve_seconds") for r in value["cases"]]
    return result


def explain_entry_timing(answer, request, context):
    """Recalculate frozen plan budgets only; never accept/import/edit an answer."""
    if any(answer.get(k) != request.get(k) for k in ("request_id", "forecast_id", "market_cutoff", "market_snapshot_id")):
        return []
    rows = []
    for index, plan in enumerate(answer.get("plans", [])):
        timing = planned_timing(plan, request, context)
        deadline = parse_utc(plan["entry_deadline"])
        def seconds(at):
            return format(max(Decimal(0), Decimal(str(at.total_seconds()))), "f")
        short = seconds(parse_utc(timing["minimum_entry_deadline"]) - deadline)
        excess = seconds(deadline - parse_utc(timing["analysis_fresh_until"]))
        if Decimal(short) > 0 or Decimal(excess) > 0:
            rows.append({"plan_index": index, "trading_style": plan["trading_style"],
                         "entry_deadline": plan["entry_deadline"],
                         "minimum_entry_deadline": timing["minimum_entry_deadline"],
                         "max_entry_deadline": timing["analysis_fresh_until"],
                         "shortfall_seconds": short, "exceeds_seconds": excess})
    return rows
