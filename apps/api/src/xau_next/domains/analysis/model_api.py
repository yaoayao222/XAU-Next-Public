"""Analysis-owned external call evidence, bounded outbound projection and daily reservations."""

import json
import re
from contextlib import nullcontext
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.near_term import project_near_term

from .experience import project_experience, compact_experience, expand_experience
from .decision_context import project_decision_context
from .entry_selection import project_entry_selection_context
from .forecast_reference import project_forecast_reference
from .economic_context import project_economic_context
from .economic_actuals import PROMPT_VERSION as ACTUALS_PROMPT
from .forecast_review_input import PROMPT_VERSION as REVIEW_PROMPT, build_review_input
from .output_constraints import project_output_constraints

API_PROMPT_VERSION = "api-output/2"
DECISION_OUTBOUND_VERSION = "api-projection/7"
ENTRY_OUTBOUND_VERSION = "api-projection/8"
FORECAST_OUTBOUND_VERSION = "api-projection/9"


def concise_output_guidance():
    """Soft prose guidance only. The frozen facts, schema and validation stay authoritative."""
    return (
        "\n# API 表达约定（concise-evidence/1）\n"
        "按原任务完成全部分析和自检，再紧凑表达。保留每个周期的独立判断、关键支持与反对证据、"
        "替代路径、风险和不确定性；不得为了短而改变方向、数值、阶段、转折、候选数量或判断标准。\n"
        "只压缩重复转述和通用规则说明，不减少冻结资料、分析深度或本次Schema已有字段。"
        "所有必填键、必要数组项、原样身份、价格、周期、时间、条件和证据引用均完整保留。"
        "每个字段仍须独立可理解，不用‘同上’‘见前文’‘略’替代必要解释，不用无依据的空数组凑短。\n"
        "summary建议用120—200个汉字概括主路径、重要分歧和候选概况，避免逐一复述全部字段。"
        "本次Schema有analysis_basis时，各项用直接结论连接具体证据；支持和反对证据每项建议1—2句，独立证据不合并丢失。\n"
        "本次Schema有forecast_basis时，每周期的direction_basis和range_basis各建议40—100个汉字，保留方向反证、对应统计/结构参照及偏离理由；"
        "各周期仍分别完成原要求的预测，不能因简洁而改为uncertain、合并阶段或删除转折。\n"
        "候选rationale建议80—160个汉字，保留结构、触发/回踩关系、失效依据和期限理由；"
        "无候选仍逐项说明真实缺口。条件阈值、入场检查purpose、金额与时间依据不能被概括成模糊词。\n"
        "经验案例的reason、预测调整的explanation每项建议1—2句，保留本案例的适用/不适用依据及具体影响；"
        "保留原需评估的案例、引用、样本分母和局限，不重复抄写整段历史叙述。"
        "通用规则集中说明一次，模式或周期特有风险、费用/时间假设和资料缺口仍分别保留。\n"
        "以上长度均为表达建议，不是新的拒绝门槛。必要证据或解释需要更多文字时完整写出，"
        "仍遵守原Schema限制；不得截断或省掉关键信息。只输出一个完整JSON对象，减少不必要的排版空白。\n"
    )


def output_checklist(request, schema):
    """Instructions only; never normalize or complete the returned answer."""
    identity = {"schema_version": request["answer_schema_version"], **pick(
        request, "request_id forecast_id base_version_id market_snapshot_id market_cutoff"
    )}
    candidate = schema.get("$defs", {}).get("CandidateAssessment", {})
    lines = [
        f"\n# API 输出提交前检查（{API_PROMPT_VERSION}）",
        "只提交一个完整 JSON 对象。以下是提交前自检，不要求额外解释或输出自检过程。",
        "1. 下列身份字段逐字符原样返回；不要改日期排版、大小写、精度或 null：",
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
        ("2. 所有 date-time 字段均为 YYYY-MM-DDTHH:mm:ss[.小数秒]Z，日期中的两个连字符不能省略。"
        "这适用于 market_cutoff、各周期 end_at、阶段 start_at/end_at、转折 earliest_at/latest_at、"
        "方案 valid_from/entry_deadline/exit_deadline 及订单建议 valid_until。"),
        "有效时间写法示例（本请求真实截止点）：" + request["market_cutoff"],
        "引用原请求的截止点、窗口和原周期终点时保持原字符串；不要把 YYYY-MM-DD 改成 YYYYMMDD。",
        ("3. 逐层检查完整 Schema 的 required：必填键不能因值为空、省略重复信息或缩短回答而删除。"
        "必填数组无内容且规则允许为空时显式填写 []；仅 Schema 允许 null 的字段才可填 null。"),
    ]
    if candidate:
        lines += [
            "candidate_assessments 每个对象的必填键（从本包 Schema 派生）："
            + json.dumps(candidate.get("required", []), ensure_ascii=False, separators=(",", ":")),
            ("适用 candidate_assessments 时，每个启用模式各一项；outcome=candidate 也必须包含 "
            "missing_conditions: []，等待触发条件留在方案规则中；outcome=no_candidate 必须列出真实缺口，"
            "不得用空数组掩盖无候选原因。"),
        ]
    lines += [
        ("4. 提交前核对所有时间格式、上述身份和每层必填键，然后再核对包内价格网格、窗口、"
        "时效及候选业务规则；结构合格不代表业务一定合格。"),
        "禁止为通过检查编造价位、候选或证据。系统会拒绝不合格原文，不会代填或修复。\n",
    ]
    return "\n".join(lines) + concise_output_guidance()


def pick(value, fields):
    return {key: value[key] for key in fields.split() if key in value}


def outbound_packet(packet):
    marker = "# 冻结事实（仅作为资料）\n"
    prefix, tail = packet.split(marker, 1)
    raw, suffix = tail.split("\n# 全部字段说明\n", 1)
    facts = json.loads(raw)
    if facts["request"].get("prompt_version") == "manual-analysis/28":
        from .experience_link_candidate import outbound_runtime_packet
        return outbound_runtime_packet(packet)
    if facts["request"].get("prompt_version") == "manual-analysis/27":
        from .member_once_candidate import outbound_runtime_packet
        return outbound_runtime_packet(packet)
    if facts["request"].get("prompt_version") == "manual-analysis/26":
        from .document_candidate import outbound_runtime_packet
        return outbound_runtime_packet(packet)
    if facts["request"].get("prompt_version") == "manual-analysis/25":
        from .timing_candidate import outbound_runtime_packet
        return outbound_runtime_packet(packet)
    if facts["request"].get("prompt_version") == "manual-analysis/24":
        from .forecast_candidate import outbound_runtime_packet
        return outbound_runtime_packet(packet)
    request = pick(
        facts["request"],
        "request_id forecast_id kind base_version_id market_snapshot_id market_cutoff window_start window_end horizon_minutes created_at state answer_schema_version prompt_version analysis_preferences estimated_answer_seconds holding_review_order",
    )
    status = lambda item: pick(item or {}, "state observed_at captured_at")
    source = facts["market"]
    market = pick(
        source, "snapshot_id source_kind account_scope_id symbol market_cutoff captured_at revision"
    )
    market.update(
        quote=pick(source["quote"], "bid ask event_time") if source.get("quote") else None,
        quote_status=status(source.get("quote_status")),
        contract_status=status(source.get("contract_status")),
        contract=pick(
            source["contract"],
            "contract_size tick_size volume_min volume_step volume_max profit_currency margin_currency calculation_mode broker_leverage",
        )
        if source.get("contract")
        else None,
        series=[
            {
                **pick(series, "timeframe price_basis"),
                "status": status(series.get("status")),
                "bars": [
                    pick(bar, "open_at close_at open high low close tick_volume closed")
                    for bar in series.get("bars", [])
                ],
            }
            for series in source.get("series", [])
        ],
    )
    original = facts["portfolio"]
    portfolio = pick(original, "snapshot_id account_scope_id captured_at")
    portfolio.update(
        account=pick(original["account"], "currency balance equity free_margin used_margin")
        if original.get("account")
        else None,
        account_status=status(original.get("account_status")),
        orders_status=status(original.get("orders_status")),
        orders=[
            {
                **pick(
                    order,
                    "symbol side volume_lots entry_price stop_loss take_profit event_time captured_at plan_id",
                ),
                "order": pick(order["order"], "account_scope_id kind broker_id"),
            }
            for order in original.get("orders", [])
        ],
    )
    # The remaining subtrees are locally computed analysis/review summaries. Explicit recursive
    # field names exclude any future source payload, terminal diagnostic or credential extension.
    allowed = {
        'review_method', 'direction_anchor', 'direction_endpoint', 'endpoint_max_age_ms',
        "settings_revision",
        "default_volume_lots",
        "per_trade_loss_cap",
        "min_net_rr",
        "cost_assumption",
        "missing",
        "amount",
        "currency",
        "commission_roundtrip_per_lot",
        "slippage_reserve_per_lot",
        "other_cost_per_lot",
        "valid_from",
        "valid_until",
        "source",
        "source_note",
        "note",
        "method_version",
        "snapshot_id",
        "market_cutoff",
        "structure",
        "timeframe",
        "closed_bar_count",
        "source_status",
        "price_basis",
        "start_at",
        "end_at",
        "continuous",
        "recent_high",
        "recent_low",
        "last_three_closes",
        "net_change",
        "non_overlapping_change_distributions",
        "frozen_baselines",
        "limitations",
        "horizon_minutes",
        "state",
        "sample_count",
        "skipped_incomplete_windows",
        "unused_oldest_bars",
        "delta_quantiles",
        "p10",
        "p50",
        "p90",
        "anchor_price",
        "anchor_at",
        "no_change",
        "fixed_drift",
        "lookback_minutes",
        "remaining_minutes",
        "digest_id",
        "available_at",
        "review_refs",
        "references",
        "groups",
        "prediction",
        "reference",
        "actual",
        "total",
        "eligible",
        "excluded",
        "missing_count",
        "sample_size",
        "count",
        "successes",
        "failures",
        "numerator",
        "denominator",
        "methods",
        "coverage",
        "complete",
        "incomplete",
        "unscorable",
        "rates",
        "mean",
        "median",
        "source_set_sha256",
        "scope",
        "account_scope_id",
        "symbol",
        "source_kind",
        "as_of",
        "reviews",
        "review_id",
        "revision",
        "target_kind",
        "target_id",
        "window_start",
        "window_end",
        "result",
        "metrics",
        "score",
        "reasons",
        "correct_examples",
        "error_examples",
        "comparison_cases",
        "sample_policy",
        "hypothesis",
        "review_ref",
        "original_prediction",
        "summary",
        "phases",
        "direction",
        "expected_range",
        "low",
        "high",
        "turning_points",
        "kind",
        "earliest_at",
        "latest_at",
        "price_range",
        "confidence",
        "version_id",
        "plans",
        "analysis_basis",
        "candidate_assessments",
        "summary_policy",
        "evidence_refs",
        "plan_key",
        "role",
        "side",
        "entry",
        "mode",
        "price",
        "zone",
        "activation",
        "invalidation",
        "post_activation_invalidation",
        "stop_loss",
        "take_profit",
        "volume_lots",
        "entry_deadline",
        "exit_deadline",
        "max_holding_minutes",
        "holding_time_basis",
        "rationale",
        "trading_style",
        "entry_checks",
        "operator",
        "quote_side",
        "instruction",
        "main_thesis",
        "supporting_evidence",
        "opposing_evidence",
        "alternative",
        "range_basis",
        "change_since_previous",
        "reassessment_conditions",
        "outcome",
        "missing_conditions",
        "scalp",
        "intraday",
        "rule",
        "forecast_id",
        "forecast_start",
        "forecast_end",
        "policy_version",
        "planning_delay_seconds",
        "default_seconds",
        "observed_seconds",
        "sample_version_ids",
        "basis",
        "enabled_styles",
        "scalp_refresh_minutes",
        "intraday_refresh_minutes",
        "reminders_enabled",
        "auto_prepare_requests",
        "input_count",
        "eligible_count",
        "sent_count",
        "excluded_not_closed_or_future",
        "truncation",
        "max_sent",
        "first_at",
        "last_at",
        "recent_closed_window",
        "gaps",
        "after",
        "before",
        "misaligned_or_incomplete_bar_count",
        "full_eligible_summary",
        "open",
        "close",
        "tick_volume",
        "expected",
        "latest_closed_at",
        "expected_close_at",
        "actual_close_at",
        "age_seconds",
        "max_age_seconds",
        "confirmed_at",
        "decision_at",
        "planning_answer_seconds",
        "range_coverage",
        "direction_accuracy",
        "turning_point_error",
        "price_error",
        "signed_error",
        "absolute_error",
        "mfe",
        "mae",
        "net_pnl",
        "net_profit",
        "quote_max_age_ms",
        "observation_basis",
        "source_count",
        "review_count",
        "incomplete_count",
        "completed_count",
        "unknown_count",
        "known_count",
        "source_available_at",
        "availability_boundary",
        "denominator_policy",
        "method_versions",
        "status",
        "aggregate",
        "categories",
        "support",
        "contradiction",
        "schema_version",
        "cutoff",
        "period_start",
        "period_end",
        "review_reference_total",
        "omitted_reference_count",
        "reference_policy",
        "cases",
        "notes",
        "forecast",
        "reference_plan",
        "actual_trade",
        "unique_targets",
        "scorable",
        "unknown",
        "not_triggered_or_not_entered",
        "outcomes",
        "evaluation_scopes",
        "known_net_totals",
        "net_unknown_or_not_applicable",
        "inputs",
        "evaluated_start",
        "evaluated_end",
        "metric_selection",
        "interpretation",
        "name",
        "value",
        "unit",
    }

    def bounded(value, parent=""):
        if isinstance(value, dict):
            if parent in {"methods", "evaluation_scopes", "outcomes"}:
                return {
                    key: v
                    for key, v in value.items()
                    if re.fullmatch(r"[A-Za-z0-9_./:-]{1,100}", key)
                    and isinstance(v, int)
                    and not isinstance(v, bool)
                }
            if parent == "known_net_totals":
                return {key: bounded(v) for key, v in value.items() if re.fullmatch(r"[A-Z]{3,8}", key)}
            return {key: bounded(v, key) for key, v in value.items() if key in allowed}
        if isinstance(value, list):
            return [bounded(v, parent) for v in value]
        return value

    if "analysis_preferences" in request:
        request["analysis_preferences"] = bounded(request["analysis_preferences"])
    projected = {
        "request": request,
        "market": market,
        "portfolio": portfolio,
        **{
            key: bounded(facts[key])
            for key in (
                "bar_compression",
                "budget_and_cost",
                "statistics",
                "previous_analysis",
                "timing_reference",
                "analysis_preferences",
            )
            if key in facts
        },
        **pick(facts, "evidence_refs data_kind full_market_reference"),
    }
    if "experience" in facts:
        experience = (expand_experience(facts["experience"])
                      if facts.get("packet_encoding") == "macro-packet/1" else facts["experience"])
        projected["experience"] = project_experience(experience)
    if facts.get("holding_review"):
        projected["holding_review"] = project_holding_review(facts["holding_review"])
    if facts["request"].get("prompt_version") in {"manual-analysis/9", "manual-analysis/10", "manual-analysis/11", "manual-analysis/12", "manual-analysis/13", "manual-analysis/14", "manual-analysis/15", "manual-analysis/16", "manual-analysis/17", "manual-analysis/18", "manual-analysis/19", "manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        projected["previous_analysis"] = project_previous_analysis(facts.get("previous_analysis"))
        for value, original in ((projected["analysis_preferences"], facts["analysis_preferences"]),
                                (request["analysis_preferences"], facts["request"]["analysis_preferences"])):
            if "scalp_preferences" in original:
                value["scalp_preferences"] = pick(original["scalp_preferences"],
                    "version price_unit target_min target_max max_entry_distance max_trigger_distance preferred_holding_minutes max_holding_minutes")
        if "near_term" in facts:
            projected["near_term"] = project_near_term(facts["near_term"])
        if "previous_candidate_suitability" in facts:
            projected["previous_candidate_suitability"] = [
                {"plan_key": item["plan_key"], "diagnostic": project_suitability(item["diagnostic"])}
                for item in facts["previous_candidate_suitability"]]
        for series in market["series"]:
            columns = "open_at close_at open high low close tick_volume closed".split()
            series["bar_columns"] = columns
            series["bars"] = [[bar.get(key) for key in columns] for bar in series["bars"]]
        projected["bar_encoding"] = "ohlc-rows/1: each bars row follows its bar_columns; no values or rows removed"
    if facts["request"].get("prompt_version") in {"manual-analysis/10", "manual-analysis/11", "manual-analysis/12", "manual-analysis/13", "manual-analysis/14", "manual-analysis/15", "manual-analysis/16", "manual-analysis/17", "manual-analysis/18", "manual-analysis/19", "manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        projected["experience"] = compact_experience(projected.get("experience"))
        projected["experience_encoding"] = ("cases/metrics may use named-rows/1; for each group, every row follows its columns. "
            "Expand each row to an object before interpretation. Groups preserve source order and missing keys; no field value or case removed. "
            "This is input encoding only; output arrays must follow the authoritative answer Schema.")
        from .quality import PREDICTION_POLICY, CONDITION_POLICY
        projected["prediction_policy"] = {k:facts["prediction_policy"][k]
            for k in [*PREDICTION_POLICY,'endpoint_max_age_ms'] if k in facts['prediction_policy']}
        projected["condition_policy"] = {k:facts["condition_policy"][k] for k in CONDITION_POLICY}
        projected["experience_cohort"] = pick(facts["experience_cohort"],
            "version case_count unique_forecast_versions cases_without_forecast_identity overlap_components independent_sample_count notes")
        alignment = facts["snapshot_alignment"]
        projected["snapshot_alignment"] = pick(alignment,
            "version market_snapshot_id portfolio_snapshot_id binding capture_settings_revision request_settings_revision settings_semantics market_cutoff")
        for name in ("account_status", "orders_status"):
            projected["snapshot_alignment"][name] = pick(alignment.get(name) or {}, "state observed_at captured_at reasons")
        evidence = facts["forecast_evidence"]
        projected["forecast_evidence"] = pick(evidence, "version market_snapshot_id market_cutoff notes")
        projected["forecast_evidence"]["horizons"] = []
        for item in evidence["horizons"]:
            row = pick(item, "horizon_minutes remaining_minutes state sample_count timeframe price_basis skipped_incomplete_windows start_at end_at")
            row["baseline"] = pick(item["baseline"], "anchor_at anchor_price no_change fixed_drift lookback_minutes") if item.get("baseline") else None
            row["quantiles"] = {name: pick(q, "p10 p50 p90") if q else None for name,q in item["quantiles"].items()} if item.get("quantiles") else None
            projected["forecast_evidence"]["horizons"].append(row)
    if facts["request"].get("prompt_version") in {"manual-analysis/14", "manual-analysis/15", "manual-analysis/16", "manual-analysis/17", "manual-analysis/18", "manual-analysis/19", "manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        projected["decision_context"] = project_decision_context(facts["decision_context"])
    if facts["request"].get("prompt_version") in {"manual-analysis/17", "manual-analysis/18", "manual-analysis/19", "manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        projected["output_constraints"] = project_output_constraints(facts["output_constraints"])
    if facts["request"].get("prompt_version") in {"manual-analysis/15", "manual-analysis/16"}:
        projected["entry_selection_context"] = project_entry_selection_context(facts["entry_selection_context"])
    if facts["request"].get("prompt_version") == "manual-analysis/16":
        projected["forecast_comparison_context"] = project_forecast_reference(facts["forecast_comparison_context"])
    if facts["request"].get("prompt_version") in {"manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        macro = None
        if facts["request"].get("prompt_version") == ACTUALS_PROMPT and facts.get("macro_research"):
            from .macro_research import project_macro
            macro=project_macro(facts["macro_research"],cutoff=request["market_cutoff"],window_end=request["window_end"])
            projected["macro_research"]=macro
        projected["economic_context"] = project_economic_context(facts["economic_context"], prompt_version=facts["request"].get("prompt_version"),macro_research=macro)
    if facts["request"].get("prompt_version") in {REVIEW_PROMPT, ACTUALS_PROMPT}:
        projected["forecast_review_input"] = build_review_input(request, projected["economic_context"], facts["market"])
    return compact_packet(prefix, projected, suffix)


def project_suitability(value):
    if value is None:
        return None
    result = pick(value, "version preference_version state reasons codes execution_side basis_quote_at entry_distance required_path whole_range_geometry current_target_space current_stop_space")
    for key in ("effective_entry_range", "target_space", "stop_space"):
        result[key] = pick(value[key], "low high") if value.get(key) else None
    result["trigger_distances"] = [pick(item, "role kind price_basis threshold distance state note") for item in value.get("trigger_distances", [])]
    result["entry_boundaries"] = [pick(item, "entry_price target_space stop_space") for item in value.get("entry_boundaries", [])]
    return result


def project_previous_analysis(value):
    if value is None:
        return None
    rule = lambda item: pick(item, "kind timeframe operator price quote_side instruction observation purpose") if item else item
    output = pick(value, "version_id available_at market_cutoff summary summary_policy")
    output["horizons"] = []
    for horizon in value.get("horizons", []):
        projected = pick(horizon, "horizon_minutes end_at direction confidence")
        projected["expected_range"] = pick(horizon["expected_range"], "low high")
        projected["phases"] = [pick(phase, "start_at end_at direction note") for phase in horizon["phases"]]
        projected["turning_points"] = [{**pick(turning, "kind earliest_at latest_at rationale"),
            "price_range": pick(turning["price_range"], "low high")} for turning in horizon["turning_points"]]
        if "forecast_basis" in horizon:
            projected["forecast_basis"] = pick(horizon["forecast_basis"],
                "direction_basis range_basis supporting_evidence opposing_evidence evidence_refs limitations")
        output["horizons"].append(projected)
    output["plans"] = []
    for plan in value.get("plans", []):
        projected = pick(plan, "plan_key role side stop_loss take_profit volume_lots valid_from entry_deadline exit_deadline max_holding_minutes rationale trading_style holding_time_basis")
        projected["entry"] = {**pick(plan["entry"], "mode price"),
            "zone": pick(plan["entry"]["zone"], "low high") if plan["entry"].get("zone") else None}
        for name in ("activation", "invalidation", "post_activation_invalidation"):
            if name in plan:
                projected[name] = rule(plan[name])
        if "entry_checks" in plan:
            projected["entry_checks"] = [rule(item) for item in plan["entry_checks"]]
        if "holding_exit" in plan:
            projected["holding_exit"] = {"condition": rule(plan["holding_exit"]["condition"]), "reason": plan["holding_exit"]["reason"]}
        output["plans"].append(projected)
    basis = value.get("analysis_basis")
    output["analysis_basis"] = ({**pick(basis, "main_thesis supporting_evidence opposing_evidence alternative range_basis change_since_previous limitations"),
        "reassessment_conditions": [{"trading_style": item["trading_style"], "condition": rule(item["condition"])}
            for item in basis["reassessment_conditions"]]} if basis else None)
    assessments = value.get("candidate_assessments")
    output["candidate_assessments"] = [pick(item, "trading_style outcome plan_key rationale missing_conditions") for item in assessments] if assessments is not None else None
    return output


def project_holding_review(value):
    rule = lambda r: pick(r, "kind timeframe operator price quote_side") if r else None
    out = pick(value, "plan_id opened_at deadline original_version_id")
    out["order"] = pick(value["order"], "account_scope_id kind broker_id")
    out["local_facts"] = pick(value.get("local_facts", {}), "exit_price target_distance remaining_seconds original_stop original_target broker_stop broker_target")
    plan = value.get("original_plan")
    out["original_plan"] = pick(plan, "plan_key side stop_loss take_profit rationale exit_deadline max_holding_minutes") if plan else None
    if plan:
        for key in ("invalidation", "post_activation_invalidation"):
            out["original_plan"][key] = rule(plan.get(key))
        policy = plan.get("holding_exit")
        out["original_plan"]["holding_exit"] = {"condition": rule(policy["condition"]), "reason": policy["reason"]} if policy else None
    previous = value.get("previous_review")
    out["previous_review"] = None
    if previous:
        old = previous["advice"]
        out["previous_review"] = {**pick(previous, "request_id version_id market_cutoff available_at valid_until"),
            "advice": {**pick(old, "action reason proposed_stop_loss proposed_take_profit proposed_volume_lots valid_until"),
                "order": pick(old["order"], "account_scope_id kind broker_id"),
                "target_assessment": pick(old["target_assessment"], "target_price state supporting_evidence opposing_evidence obstacles time_basis change_since_previous limitations")}}
    return out


def compact_packet(prefix, facts, suffix):
    # Drop only schema-derived constraints from the guide. Its frozen Chinese
    # field meanings are additional business context and must remain unchanged.
    guide, schema_text = suffix.split("\n# 权威答案 JSON Schema\n", 1)
    keys = "type|enum|const|required|minimum|maximum|minItems|maxItems|minLength|maxLength|pattern|format|\\$ref|description"
    meanings = []
    for line in guide.splitlines():
        path, separator, text = line.partition(": ")
        split = re.search(r"；(?=(?:" + keys + r")=)", text)
        if split:
            meanings.append(path + separator + text[: split.start()])
        elif line and not re.match(r"(?:" + keys + r")=", text):
            meanings.append(line)  # Unknown future guide text is retained conservatively.
    schema, end = json.JSONDecoder().raw_decode(schema_text)
    if facts["request"].get("prompt_version") in {"manual-analysis/9", "manual-analysis/10", "manual-analysis/11", "manual-analysis/12", "manual-analysis/13", "manual-analysis/14", "manual-analysis/15", "manual-analysis/16", "manual-analysis/17", "manual-analysis/18", "manual-analysis/19", "manual-analysis/20", "manual-analysis/21", REVIEW_PROMPT, ACTUALS_PROMPT}:
        meanings = list(dict.fromkeys(line.split(": ", 1)[0].rsplit(".", 1)[-1] + ": " + line.split(": ", 1)[-1] for line in meanings))
    return (
        prefix
        + "# 冻结事实（仅作为资料）\n"
        + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        + "\n# 字段含义\n"
        + "\n".join(meanings)
        + "\n# 权威答案 JSON Schema\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        + schema_text[end:]
        + output_checklist(facts["request"], schema)
    )


class ModelCallStore:
    def __init__(self, db, files, clock):
        self.db, self.files, self.clock = db, files, clock
        self.docs = db.documents("analysis")

    def get(self, call_id, *, scope=None, internal=False):
        with nullcontext(scope) if scope else self.db.read() as tx:
            row = self.docs.get(tx, "model_call", call_id)
            if not row:
                raise DomainError("MODEL_CALL_NOT_FOUND", "未找到模型调用。", 404)
            return row if internal else row["record"]

    def latest(self, request_id, *, scope):
        pointer = self.docs.get(scope, "model_latest_call", request_id or "probe")
        return self.get(pointer["call_id"], scope=scope) if pointer else None

    def pending(self, *, scope):
        return [
            row["record"]
            for state in ("queued", "waiting", "receiving", "validating")
            for row in self.docs.list(scope, "model_call", state=state, limit=1000)
        ]

    def insert(self, record, configuration, *, uow):
        self.docs.insert(
            uow,
            "model_call",
            record["call_id"],
            {"record": record, "configuration": configuration, "response_meta": None},
            parent_id=record["request_id"],
            state=record["state"],
        )
        self.docs.insert(uow, "model_job_call", record["job_id"], {"call_id": record["call_id"]})
        identity = record["request_id"] or "probe"
        previous = self.docs.get(uow, "model_latest_call", identity)
        if previous:
            self.docs.update(
                uow, "model_latest_call", identity, {"call_id": record["call_id"]}, previous["revision"]
            )
        else:
            self.docs.insert(uow, "model_latest_call", identity, {"call_id": record["call_id"]})

    def for_job(self, job_id, *, scope):
        pointer = self.docs.get(scope, "model_job_call", job_id)
        return self.get(pointer["call_id"], scope=scope) if pointer else None

    def update(self, call_id, patch, *, uow, response_meta=None):
        row = self.get(call_id, scope=uow, internal=True)
        record = {**row["record"], **patch}
        self.docs.update(
            uow,
            "model_call",
            call_id,
            {
                "record": record,
                "configuration": row["configuration"],
                "response_meta": response_meta if response_meta is not None else row["response_meta"],
            },
            row["revision"],
            state=record["state"],
        )
        return record

    def prepare(self, raw):
        return self.files.prepare(raw, media_type="application/json")

    def register(self, attachment, *, uow):
        ref = self.files.register(uow, attachment)
        return {"id": ref.id, "sha256": ref.sha256, "size": ref.size}

    def read(self, attachment, *, scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            return self.files.read(tx, attachment["id"])

    def reserve(self, config, input_bytes, *, uow):
        day = self.clock()[:10]  # UTC day, stated in the settings UI.
        previous = self.docs.get(uow, "model_daily_reservation", day)
        count = previous["count"] if previous else 0
        amounts = dict(previous["amounts"]) if previous else {}
        if count >= config["daily_call_limit"]:
            raise DomainError("MODEL_CALL_LIMIT", "今天（UTC）的模型调用次数上限已达；未发送。", 409)
        estimate = estimate_cost(config, input_bytes, config["max_output_tokens"])
        currency = config["price_currency"]
        if estimate is not None:
            total = Decimal(amounts.get(currency, "0")) + Decimal(estimate)
            if config["daily_budget"] is not None and total > Decimal(config["daily_budget"]):
                raise DomainError(
                    "MODEL_BUDGET_LIMIT", "按输入字节及输出上限预留后超过今日估算预算；未发送。", 409
                )
            amounts[currency] = str(total)
        elif config["daily_budget"] is not None:
            raise DomainError("MODEL_PRICE_UNKNOWN", "本站价格未知，不能执行金额预算检查；未发送。", 409)
        data = {"count": count + 1, "amounts": amounts}
        if previous:
            self.docs.update(uow, "model_daily_reservation", day, data, previous["revision"])
        else:
            self.docs.insert(uow, "model_daily_reservation", day, data)

    def reserve_experiment(self, plan, sample_id, arm, input_bytes, *, uow):
        """Same daily budget; an isolated append-once arm claim survives process crashes."""
        identity = plan["experiment_id"] + ":" + sample_id + ":" + arm
        if self.docs.get(uow, "experience_experiment_call", identity):
            raise DomainError("EXPERIMENT_ALREADY_CLAIMED", "该实验组已预留或调用，不能重发；先核对原证据。", 409)
        ledger = self.docs.get(uow, "experience_experiment_budget", plan["experiment_id"])
        count = ledger["count"] if ledger else 0
        amount = Decimal(ledger["reserved_cost"]) if ledger else Decimal(0)
        if plan["call_budget"] is None or count >= plan["call_budget"]:
            raise DomainError("EXPERIMENT_CALL_LIMIT", "实验总次数未配置或已耗尽；未发送。", 409)
        estimate = estimate_cost(plan["configuration"], input_bytes, plan["configuration"]["max_output_tokens"])
        if plan["cost_budget"] is not None:
            if estimate is None:
                raise DomainError("MODEL_PRICE_UNKNOWN", "实验价格未知，无法检查金额限制。", 409)
            if amount + Decimal(estimate) > Decimal(plan["cost_budget"]):
                raise DomainError("EXPERIMENT_COST_LIMIT", "超过实验估算费用总上限；未发送。", 409)
        elif estimate is None and not plan["accept_unknown_cost"]:
            raise DomainError("MODEL_PRICE_UNKNOWN", "费用未知；须明确采用次数上限后再主动启动。", 409)
        self.reserve(plan["configuration"], input_bytes, uow=uow)
        value = {"count": count + 1, "reserved_cost": str(amount + Decimal(estimate or "0")),
                 "cost_unknown": estimate is None or bool(ledger and ledger.get("cost_unknown"))}
        if ledger:
            self.docs.update(uow, "experience_experiment_budget", plan["experiment_id"], value, ledger["revision"])
        else:
            self.docs.insert(uow, "experience_experiment_budget", plan["experiment_id"], value)
        self.docs.insert(uow, "experience_experiment_call", identity,
            {"experiment_id": plan["experiment_id"], "sample_id": sample_id, "arm": arm,
             "state": "outcome_unknown", "claimed_at": self.clock(), "reserved_cost": estimate,
             "reason": "预留后中断时保持未知，绝不自动重发。"})
        return identity

    def finish_experiment(self, identity, result, *, uow):
        prior = self.docs.get(uow, "experience_experiment_call", identity)
        self.docs.update(uow, "experience_experiment_call", identity,
                         {**prior, "result": result, "state": result["state"]}, prior["revision"])


def estimate_cost(config, input_tokens, output_tokens):
    if (
        input_tokens is None
        or output_tokens is None
        or not all(
            config.get(k) is not None
            for k in ("input_price_per_million", "output_price_per_million", "price_currency", "price_source")
        )
    ):
        return None
    return format(
        (
            Decimal(config["input_price_per_million"]) * input_tokens
            + Decimal(config["output_price_per_million"]) * output_tokens
        )
        / Decimal(1000000),
        "f",
    )
