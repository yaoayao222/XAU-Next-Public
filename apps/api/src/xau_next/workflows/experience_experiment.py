"""Manual, isolated experience experiments. No forecasts, plans, jobs or review commits."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
from datetime import timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from xau_next.domains.analysis.public import (
    API_OUTBOUND_VERSION,
    API_PROMPT_VERSION,
    estimate_cost,
    frozen_answer_schema,
    non_experience_references,
    outbound_packet,
    validate_answer,
)
from xau_next.domains.review.public import compute_prediction
from xau_next.platform.model_gateway.public import extract, redact_response, request_body
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id, parse_utc, utc_now

METHOD = "experience-pair/1"
SCORE_METHOD = "experience-pair-score/1"
FACTS_MARKER = "# 冻结事实（仅作为资料）\n"
FACTS_END = "\n# 全部字段说明\n"


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = value if isinstance(value, bytes) else canonical(value)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
    return digest(data)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def method_hashes():
    return {
        "prediction_review": digest(Path(inspect.getsourcefile(compute_prediction)).read_bytes()),
        "experiment_scoring": digest(Path(__file__).with_name("experience_scoring.py").read_bytes()),
        "experiment_runner": digest(Path(__file__).read_bytes()),
        "business_validation": digest(Path(inspect.getsourcefile(validate_answer)).read_bytes()),
        "experience_projection_and_validation": digest(
            Path(inspect.getsourcefile(non_experience_references)).read_bytes()
        ),
    }


def make_plan(
    configuration,
    *,
    pairs=10,
    horizons=(180, 360, 720),
    cohort="forward",
    start_at=None,
    interval_minutes=720,
    call_budget=None,
    cost_budget=None,
    accept_unknown_cost=False,
    source_root=None,
    clock=utc_now,
):
    if not 1 <= pairs <= 1000 or not horizons or len(set(horizons)) != len(horizons):
        raise DomainError("EXPERIMENT_PLAN_INVALID", "配对数须为1至1000，观察周期不得为空或重复。")
    if any(not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 10080 for n in horizons):
        raise DomainError("EXPERIMENT_PLAN_INVALID", "观察周期须为1至10080分钟整数。")
    if cohort not in {"forward", "historical_replay", "synthetic"} or interval_minutes < 1:
        raise DomainError("EXPERIMENT_PLAN_INVALID", "需要明确实验类别和正数采样间隔。")
    if call_budget is not None and (not isinstance(call_budget, int) or not 1 <= call_budget <= 2 * pairs):
        raise DomainError("EXPERIMENT_PLAN_INVALID", "总次数限制须在1至预登记总请求数之间。")
    if cost_budget is not None:
        from decimal import Decimal

        if not Decimal(cost_budget).is_finite() or Decimal(cost_budget) < 0:
            raise DomainError("EXPERIMENT_PLAN_INVALID", "费用上限须为非负有限值。")
    start = parse_utc(start_at) if start_at else None
    plan = {
        "method_version": METHOD,
        "score_method_version": SCORE_METHOD,
        "experiment_id": "experience-exp-" + new_id(),
        "created_at": clock(),
        "configuration": copy.deepcopy(configuration),
        "cohort": cohort,
        "source_root_sha256": digest(str(Path(source_root).resolve()).casefold().encode())
        if source_root
        else None,
        "horizon_minutes": sorted(horizons),
        "pair_count": pairs,
        "planned_requests": pairs * 2,
        "call_budget": call_budget,
        "cost_budget": cost_budget,
        "accept_unknown_cost": accept_unknown_cost,
        "order_rule": "alternating-AB-BA/1",
        "sample_interval_minutes": interval_minutes,
        "selection_rule": "each preregistered slot: manually freeze the first selected new request within [planned_at, planned_at+5min]; no automatic sampling",
        "neutral_band": "0.00",
        "price_basis": "closed-bid-M1",
        "method_hashes": method_hashes(),
        "primary_metric": "direction_correct_by_horizon",
        "parallel_metrics": [
            "lower_boundary_absolute_error",
            "upper_boundary_absolute_error",
            "range_boundary_absolute_error",
            "range_contained",
            "predicted_range_width",
            "actual_range_width",
            "phase.direction_correct",
            "turning_point_hit",
            "frozen_baselines",
        ],
        "unsupported_metrics": [
            "exact_turning_time_error",
            "exact_turning_price_error",
            "phase_sequence_score",
        ],
        "scopes": ["full_descriptive", "after_own_available", "after_common_available"],
        "failure_policy": "single attempt per arm; stop remaining arm after failure; all registered arms remain denominators; no automatic retries",
        "estimand": "本次直接加入experience的增量；previous_analysis保留，B组可能间接受历史经验影响。",
        "conclusion_limit": "流程试验不证明稳定优越性；技术重复、同轮版本及重叠窗口不构成新的独立样本。",
        "samples": [
            {
                "sample_id": f"pair-{i + 1:04d}",
                "index": i,
                "planned_at": (start + timedelta(minutes=i * interval_minutes))
                .isoformat()
                .replace("+00:00", "Z")
                if start
                else None,
                "order": ["A", "B"] if i % 2 == 0 else ["B", "A"],
            }
            for i in range(pairs)
        ],
    }
    return plan


def create_plan(root, plan):
    path = Path(root)
    if path.exists() and any(path.iterdir()):
        raise DomainError("EXPERIMENT_EXISTS", "实验目录须不存在或为空；不会覆盖原证据。", 409)
    save(path / "plan.json", plan)
    save(path / "plan.sha256", digest(plan).encode())
    return digest(plan)


def load_plan(root):
    root = Path(root)
    plan = read(root / "plan.json")
    if digest(plan) != (root / "plan.sha256").read_text().strip():
        raise DomainError("EXPERIMENT_PLAN_CHANGED", "冻结计划哈希不符。", 409)
    if plan["method_version"] != METHOD or plan["score_method_version"] != SCORE_METHOD:
        raise DomainError("EXPERIMENT_METHOD_CHANGED", "实验方法版本不同，不能按新方法重解释旧结果。", 409)
    return plan


def slot(plan, sample_id):
    found = next((s for s in plan["samples"] if s["sample_id"] == sample_id), None)
    if not found:
        raise DomainError("EXPERIMENT_SAMPLE_UNKNOWN", "样本不在预登记计划内。", 404)
    return found


def prompt_facts(prompt):
    from xau_next.domains.analysis.public import expand_experience
    result = json.JSONDecoder().raw_decode(prompt.split(FACTS_MARKER, 1)[1].lstrip())[0]
    if result['request'].get('prompt_version') in {"manual-analysis/10", "manual-analysis/11"}:
        result['experience'] = expand_experience(result.get('experience'))
    return result


def paired_bodies(request, configuration):
    """Retain the exact frozen instructions and schema; only change the treatment facts."""
    schema = frozen_answer_schema(request)
    schema["properties"]["schema_version"] = {"type": "string", "const": request["answer_schema_version"]}
    original = request["packet_text"]
    prefix, tail = original.split(FACTS_MARKER, 1)
    fact_text, suffix = tail.split(FACTS_END, 1)
    facts = json.loads(fact_text)
    facts["request"].update({key: request[key] for key in ("request_id", "forecast_id")})
    if not facts.get("experience") or not facts["experience"].get("cases"):
        raise DomainError(
            "EXPERIMENT_NO_EXPERIENCE", "配对必须实际存在经验案例；无经验样本保留未发送状态。", 409
        )
    contexts, bodies, prompts = {}, {}, {}
    for arm in ("A", "B"):
        current = copy.deepcopy(facts)
        context = copy.deepcopy(request["context"])
        if arm == "B":
            refs = non_experience_references(current)
            current["experience"] = None
            current["evidence_refs"] = [ref for ref in current["evidence_refs"] if ref in refs]
            context["experience"] = None
            context["evidence_refs"] = current["evidence_refs"]
            if request["prompt_version"] in {"manual-analysis/10", "manual-analysis/11"}:
                current["experience_cohort"] = context["experience_cohort"] = {
                    **current["experience_cohort"], "case_count":0,"unique_forecast_versions":0,
                    "cases_without_forecast_identity":0,"overlap_components":0}
        text = prefix + FACTS_MARKER + json.dumps(current, ensure_ascii=False) + FACTS_END + suffix
        prompt = outbound_packet(text)
        terminal = context.get("settings", {}).get("values", {}).get("connection", {}).get("terminal_path")
        if terminal:
            prompt = prompt.replace(terminal, "[本机路径已移除]").replace(
                json.dumps(terminal)[1:-1], "[本机路径已移除]"
            )
        prompts[arm], contexts[arm] = prompt, context
        bodies[arm] = request_body(configuration, prompt, schema)
    a, b = (prompt_facts(prompts[arm]) for arm in ("A", "B"))
    expected_b = copy.deepcopy(a)
    expected_b["experience"] = None
    expected_b["evidence_refs"] = [r for r in a["evidence_refs"] if r in non_experience_references(a)]
    if request["prompt_version"] in {"manual-analysis/10", "manual-analysis/11"}:
        expected_b["experience_cohort"] = contexts["B"]["experience_cohort"]
    if expected_b != b:
        raise DomainError("EXPERIMENT_INPUT_DIFF", "经验之外的事实差异超出允许范围。", 409)
    outside = []
    for arm in ("A", "B"):
        body = json.loads(bodies[arm])
        text = body.pop("input", None)
        if text is None:
            messages = body.pop("messages")
            assert len(messages) == 1 and messages[0]["role"] == "user"
        p, rest = prompts[arm].split(FACTS_MARKER, 1)
        _, end = json.JSONDecoder().raw_decode(rest)
        outside.append([body, p, rest[end:]])
    if outside[0] != outside[1]:
        raise DomainError("EXPERIMENT_INPUT_DIFF", "提示、Schema或调用参数发生了非经验差异。", 409)
    return (
        schema,
        contexts,
        bodies,
        {
            "status": "PASS",
            "allowed_paths": ["/facts/experience", "/facts/evidence_refs"],
            "removed_exclusive_refs": [r for r in a["evidence_refs"] if r not in b["evidence_refs"]],
            "non_experience_facts_equal": True,
            "instructions_schema_and_parameters_equal": True,
            "experience_sha256": digest(a["experience"]),
            "body_sha256": {arm: digest(body) for arm, body in bodies.items()},
        },
    )


def freeze_pair(root, sample_id, source_request, *, clock=utc_now):
    root = Path(root)
    plan = load_plan(root)
    selected = slot(plan, sample_id)
    request = copy.deepcopy(source_request)
    if (request["answer_schema_version"], request["prompt_version"]) not in {("1.4.0", "manual-analysis/8"), ("1.5.0", "manual-analysis/9"), ("1.6.0", "manual-analysis/10"), ("1.6.0", "manual-analysis/11")}:
        raise DomainError(
            "EXPERIMENT_REQUEST_VERSION", "此实验要求支持的冻结1.4/1.5/1.6请求；不重写旧包或擅自升级原请求。", 409
        )
    if request["kind"] != "new" or sorted(request["horizon_minutes"]) != plan["horizon_minutes"]:
        raise DomainError("EXPERIMENT_WINDOW_MISMATCH", "每对使用新分析且周期与预登记一致。", 409)
    if selected["planned_at"]:
        delay = (parse_utc(request["market_cutoff"]) - parse_utc(selected["planned_at"])).total_seconds()
        if not 0 <= delay <= 300:
            raise DomainError("EXPERIMENT_SAMPLING_WINDOW", "资料截止点不在本预登记采样点后的5分钟内。", 409)
    if plan["cohort"] == "forward" and (
        not selected["planned_at"]
        or not parse_utc(plan["created_at"])
        <= parse_utc(request["market_cutoff"])
        <= parse_utc(clock())
        < parse_utc(request["window_end"])
    ):
        raise DomainError("EXPERIMENT_NOT_FORWARD", "前向实验需先登记计划，并在实际窗口结束前冻结资料。", 409)
    if plan["cohort"] != "synthetic" and request["context"]["market"]["source_kind"] == "synthetic":
        raise DomainError(
            "EXPERIMENT_SYNTHETIC_COHORT", "合成输入必须单列synthetic类别，不能充当真实历史或前向样本。", 409
        )
    identity = plan["experiment_id"] + ":" + sample_id
    source_identity = {key: request[key] for key in ("request_id", "forecast_id", "market_snapshot_id")}
    request.update(request_id=identity + ":request", forecast_id=identity + ":forecast")
    schema, contexts, bodies, diff = paired_bodies(request, plan["configuration"])
    folder = root / sample_id
    if folder.exists():
        raise DomainError("EXPERIMENT_SAMPLE_FROZEN", "该样本已有冻结或中断证据，不能覆盖。", 409)
    hashes = {
        "base-request.json": save(folder / "base-request.json", source_request),
        "schema.json": save(folder / "schema.json", schema),
    }
    request.pop("context")
    request.pop("packet_text")
    hashes["request.json"] = save(folder / "request.json", request)
    for arm in ("A", "B"):
        hashes[arm + "-context.json"] = save(folder / (arm + "-context.json"), contexts[arm])
        hashes[arm + "-body.json"] = save(folder / (arm + "-body.json"), bodies[arm])
    hashes["input-diff.json"] = save(folder / "input-diff.json", diff)
    market = source_request["context"]["market"]
    metadata = {
        "sample_id": sample_id,
        "frozen_at": clock(),
        "source_identity": source_identity,
        "source_group": digest([market.get(k) for k in ("account_scope_id", "source_kind", "symbol")]),
        "source_kind": market["source_kind"],
        "window_start": source_request["window_start"],
        "window_end": source_request["window_end"],
        "market_cutoff": source_request["market_cutoff"],
        "technical_repeat_group": digest(
            {
                "context": source_request["context"],
                "market_cutoff": source_request["market_cutoff"],
                "window_start": source_request["window_start"],
                "window_end": source_request["window_end"],
            }
        ),
        "original_window_id": source_identity["forecast_id"],
        "time_block": source_request["market_cutoff"][:13],
        "prompt_version": source_request["prompt_version"],
        "api_prompt_version": API_PROMPT_VERSION,
        "answer_schema_version": source_request["answer_schema_version"],
        "outbound_transform_version": API_OUTBOUND_VERSION,
        "estimated_input_tokens_upper_proxy": {a: len(bodies[a]) for a in bodies},
        "estimated_max_cost": {
            a: estimate_cost(
                plan["configuration"], len(bodies[a]), plan["configuration"]["max_output_tokens"]
            )
            for a in bodies
        },
        "files": hashes,
    }
    save(folder / "frozen.json", metadata)
    return metadata


def load_pair(root, sample_id):
    root = Path(root)
    plan = load_plan(root)
    slot(plan, sample_id)
    folder = root / sample_id
    frozen = read(folder / "frozen.json")
    for name, expected in frozen["files"].items():
        if digest((folder / name).read_bytes()) != expected:
            raise DomainError("EXPERIMENT_INPUT_CHANGED", "冻结输入哈希不符，不能执行或评分。", 409)
    return plan, frozen


def validate_output(raw, schema, request, context):
    result = {
        "schema_valid": False,
        "business_valid": False,
        "experience_error": False,
        "error_code": None,
        "field_errors": [],
    }

    def unique(pairs):
        answer = {}
        for key, value in pairs:
            if key in answer:
                raise ValueError("duplicate key")
            answer[key] = value
        return answer

    try:
        answer = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")),
        )
        errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(answer))
        if errors:
            result["field_errors"] = [
                {"path": "/" + "/".join(map(str, e.absolute_path)), "message": e.message[:300]}
                for e in errors[:30]
            ]
            result["error_code"] = "SCHEMA_INVALID"
            result["experience_error"] = any(
                e.validator == "required"
                and any(k not in answer for k in ("experience_application", "prediction_adjustments"))
                or any(
                    str(p) in {"experience_application", "prediction_adjustments"} for p in e.absolute_path
                )
                for e in errors
            )
            return result
        result["schema_valid"] = True
        validate_answer(answer, schema, request, context)
        result["business_valid"] = True
    except DomainError as error:
        result.update(
            error_code=error.code,
            field_errors=error.field_errors,
            experience_error=error.code.startswith("EXPERIENCE_"),
        )
    except (ValueError, TypeError, RecursionError):
        result["error_code"] = "SCHEMA_INVALID"
    return result


def run_pair(root, sample_id, *, calls, settings, gateway, acknowledge_plan_sha256, clock=utc_now):
    """Explicit one-pair command only. Global DB claims plus frozen plan prevent repeat sends."""
    root = Path(root)
    plan, frozen = load_pair(root, sample_id)
    selected = slot(plan, sample_id)
    if digest(plan) != acknowledge_plan_sha256:
        raise DomainError("EXPERIMENT_START_REQUIRED", "主动启动须提供已审阅的计划SHA256。", 409)
    if plan["source_root_sha256"] != digest(str(calls.db.data_root).casefold().encode()):
        raise DomainError(
            "EXPERIMENT_BUDGET_ROOT", "必须在冻结的真实用量目录计次；不能换空目录绕过预算。", 409
        )
    if plan["method_hashes"] != method_hashes():
        raise DomainError("EXPERIMENT_METHOD_CHANGED", "调用前固定的评分实现已变化。", 409)
    config = settings.snapshot()
    if config["values"] != plan["configuration"]:
        raise DomainError(
            "EXPERIMENT_CONFIG_CHANGED", "当前模型参数或预算与冻结计划不同；不会自动替换。", 409
        )
    with calls.db.read() as tx:
        if not config["values"]["enabled"] or not settings.verified(config, scope=tx):
            raise DomainError("MODEL_UNVERIFIED", "当前设置未启用或未通过现有字符保真验证。", 409)
    if not settings.credentials.configured(config["credential_ref"]):
        raise DomainError("MODEL_KEY_REQUIRED", "本应用没有可用密钥；未发送。", 409)
    key = settings.credentials.read(config["credential_ref"])
    folder = root / sample_id
    # Append-only start token; a crash or second process cannot silently restart the pair.
    save(folder / "started.json", {"at": clock(), "plan_sha256": acknowledge_plan_sha256})
    results = {}
    for index, arm in enumerate(selected["order"]):
        result = {
            "experiment_id": plan["experiment_id"],
            "sample_id": sample_id,
            "arm": arm,
            "state": "not_sent",
            "error_code": None,
            "schema_valid": False,
            "business_valid": False,
            "experience_error": False,
            "started_at": None,
            "completed_received_at": None,
            "available_at": None,
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost": None,
            "billing_cost": None,
            "billing_cost_status": "unknown_provider_invoice_not_supplied",
            "currency": config["values"]["price_currency"],
        }
        if index and results[selected["order"][0]]["state"] != "succeeded":
            result["error_code"] = "STOPPED_AFTER_OTHER_ARM_FAILURE"
            save(folder / (arm + "-result.json"), result)
            results[arm] = result
            continue
        body = (folder / (arm + "-body.json")).read_bytes()
        if key.encode() in body:
            raise DomainError("MODEL_SECRET_IN_INPUT", "资料包含本应用密钥，已阻止外发。", 409)
        identity = None
        try:
            if plan["cohort"] == "forward" and parse_utc(clock()) >= parse_utc(frozen["window_end"]):
                raise DomainError("EXPERIMENT_WINDOW_ENDED", "前向窗口已结束，未补发历史调用。", 409)
            with calls.db.write() as tx:
                if settings.snapshot(scope=tx) != config:
                    raise DomainError(
                        "EXPERIMENT_CONFIG_CHANGED", "调用前设置发生变化，保留原组状态；未自动继续。", 409
                    )
                identity = calls.reserve_experiment(plan, sample_id, arm, len(body), uow=tx)
            result.update(state="outcome_unknown", started_at=clock())
            save(
                folder / (arm + "-sent.json"),
                {"claimed_id": identity, "at": result["started_at"], "body_sha256": digest(body)},
            )
            started = time.monotonic()
            response = gateway.exchange(config["values"], key, body)
            result.update(
                latency_seconds=round(time.monotonic() - started, 3),
                completed_received_at=clock() if response.complete else None,
                http_status=response.status,
                provider_request_id=response.request_id,
                diagnostics=response.diagnostics,
            )
            safe, redacted = redact_response(response.raw, key)
            result.update(
                raw_sha256=save(folder / (arm + "-raw.bin"), safe),
                original_raw_sha256=digest(response.raw),
                raw_redacted=redacted,
            )
            output = extract(response, config["values"]["protocol"])
            raw = output.pop("answer")
            result.update(output)
            raw_bytes, answer_redacted = redact_response(raw.encode(), key)
            result["answer_sha256"] = save(folder / (arm + "-answer.json"), raw_bytes)
            if redacted or answer_redacted:
                raise DomainError(
                    "MODEL_SECRET_IN_RESPONSE", "原响应出现密钥，已脱敏并拒绝作为有效实验回答。", 422
                )
            if len(raw_bytes) > config["values"]["max_answer_bytes"]:
                raise DomainError("ANSWER_TOO_LARGE", "回答超过冻结字节上限。", 422)
            validation = validate_output(
                raw,
                read(folder / "schema.json"),
                read(folder / "request.json"),
                read(folder / (arm + "-context.json")),
            )
            result.update(validation)
            result.update(
                state="succeeded" if validation["business_valid"] else "validation_failed",
                available_at=clock() if validation["business_valid"] else None,
            )
        except DomainError as error:
            result.update(getattr(error, "model_metadata", {}))
            result.update(
                error_code=error.code,
                state="outcome_unknown"
                if identity and ("UNKNOWN" in error.code or error.code == "MODEL_CONNECTION_UNKNOWN")
                else "failed"
                if identity
                else "not_sent",
            )
        result["estimated_cost"] = estimate_cost(
            config["values"], result["input_tokens"], result["output_tokens"]
        )
        result["ended_at"] = clock()
        save(folder / (arm + "-result.json"), result)
        if identity:
            with calls.db.write() as tx:
                calls.finish_experiment(identity, result, uow=tx)
        results[arm] = result
    return results
