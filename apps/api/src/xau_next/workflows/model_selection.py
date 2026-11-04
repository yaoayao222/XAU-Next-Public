"""Four-model experiment only. Never activates profiles or registers business answers."""

import copy
import hashlib
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from xau_next.domains.analysis.public import (
    build_economic_context,
    bind_shared_research_refs,
    constrain_case_refs,
    effective_frozen_schema,
    estimate_cost,
    frozen_answer_schema,
    outbound_packet,
)
from xau_next.domains.review.public import score_selection_window
from xau_next.platform.model_gateway.public import audit_search_response, extract, normalize_base, redact_response, request_body
from xau_next.platform.model_gateway.selection import SelectionLedger
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc, utc_now
from xau_next.workflows.experience_experiment import validate_output

METHOD = "four-model-selection/1"
ARMS = {"A": "gpt-6.1-sol", "B": "gpt-6-astra", "C": "deepseek-flash", "D": "deepseek-v4-pro"}
CONFIG_FIELDS = [
    "enabled",
    "provider",
    "base_url",
    "protocol",
    "model_id",
    "reasoning_effort",
    "output_format",
    "stream",
    "timeout_seconds",
    "max_output_tokens",
    "max_response_bytes",
    "max_answer_bytes",
    "daily_call_limit",
    "daily_budget",
    "input_price_per_million",
    "output_price_per_million",
    "price_currency",
    "price_source",
]


def fail(code, message):
    raise DomainError(code, message, 409)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, value):
    """Exclusive durable files: an interrupted write is preserved, never overwritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = value if isinstance(value, bytes) else canonical(value)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(raw)


def make_plan(profile, formal_root, *, answer_schema, clock=utc_now, cohort="forward"):
    """Copy only the saved CPA values/reference. No constructor, key read, or activation."""
    values = {key: copy.deepcopy(profile["values"][key]) for key in CONFIG_FIELDS}
    base = normalize_base(values["base_url"])
    if not base.startswith("http://127.0.0.1:") or values["protocol"] != "responses":
        fail("SELECTION_CPA_REQUIRED", "请选择已保存的本地CPA Responses配置。")
    if not profile.get("credential_ref"):
        fail("SELECTION_CPA_REFERENCE_REQUIRED", "CPA配置缺少本应用凭据引用。")
    configs = {}
    experiment_id = "selection-" + digest({"at": clock(), "root": str(Path(formal_root).resolve())})[:24]
    deepseek_ref = "XAU-Next/model/" + experiment_id + "-deepseek"
    for arm, model in ARMS.items():
        config = copy.deepcopy(values)
        config.update(
            base_url=base,
            model_id=model,
            reasoning_effort="high",
            output_format="json_schema",
            protocol="responses",
            stream=True,
        )
        if arm in "CD":
            config.update(
                provider="DeepSeek官方",
                base_url="https://api.deepseek.com/v1",
                daily_budget=None,
                input_price_per_million=None,
                output_price_per_million=None,
                price_currency=None,
                price_source=None,
            )
        configs[arm] = {
            "configuration": config,
            "credential_ref": profile["credential_ref"] if arm in "AB" else deepseek_ref,
            "accepted_returned_models": [model],
            "channel": "saved_cpa" if arm in "AB" else "deepseek_official",
        }
    return {
        "method": METHOD,
        "status": "DRAFT",
        "cohort": cohort,
        "experiment_id": experiment_id,
        "created_at": clock(),
        "formal_root": str(Path(formal_root).resolve()),
        "cpa_profile_id": profile["profile_id"],
        "cpa_profile_fingerprint": digest(profile),
        "arms": configs,
        "total_call_limit": 48,
        "stage_call_limits": {"1": 24, "2": 24},
        "answer_schema_sha256": digest(effective_frozen_schema(answer_schema, "1.7.0")),
        "cost_limits": {},
        "accept_unknown_cost": False,
        "first_source_not_before": clock(),
        "horizons": [30, 60, 180],
        "primary_horizon": 30,
        "minimum_common_minutes": 15,
        "max_freeze_age_seconds": 30,
        "max_send_age_seconds": 60,
        "max_inflight": 4,
        "neutral_band": "0.00",
        "registered_samples": [],
        "method_hashes": method_hashes(),
        "claim_limit": "exploratory; not stable superiority or profit",
    }


def method_hashes():
    import xau_next

    # Include transitive shared validation/computations. Wheel-only migration
    # copies are not part of the prediction/scoring method implementation.
    base = Path(xau_next.__file__).resolve().parent
    return {
        path.relative_to(base).as_posix(): digest(path.read_bytes())
        for path in sorted(base.rglob("*.py"))
        if path.relative_to(base).parts[0] != "_migrations"
    }


def validate_plan(plan, *, frozen=False):
    if plan.get("method") != METHOD or plan.get("status") not in ("DRAFT", "FROZEN"):
        fail("SELECTION_PLAN_INVALID", "实验计划格式无效。")
    if frozen and plan["status"] != "FROZEN":
        fail("SELECTION_NOT_FROZEN", "草案没有收费执行资格。")
    purpose = plan.get("purpose", "model_selection")
    if purpose not in {"model_selection", "three_model_selection", "deepseek_compatibility", "cpa_compatibility"}:
        fail("SELECTION_PLAN_INVALID", "实验用途无效。")
    prompt = plan.get("prompt_version", "manual-analysis/14")
    revisions = {"manual-analysis/17": "output-constraints/1", "manual-analysis/18": "output-constraints/2",
                 "manual-analysis/19": "output-constraints/3", "manual-analysis/20": "economic-context/1",
                 "manual-analysis/21": "shared-search/1", "manual-analysis/22": "shared-search/2", "manual-analysis/23": "economic-actuals/1"}
    multi = prompt=="manual-analysis/23" and plan.get('input_revision')=='macro-research/2'
    if multi and (not isinstance(plan.get('macro_research_sha256'),str) or len(plan['macro_research_sha256'])!=64
                  or any(c not in '0123456789abcdef' for c in plan['macro_research_sha256'])):
        fail('SELECTION_RESEARCH_INVALID','多来源共同资料必须绑定完整冻结宏观资料哈希。')
    if prompt not in {"manual-analysis/14", *revisions} or (
        prompt in revisions and plan.get("input_revision") != revisions[prompt] and not multi
    ):
        fail("SELECTION_PLAN_INVALID", "输入只允许原27b或显式登记的输出约束修订，不能混入28/29。")
    research = plan.get("external_research")
    if prompt in {"manual-analysis/20", "manual-analysis/21", "manual-analysis/22", "manual-analysis/23"}:
        is_shared = prompt in {"manual-analysis/21", "manual-analysis/22", "manual-analysis/23"}
        if (purpose != ("model_selection" if is_shared else "three_model_selection") or not isinstance(research, dict)
            or set(research) != {"mode", "context_path", "context_sha256"}
            or research["mode"] not in ({"shared_calendar", "shared_search"} if prompt == "manual-analysis/23" else {"shared_search"} if is_shared else {"shared_calendar", "native_search_probe"})
            or not isinstance(research["context_path"], str) or not research["context_path"]
            or not isinstance(research["context_sha256"], str) or len(research["context_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in research["context_sha256"])):
            fail("SELECTION_RESEARCH_INVALID", "新经济资料版本必须独立登记三模型、资料哈希及研究模式。")
        if research["mode"] == "native_search_probe" and (not plan["accept_unknown_cost"] or plan["cost_limits"]):
            fail("SELECTION_SEARCH_COST_UNKNOWN", "工具费不在token估算内，搜索验证须明确接受未知费用且不能声明金额封顶。")
    elif research is not None:
        fail("SELECTION_RESEARCH_INVALID", "旧输入修订不能隐式增加外部资料或搜索。")
    if plan["horizons"] != [30, 60, 180] or plan["neutral_band"] != "0.00":
        fail("SELECTION_PLAN_INVALID", "主周期或评分方法已改变。")
    if (
        plan["primary_horizon"] != 30
        or plan["minimum_common_minutes"] != 15
        or plan["max_freeze_age_seconds"] != 30
        or plan["max_send_age_seconds"] != 60
    ):
        fail("SELECTION_PLAN_INVALID", "本轮时间窗口与主指标必须保持预定值。")
    if type(plan["total_call_limit"]) is not int or not 0 < plan["total_call_limit"] <= 48:
        fail("SELECTION_BUDGET_INVALID", "总次数必须为1—48。")
    extension = plan.get("shared_budget_extension")
    if extension is not None:
        keys = {"prior_shared_attempts", "additional_attempts", "cumulative_limit", "human_authorization_sha256"}
        shared_four = purpose == "model_selection" and prompt in {"manual-analysis/21", "manual-analysis/22", "manual-analysis/23"} and research["mode"] in ({"shared_calendar", "shared_search"} if prompt == "manual-analysis/23" else {"shared_search"})
        allowed_route = shared_four or (purpose == "three_model_selection" and prompt == "manual-analysis/20" and research["mode"] == "native_search_probe")
        extension_size = 4 if shared_four else 3
        if (not isinstance(extension, dict) or set(extension) != keys
            or not allowed_route
            or any(type(extension[k]) is not int for k in ("prior_shared_attempts", "additional_attempts", "cumulative_limit"))
            or extension["prior_shared_attempts"] < 0 or extension["additional_attempts"] != extension_size
            or extension["cumulative_limit"] != extension["prior_shared_attempts"] + extension_size
            or plan["total_call_limit"] != extension_size
            or not isinstance(extension["human_authorization_sha256"], str)
            or len(extension["human_authorization_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in extension["human_authorization_sha256"])
            or len(plan["registered_samples"]) != 1
            or plan["registered_samples"][0]["stage"] != 1
            or plan["registered_samples"][0]["arms"] != list("ABCD" if shared_four else "ABD")):
            fail("SELECTION_BUDGET_INVALID", "共享账本追加仅允许当前输入版本独立授权的一组，每模型一次。")
    if plan["stage_call_limits"] != {"1": 24, "2": 24} or plan["max_inflight"] != 4:
        fail("SELECTION_BUDGET_INVALID", "阶段上限及并发必须事先固定。")
    if set(plan["arms"]) != set(ARMS):
        fail("SELECTION_PLAN_INVALID", "四个实验臂不能缺失或增加。")
    if plan.get("cohort") not in ("forward", "synthetic"):
        fail("SELECTION_PLAN_INVALID", "实验只能是前向或显式合成。")
    if type(plan.get("accept_unknown_cost")) is not bool:
        fail("SELECTION_BUDGET_INVALID", "金额未知政策须明确为布尔值。")
    for arm, item in plan["arms"].items():
        config = item["configuration"]
        if set(config) != set(CONFIG_FIELDS):
            fail("SELECTION_CONFIG_INVALID", "模型配置字段不完整或含非白名单字段。")
        if (
            config["model_id"] != ARMS[arm]
            or config["protocol"] != "responses"
            or config["reasoning_effort"] != "high"
            or config["output_format"] != "json_schema"
            or config["stream"] is not True
        ):
            fail("SELECTION_CONFIG_INVALID", "本轮只允许预定模型、high、Responses严格格式和流式。")
        if normalize_base(config["base_url"]) != config["base_url"]:
            fail("SELECTION_CONFIG_INVALID", "模型地址未标准化。")
        if arm in "CD" and config["base_url"] != "https://api.deepseek.com/v1":
            fail("SELECTION_OFFICIAL_REQUIRED", "DeepSeek实验只接受官方API地址。")
        if arm in "AB" and not config["base_url"].startswith("http://127.0.0.1:"):
            fail("SELECTION_CPA_REQUIRED", "Sol和Astra必须复用本地CPA。")
        for name, low, high in [
            ("timeout_seconds", 1, 600),
            ("max_output_tokens", 1, 128000),
            ("max_response_bytes", 1, 16000000),
            ("max_answer_bytes", 1, 2000000),
            ("daily_call_limit", 1, 10000),
        ]:
            if type(config[name]) is not int or not low <= config[name] <= high:
                fail("SELECTION_CONFIG_INVALID", "模型限额必须为有效整数。")
        if not item["credential_ref"].startswith("XAU-Next/model/"):
            fail("SELECTION_KEY_REFERENCE_INVALID", "只接受本应用Windows凭据引用。")
        for price in ("input_price_per_million", "output_price_per_million", "daily_budget"):
            value = config[price]
            if value is not None and (not Decimal(value).is_finite() or Decimal(value) < 0):
                fail("SELECTION_PRICE_INVALID", "单价与金额预算必须是非负有限十进制。")
        if plan["cost_limits"] and estimate_cost(config, 1, 1) is None:
            fail("SELECTION_PRICE_UNKNOWN", "金额封顶计划不能包含未知单价渠道。")
        if ARMS[arm] not in item["accepted_returned_models"] or not item["accepted_returned_models"]:
            fail("SELECTION_MODEL_INVALID", "必须冻结允许的供应商模型声明。")
    # CPA arms must differ only in model id, including the saved credential reference.
    for amount in plan["cost_limits"].values():
        if not Decimal(amount).is_finite() or Decimal(amount) < 0:
            fail("SELECTION_PRICE_INVALID", "实验金额上限必须是非负有限十进制。")
    a, b = (copy.deepcopy(plan["arms"][k]) for k in "AB")
    for item in (a, b):
        item["configuration"].pop("model_id")
        item.pop("accepted_returned_models")
    if a != b:
        fail("SELECTION_CPA_CHANGED", "Sol与Astra必须复用同一CPA配置及凭据引用。")
    seen, previous = set(), None
    for slot in plan["registered_samples"]:
        identity = slot["sample_id"]
        if (
            identity in seen
            or not identity.startswith("sample-")
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in identity)
        ):
            fail("SELECTION_SLOT_INVALID", "样本标识重复或包含路径字符。")
        seen.add(identity)
        if slot["stage"] not in (1, 2):
            fail("SELECTION_SLOT_INVALID", "阶段必须是1或2。")
        arms = slot["arms"]
        if slot["stage"] == 1:
            if purpose in {"deepseek_compatibility", "cpa_compatibility"}:
                allowed = set("CD" if purpose == "deepseek_compatibility" else "AB")
                if not 1 <= len(arms) <= 2 or len(set(arms)) != len(arms) or not set(arms) <= allowed:
                    fail("SELECTION_SLOT_INVALID", "接入复测只允许当前用途明确登记的模型。")
            elif purpose == "three_model_selection":
                if len(arms) != 3 or set(arms) != set("ABD"):
                    fail("SELECTION_SLOT_INVALID", "三模型对比仅允许Sol、Astra、Pro各一次。")
            elif set(arms) != set(ARMS) or len(arms) != len(ARMS):
                fail("SELECTION_SLOT_INVALID", "第一阶段每组四个模型。")
        if purpose in {"deepseek_compatibility", "cpa_compatibility"} and slot["stage"] != 1:
            fail("SELECTION_SLOT_INVALID", "接入复测不能充当第二阶段选模。")
        if purpose == "three_model_selection" and slot["stage"] != 1:
            fail("SELECTION_SLOT_INVALID", "三模型补样只允许显式登记的第一阶段组。")
        if slot["stage"] == 2 and (
            len(arms) != 2 or "A" not in arms or len(set(arms)) != 2 or not set(arms) <= set(ARMS)
        ):
            fail("SELECTION_SLOT_INVALID", "第二阶段须保留Sol基线及一个挑战者。")
        at = parse_utc(slot["not_before"])
        if at < parse_utc(plan["first_source_not_before"]) or (
            previous and (at - previous).total_seconds() < 10800
        ):
            fail("SELECTION_SLOT_INVALID", "样本时段必须晚于冻结入口且相隔至少180分钟。")
        previous = at
    for stage in (1, 2):
        slots = [s for s in plan["registered_samples"] if s["stage"] == stage]
        if sum(len(s["arms"]) for s in slots) > 24:
            fail("SELECTION_STAGE_LIMIT", "本阶段登记槽位超过24次。")


def seal_plan(root, plan):
    """Offline freeze. Caller must supply the predeclared schedule; no paid authorization."""
    value = copy.deepcopy(plan)
    value["status"] = "FROZEN"
    validate_plan(value, frozen=True)
    if not value["registered_samples"]:
        fail("SELECTION_SCHEDULE_REQUIRED", "必须先登记样本日程。")
    if any(s["stage"] != 1 for s in value["registered_samples"]):
        fail("SELECTION_FOLLOWUP_INVALID", "先冻结第一阶段，筛选后另加第二阶段协议。")
    root = Path(root).resolve()
    formal = Path(value["formal_root"]).resolve()
    if root == formal or root.is_relative_to(formal) or formal.is_relative_to(root):
        fail("SELECTION_ROOT_INVALID", "实验目录必须与正式数据目录分离。")
    save(root / "plan.json", value)
    save(root / "plan.sha256", {"sha256": digest(value)})
    return value


def seal_followup(root, samples, challenger, evidence_files, *, clock=utc_now):
    """Append a second-stage choice; never rewrite phase one or reset its ledger."""
    root = Path(root)
    plan = load_plan(root)
    if challenger not in {"B", "C", "D"} or not samples or len(samples) > 12:
        fail("SELECTION_FOLLOWUP_INVALID", "第二阶段需一个挑战者及最多12个新样本。")
    first = [s for s in plan["registered_samples"] if s["stage"] == 1]
    if any(s["stage"] == 2 for s in plan["registered_samples"]):
        fail("SELECTION_FOLLOWUP_INVALID", "计划已经预登记第二阶段，不能另加。")
    for sample in first:
        if not (root / sample["sample_id"] / "group-result.json").exists():
            fail("SELECTION_FOLLOWUP_INVALID", "第一阶段尚未完成，不能提前选择赢家。")
    candidate = copy.deepcopy(plan)
    normalized = [{**s, "stage": 2, "arms": ["A", challenger]} for s in samples]
    if any(parse_utc(s["not_before"]) < parse_utc(clock()) for s in normalized):
        fail("SELECTION_FOLLOWUP_INVALID", "复核必须登记新的未来时段。")
    candidate["registered_samples"] += normalized
    validate_plan(candidate)
    evidence = {}
    for name in evidence_files:
        path = Path(name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            fail("SELECTION_FOLLOWUP_INVALID", "选择证据须来自本实验。")
        evidence[str(path.relative_to(root.resolve()))] = digest(path.read_bytes())
    if not evidence:
        fail("SELECTION_FOLLOWUP_INVALID", "必须登记筛选依据，不能空选赢家。")
    value = {
        "plan_sha256": digest(plan),
        "samples": normalized,
        "challenger": challenger,
        "evidence": evidence,
        "recorded_at": clock(),
    }
    save(root / "followup.json", value)
    return value


def load_plan(root):
    plan = read(Path(root) / "plan.json")
    validate_plan(plan, frozen=True)
    if read(Path(root) / "plan.sha256")["sha256"] != digest(plan) or plan["method_hashes"] != method_hashes():
        fail("SELECTION_PLAN_CHANGED", "计划或实现哈希变化，禁止继续旧实验。")
    return plan


def slot_for(plan, sample_id, root=None):
    samples = list(plan["registered_samples"])
    if root and (Path(root) / "followup.json").exists():
        followup = read(Path(root) / "followup.json")
        if followup["plan_sha256"] != digest(plan):
            fail("SELECTION_PLAN_CHANGED", "后续协议不属于当前计划。")
        for name, expected in followup["evidence"].items():
            path = (Path(root) / name).resolve()
            if not path.is_relative_to(Path(root).resolve()) or digest(path.read_bytes()) != expected:
                fail("SELECTION_PLAN_CHANGED", "筛选依据文件已改变。")
        samples += followup["samples"]
        candidate = {**plan, "registered_samples": samples}
        validate_plan(candidate)
    slot = next((s for s in samples if s["sample_id"] == sample_id), None)
    if not slot:
        fail("SELECTION_SLOT_INVALID", "样本未预登记。")
    return slot


def freeze_sample(root, sample_id, request, *, clock=utc_now):
    """One immutable 27b packet, identical schema and bytes for every model."""
    root = Path(root)
    plan = load_plan(root)
    slot = slot_for(plan, sample_id, root)
    expected_prompt = plan.get("prompt_version", "manual-analysis/14")
    if request["prompt_version"] != expected_prompt or request["answer_schema_version"] != "1.7.0":
        fail("SELECTION_BASELINE_REQUIRED", "本实验必须使用封存计划指定的27b输入修订与回答1.7，不允许28/29。")
    if sorted(request["horizon_minutes"]) != plan["horizons"]:
        fail("SELECTION_HORIZONS_INVALID", "新实验请求仅包含30/60/180分钟。")
    cutoff = parse_utc(request["market_cutoff"])
    if plan["cohort"] == "forward":
        if (
            not parse_utc(slot["not_before"])
            <= cutoff
            <= parse_utc(slot["not_before"]) + timedelta(seconds=60)
            or not 0 <= (parse_utc(clock()) - cutoff).total_seconds() <= plan["max_freeze_age_seconds"]
        ):
            fail("SELECTION_SOURCE_STALE", "不使用旧时点或未来资料补作前向请求。")
        if request["context"]["market"]["source_kind"] != "mt5":
            fail("SELECTION_SOURCE_INVALID", "前向实验必须使用真实只读同源资料。")
    context = request["context"]
    market = context["market"]
    if (
        request["kind"] != "new"
        or parse_utc(request["window_start"]) != cutoff
        or parse_utc(request["window_end"]) != cutoff + timedelta(minutes=180)
    ):
        fail("SELECTION_WINDOW_INVALID", "新实验只接受新请求及原30/60/180端点。")
    if (
        market["snapshot_id"] != request["market_snapshot_id"]
        or market["market_cutoff"] != request["market_cutoff"]
    ):
        fail("SELECTION_IDENTITY_INVALID", "冻结身份不一致。")
    if (
        market.get("quote_status", {}).get("state") != "available"
        or not market.get("quote")
        or not 0
        <= (cutoff - parse_utc(market["quote"]["event_time"])).total_seconds() * 1000
        <= context["settings"]["values"]["quote_max_age_ms"]
    ):
        fail("SELECTION_SOURCE_INVALID", "冻结Bid缺失、过时或晚于截止。")
    for series in market.get("series", []):
        if any(b.get("closed") and parse_utc(b["close_at"]) > cutoff for b in series.get("bars", [])):
            fail("SELECTION_FUTURE_INPUT", "闭柱资料晚于截止，不使用未来资料。")
    experience = context.get("experience")
    if experience and experience.get("available_at") and parse_utc(experience["available_at"]) > cutoff:
        fail("SELECTION_FUTURE_INPUT", "经验资料在截止后才可用。")
    if expected_prompt in {"manual-analysis/20", "manual-analysis/21", "manual-analysis/22", "manual-analysis/23"}:
        multi=context.get('macro_research',{}).get('version')=='macro-research/2'
        if multi != (plan.get('input_revision')=='macro-research/2') or (multi and
                digest(context['macro_research'])!=plan.get('macro_research_sha256')):
            fail('SELECTION_RESEARCH_CHANGED','多来源输入版本或完整资料与封存计划不同。')
        research = plan["external_research"]
        if (digest(context.get("economic_context")) != research["context_sha256"]
            or context.get("research_policy") != {"mode": research["mode"]}):
            fail("SELECTION_RESEARCH_CHANGED", "经济资料或搜索模式与封存计划不同。")
        if context["economic_context"].get("data_kind") != ("REAL" if plan["cohort"] == "forward" else "SYNTHETIC"):
            fail("SELECTION_RESEARCH_INVALID", "真实与合成经济资料不得混用。")
        build_economic_context(request, context)
        if expected_prompt in {"manual-analysis/21", "manual-analysis/22", "manual-analysis/23"} and bind_shared_research_refs(request, context)["evidence_refs"] != context["evidence_refs"]:
            fail("SELECTION_RESEARCH_REFS_UNBOUND", "共享来源尚未进入本包合法引用集合，未派发。")
    schema = frozen_answer_schema(request)
    if digest(schema) != plan["answer_schema_sha256"]:
        fail("SELECTION_SCHEMA_CHANGED", "回答契约与冻结的权威1.7格式不同，不能放宽。")
    packet = outbound_packet(request["packet_text"])
    facts = json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]
    if (
        facts["request"]["request_id"] != request["request_id"]
        or facts["request"]["prompt_version"] != expected_prompt
        or "entry_selection_context" in facts
        or "forecast_comparison_context" in facts
    ):
        fail("SELECTION_PACKET_INVALID", "实际外发不是固定27b资料。")
    # Rebuild via the pure authoritative builder to reject request/text mismatches.
    from xau_next.domains.analysis.public import build_packet

    expected = build_packet(
        {
            k: v
            for k, v in request.items()
            if k not in ("packet_text", "context", "id", "revision", "recorded_at")
        },
        context,
        schema,
    )

    def semantic_packet(value):
        prefix, tail = value.split("# 冻结事实（仅作为资料）\n", 1)
        data, end = json.JSONDecoder().raw_decode(tail.lstrip())
        return prefix, canonical(data), tail.lstrip()[end:]

    # Persistent document JSON sorts object keys; this must not mutate original packet bytes.
    if semantic_packet(outbound_packet(expected)) != semantic_packet(packet):
        fail("SELECTION_PACKET_INVALID", "请求与冻结外发内容不一致。")
    terminal = context["settings"]["values"].get("connection", {}).get("terminal_path")
    if terminal:
        packet = packet.replace(terminal, "[本机路径已移除]").replace(
            json.dumps(terminal)[1:-1], "[本机路径已移除]"
        )
    folder = root / sample_id
    if folder.exists():
        fail("SELECTION_SAMPLE_EXISTS", "原样本已经冻结，不覆盖、不重新构包。")
    hashes = {
        "request.json": save(folder / "request.json", request),
        "schema.json": save(folder / "schema.json", schema),
        "packet.txt": save(folder / "packet.txt", packet.encode()),
    }
    for arm in slot["arms"]:
        body = request_body(plan["arms"][arm]["configuration"], packet, constrain_case_refs(schema, request),
            web_search=plan.get("external_research", {}).get("mode") == "native_search_probe")
        hashes[arm + "-body.json"] = save(folder / (arm + "-body.json"), body)
    return save(
        folder / "frozen.json",
        {
            "files": hashes,
            "plan_sha256": digest(plan),
            "market_cutoff": request["market_cutoff"],
            "frozen_at": clock(),
        },
    )


def verify_sample(root, sample_id, plan):
    folder = Path(root) / sample_id
    frozen = read(folder / "frozen.json")
    if frozen["plan_sha256"] != digest(plan):
        fail("SELECTION_SAMPLE_CHANGED", "冻结样本不属于当前计划。")
    for name, expected in frozen["files"].items():
        if Path(name).name != name or digest((folder / name).read_bytes()) != expected:
            fail("SELECTION_SAMPLE_CHANGED", "原输入或实际外发哈希不符。")
    return read(folder / "request.json"), read(folder / "schema.json")


def authorize(plan, authorization, stage, sample_id=None):
    attempts = authorization.get("max_attempts")
    if (
        authorization.get("plan_sha256") != digest(plan)
        or authorization.get("allow_paid") is not True
        or stage not in authorization.get("stages", [])
        or type(attempts) is not int
        or not 0 < attempts <= plan["total_call_limit"]
    ):
        fail("SELECTION_START_REQUIRED", "缺少对当前计划、阶段和次数的明确收费授权。")
    if authorization.get("accept_unknown_cost") != plan["accept_unknown_cost"]:
        fail("SELECTION_START_REQUIRED", "收费授权须明确对应的金额未知政策。")
    if plan["cohort"] == "forward" and authorization.get("data_kind") != "REAL":
        fail("SELECTION_START_REQUIRED", "合成授权不得用于真实渠道。")
    if "sample_ids" in authorization and sample_id not in authorization["sample_ids"]:
        fail("SELECTION_START_REQUIRED", "该样本不在本次授权范围内。")
    extension = plan.get("shared_budget_extension")
    if extension is not None and (
        attempts != extension["additional_attempts"]
        or authorization.get("shared_budget_extension_sha256") != digest(extension)
        or authorization.get("human_authorization_sha256") != extension["human_authorization_sha256"]
        or authorization.get("sample_ids") != [plan["registered_samples"][0]["sample_id"]]
    ):
        fail("SELECTION_START_REQUIRED", "追加共享额度须绑定本次人类指令、唯一组及三次上限。")


def run_sample(
    root,
    sample_id,
    *,
    authorization,
    gateway,
    credentials,
    calls,
    clock=utc_now,
    profile_check=lambda: None,
    stopped=lambda: False,
    on_result=lambda result: None,
):
    """One explicit group only; own claims + shared product reservations precede dispatch."""
    root = Path(root)
    plan = load_plan(root)
    slot = slot_for(plan, sample_id, root)
    authorize(plan, authorization, slot["stage"], sample_id)
    if slot["stage"] == 2:
        followup = read(root / "followup.json") if (root / "followup.json").exists() else None
        if not followup or authorization.get("followup_sha256") != digest(followup):
            fail("SELECTION_START_REQUIRED", "第二阶段须明确授权筛选后的追加协议。")
    if plan["cohort"] == "synthetic":
        import httpx

        if not isinstance(getattr(gateway, "transport", None), httpx.MockTransport):
            fail("SELECTION_SYNTHETIC_NETWORK", "合成实验只能使用MockTransport，不能访问真实网络。")
    if Path(calls.db.data_root).resolve() != Path(plan["formal_root"]).resolve():
        fail("SELECTION_BUDGET_ROOT", "必须使用冻结的共享计次目录，不可另建空账本。")
    request, schema = verify_sample(root, sample_id, plan)
    profile_check()
    if stopped():
        fail("SELECTION_CANCELLED_BEFORE_SEND", "用户已停止，未开始派发。")
    folder = root / sample_id
    ledger = SelectionLedger(root / "selection.sqlite3", digest(plan))
    status = ledger.status()
    if status["halt"] or any(state == "outcome_unknown" for state in status["slots"].values()):
        ledger.halt("UNFINISHED_CLAIM_NO_RESEND")
        fail("SELECTION_HALTED", "存在失败或未完成调用，禁止自动恢复或补发。")
    if (
        plan["cohort"] == "forward"
        and not 0
        <= (parse_utc(clock()) - parse_utc(request["market_cutoff"])).total_seconds()
        <= plan["max_send_age_seconds"]
    ):
        fail("SELECTION_SOURCE_STALE", "冻结资料已过派发时限，未补发历史请求。")
    save(folder / "group-started.json", {"at": clock(), "plan_sha256": digest(plan)})
    stop = threading.Event()

    def send(arm):
        item = plan["arms"][arm]
        config = item["configuration"]
        identity = plan["experiment_id"] + ":" + sample_id + ":" + arm
        result = {
            "arm": arm,
            "state": "not_sent",
            "error_code": None,
            "available_at": None,
            "schema_valid": False,
            "business_valid": False,
            "input_tokens": None,
            "output_tokens": None,
            "billing_amount": None,
            "billing_status": "UNKNOWN",
        }
        claimed, shared_identity = False, None
        key = None
        try:
            if stop.is_set() or stopped():
                result["error_code"] = "STOPPED_BEFORE_CLAIM"
                return result
            # The credential adapter reads only this application's explicit reference at dispatch.
            key = credentials.read(item["credential_ref"])
            if not isinstance(key, str) or not key:
                fail("MODEL_KEY_REQUIRED", "本机尚未配置该渠道密钥，未发送。")
            body = (folder / (arm + "-body.json")).read_bytes()
            if key.encode() in body:
                fail("MODEL_SECRET_IN_INPUT", "密钥出现在请求正文，已阻止。")
            estimate = estimate_cost(config, len(body), config["max_output_tokens"])
            currency = config["price_currency"]
            if estimate is None and not plan["accept_unknown_cost"]:
                fail("SELECTION_PRICE_UNKNOWN", "金额政策尚未明确，未发送。")
            ledger.claim(
                identity,
                slot["stage"],
                item["channel"],
                clock(),
                total_limit=authorization["max_attempts"],
                stage_limit=24,
                daily_limit=config["daily_call_limit"],
                reserved=estimate,
                currency=currency,
                cost_limit=plan["cost_limits"].get(currency),
            )
            claimed = True
            shared_plan = {
                "experiment_id": plan["experiment_id"],
                "call_budget": (plan["shared_budget_extension"]["cumulative_limit"]
                                if plan.get("shared_budget_extension") else authorization["max_attempts"]),
                "configuration": config,
                "cost_budget": None,
                "accept_unknown_cost": plan["accept_unknown_cost"],
            }
            with calls.db.write() as tx:
                shared_identity = calls.reserve_experiment(shared_plan, sample_id, arm, len(body), uow=tx)
            result["started_at"] = clock()
            save(folder / (arm + "-started.json"), {"at": result["started_at"], "claim": identity})
            # Stop only sends not yet begun. Already dispatched requests safely finish and retain evidence.
            if stop.is_set() or stopped():
                result["error_code"] = "STOPPED_AFTER_CLAIM_BEFORE_SEND"
                return result
            save(folder / (arm + "-sent.json"), {"at": clock(), "body_sha256": digest(body)})
            response = gateway.exchange(config, key, body, stopped=stopped)
            result.update(
                completed_received_at=clock(), http_status=response.status, diagnostics=response.diagnostics
            )
            raw, redacted = redact_response(response.raw, key)
            save(folder / (arm + "-raw.bin"), raw)
            result.update(raw_sha256=digest(raw), raw_redacted=redacted)
            if response.status and response.status >= 500 and b"TLS handshake: EOF" in raw:
                result["failure_stage_hint"] = "UPSTREAM_TLS_HANDSHAKE_EOF"
            response.raw = raw
            evidence = None
            if plan.get("external_research", {}).get("mode") == "native_search_probe":
                evidence = audit_search_response(response)
                save(folder / (arm + "-search.json"), evidence)
                result["search_evidence"] = evidence
            metadata = extract(response, config["protocol"])
            result.update({k: v for k, v in metadata.items() if k != "answer"})
            if evidence is not None:
                if not evidence["observed"]:
                    fail("MODEL_SEARCH_NOT_OBSERVED", "未观察到完成的搜索工具调用，不以回答自述代替。")
                if not evidence["sources_observed"]:
                    fail("MODEL_SEARCH_SOURCES_MISSING", "搜索未提供可审计来源，接入验证不通过。")
            if metadata["model_returned"] not in item["accepted_returned_models"]:
                fail("SELECTION_RETURNED_MODEL_MISMATCH", "返回模型声明不在预登记名单。")
            answer_raw = metadata["answer"].encode()
            if len(answer_raw) > config["max_answer_bytes"]:
                fail("ANSWER_TOO_LARGE", "提取回答超过原字节上限。")
            save(folder / (arm + "-answer.json"), answer_raw)
            checked = validate_output(answer_raw, schema, request, request["context"])
            result.update(checked)
            result["answer_sha256"] = digest(answer_raw)
            if not result["business_valid"]:
                fail(result.get("error_code") or "SELECTION_INVALID_ANSWER", "回答未通过原严格校验。")
            result.update(state="succeeded", available_at=clock())
        except Exception as error:  # noqa: BLE001 - persist only safe codes from any sender failure
            result.update(
                state="failed",
                error_code=error.code if isinstance(error, DomainError) else "SELECTION_LOCAL_FAILURE",
            )
            if result["error_code"] in {
                "MODEL_TIMEOUT_UNKNOWN",
                "MODEL_CONNECTION_UNKNOWN",
                "MODEL_OUTCOME_UNKNOWN",
                "MODEL_CANCELLED",
            }:
                result["state"] = "outcome_unknown"
            # Exception text and object are deliberately never persisted (may contain credentials).
            metadata = getattr(error, "model_metadata", {})
            result.update(
                {
                    k: v
                    for k, v in metadata.items()
                    if k in {"input_tokens", "output_tokens", "model_returned", "provider_response_id"}
                }
            )
            stop.set()
        finally:
            key = None
            if result["state"] != "succeeded":
                stop.set()
                ledger.halt(result["error_code"] or "GROUP_NOT_COMPLETE")
            result["ended_at"] = clock()
            save(folder / (arm + "-result.json"), result)
            if claimed:
                ledger.finish(identity, result)
            if shared_identity:
                with calls.db.write() as tx:
                    calls.finish_experiment(shared_identity, result, uow=tx)
            try:
                on_result(copy.deepcopy(result))
            except Exception:
                pass  # Durable files and claims remain authoritative; a UI update never resends.
        return result

    with ThreadPoolExecutor(max_workers=len(slot["arms"])) as pool:
        futures = {arm: pool.submit(send, arm) for arm in slot["arms"]}
        results = {arm: future.result() for arm, future in futures.items()}
    save(folder / "group-result.json", {"at": clock(), "arms": results})
    return results


def score_sample(root, sample_id, market, *, as_of, clock=utc_now):
    """Pure saved-source evaluation, append-only three scopes per model and horizon."""
    root = Path(root)
    plan = load_plan(root)
    if plan.get("external_research", {}).get("mode") == "native_search_probe":
        fail("SELECTION_SEARCH_NOT_COMPARABLE", "独立搜索仅验证接入，不进入严格同输入准确率统计。")
    slot = slot_for(plan, sample_id, root)
    request, _schema = verify_sample(root, sample_id, plan)
    if parse_utc(as_of) > parse_utc(clock()):
        fail("SELECTION_FUTURE_SCORE", "评价时刻不得晚于当前实际时刻。")
    folder = root / sample_id
    results = {
        arm: read(folder / (arm + "-result.json"))
        if (folder / (arm + "-result.json")).exists()
        else {"state": "not_sent"}
        for arm in slot["arms"]
    }
    times = [r.get("available_at") for r in results.values() if r["state"] == "succeeded"]
    common = max(times, key=parse_utc) if len(times) == len(results) and all(times) else None
    frozen_market = request["context"]["market"]
    if any(market.get(k) != frozen_market.get(k) for k in ("source_kind", "account_scope_id", "symbol")):
        fail("SELECTION_SOURCE_INVALID", "评分文件并非原冻结同源行情。")
    sources = {}
    for minutes in plan["horizons"]:
        path = folder / ("actual-" + str(minutes) + ".json")
        value = {"market": market, "as_of": as_of, "sha256": digest(market)}
        if path.exists():
            value = read(path)
            if digest(value["market"]) != value["sha256"] or parse_utc(value["as_of"]) > parse_utc(as_of):
                fail("SELECTION_SOURCE_CHANGED", "原取证文件被改变或晚于本次评价时刻。")
        elif parse_utc(as_of) >= parse_utc(request["market_cutoff"]) + timedelta(minutes=minutes):
            # First terminal source is immutable, including missing endpoints. Later
            # horizons must not replace an earlier good source or repair its unknown.
            save(path, value)
        sources[minutes] = value
    scored = {}
    for arm, result in results.items():
        records = []
        scored[arm] = records
        answer = None
        if result["state"] == "succeeded":
            raw = (folder / (arm + "-answer.json")).read_bytes()
            if digest(raw) != result["answer_sha256"]:
                fail("SELECTION_ANSWER_CHANGED", "原回答哈希改变，拒绝重评分。")
            answer = json.loads(raw)
        for minutes in plan["horizons"]:
            for scope, start in [
                ("full_descriptive", request["market_cutoff"]),
                ("after_own_available", result.get("available_at")),
                ("after_common_available", common),
            ]:
                if answer is None:
                    record = {"state": result["state"], "direction_class": result["state"], "metrics": []}
                else:
                    record = score_selection_window(
                        answer,
                        request["context"],
                        sources[minutes]["market"],
                        minutes=minutes,
                        start=start,
                        as_of=sources[minutes]["as_of"],
                        scope=scope,
                        available_at=result["available_at"],
                    )
                    if scope == "after_common_available" and start:
                        end = next(h["end_at"] for h in answer["horizons"] if h["horizon_minutes"] == minutes)
                        record["usable_primary"] = (
                            parse_utc(end) - parse_utc(start)
                        ).total_seconds() >= plan["minimum_common_minutes"] * 60
                        if minutes == 30 and not record["usable_primary"]:
                            record["primary_direction_class"] = "unusable_short_window"
                    record["actual_source_sha256"] = sources[minutes]["sha256"]
                    record["actual_source_as_of"] = sources[minutes]["as_of"]
                record.update(minutes=minutes, scope=scope)
                records.append(record)
    output = {
        "method": METHOD + "/score",
        "sample_id": sample_id,
        "as_of": as_of,
        "common_available_at": common,
        "arms": scored,
        "source_sha256": digest(market),
    }
    destination = folder / ("score-" + digest({"market": market, "as_of": as_of})[:20])
    save(destination / "market.json", market)
    save(destination / "scores.json", output)
    return output


def report(root):
    """All registered slots stay in every denominator, including never-frozen/not-sent ones."""
    root = Path(root)
    plan = load_plan(root)
    samples = list(plan["registered_samples"])
    if (root / "followup.json").exists():
        samples += read(root / "followup.json")["samples"]
    rows, sources = [], {}
    for sample in samples:
        slot_for(plan, sample["sample_id"], root)
        folder = root / sample["sample_id"]
        candidates = sorted(folder.glob("score-*/scores.json"))
        scores = [read(p) for p in candidates]
        selected = max(scores, key=lambda s: parse_utc(s["as_of"])) if scores else None
        if selected:
            sources[sample["sample_id"]] = {
                "score_sha256": digest(selected),
                "as_of": selected["as_of"],
                "source_sha256": selected["source_sha256"],
            }
        for arm in sample["arms"]:
            call = (
                read(folder / (arm + "-result.json"))
                if (folder / (arm + "-result.json")).exists()
                else {"state": "not_sent"}
            )
            for minutes in plan["horizons"]:
                for scope in ("full_descriptive", "after_own_available", "after_common_available"):
                    score = next(
                        (
                            r
                            for r in (selected or {}).get("arms", {}).get(arm, [])
                            if r["minutes"] == minutes and r["scope"] == scope
                        ),
                        None,
                    )
                    rows.append(
                        {
                            "sample": sample["sample_id"],
                            "stage": sample["stage"],
                            "arm": arm,
                            "minutes": minutes,
                            "scope": scope,
                            "class": score.get("primary_direction_class", score["direction_class"])
                            if score
                            else (call["state"] if call["state"] != "succeeded" else "unknown"),
                            "range": score.get("range", {}) if score else {},
                            "usable_primary": score.get("usable_primary") if score else None,
                        }
                    )
    summaries = []
    for stage in (1, 2):
        for arm in ARMS:
            for minutes in plan["horizons"]:
                for scope in ("full_descriptive", "after_own_available", "after_common_available"):
                    subset = [
                        r
                        for r in rows
                        if (r["stage"], r["arm"], r["minutes"], r["scope"]) == (stage, arm, minutes, scope)
                    ]
                    if not subset:
                        continue
                    counts = Counter(r["class"] for r in subset)
                    c, i, u = (counts[k] for k in ("correct", "incorrect", "uncertain"))
                    ranges = [r["range"] for r in subset if r["range"].get("state") == "AVAILABLE"]
                    summaries.append(
                        {
                            "stage": stage,
                            "arm": arm,
                            "minutes": minutes,
                            "scope": scope,
                            "registered": len(subset),
                            "counts": dict(counts),
                            "conditional_hit_rate": str(Decimal(c) / (c + i)) if c + i else None,
                            "explicit_coverage": str(Decimal(c + i) / (c + i + u)) if c + i + u else None,
                            "correct_per_registered": str(Decimal(c) / len(subset)),
                            "explicit_per_registered": str(Decimal(c + i) / len(subset)),
                            "usable_primary_count": sum(r["usable_primary"] is True for r in subset),
                            "range_denominator": len(ranges),
                            "mean_boundary_error": str(
                                sum((Decimal(r["boundary_absolute_error"]) for r in ranges), Decimal(0))
                                / len(ranges)
                            )
                            if ranges
                            else None,
                            "range_contained": sum(r["contained"] for r in ranges),
                        }
                    )
    return {
        "method": METHOD,
        "summaries": summaries,
        "rows": rows,
        "score_sources": sources,
        "winner": None,
        "interpretation": "No automated ranking or superiority claim; horizons remain separate.",
    }
