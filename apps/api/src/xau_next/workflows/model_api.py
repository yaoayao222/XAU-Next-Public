"""Persistent model I/O coordinator; commits through the existing answer-import workflow."""

import hashlib
import json
from time import perf_counter

from jsonschema import Draft202012Validator
from xau_next.domains.analysis.public import API_OUTBOUND_VERSION, API_PROMPT_VERSION, experience_effect_diagnosis, DECISION_OUTBOUND_VERSION, ENTRY_OUTBOUND_VERSION, FORECAST_OUTBOUND_VERSION, estimate_cost
from xau_next.platform.jobs.public import LeaseLost
from xau_next.platform.model_gateway.public import (
    PROBE_VERSION,
    GatewayResult,
    extract,
    integrity_probe,
    redact_response,
    request_body,
    stream_failure_diagnosis,
)
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id,parse_utc
from xau_next.platform.model_gateway.lab_simulation import SimulationCredentials, simulation_gateway

TERMINAL = {"succeeded", "failed", "cancelled", "outcome_unknown"}


class ModelCoordinator:
    def __init__(self, *, db, analysis, settings, calls, jobs, gateway, clock):
        self.db, self.analysis, self.settings, self.calls, self.jobs, self.gateway, self.clock = (
            db,
            analysis,
            settings,
            calls,
            jobs,
            gateway,
            clock,
        )

    def _assert_active(self, uow, lease):
        job = self.jobs.assert_current(uow, lease, allow_cancelling=True)
        if job["state"] == "cancelling":
            raise DomainError("MODEL_CANCELLED", "模型调用已取消，迟到回答不导入。", 409)

    def lookup(self, request_id, *, scope):
        data = self.analysis.model_request(request_id, scope=scope, require_pending=False)
        return {"call": self.calls.latest(request_id, scope=scope), "version_id": data["version_id"]}

    def enqueue(self, request_id, command, *, uow, frozen_config=None, job_kind='model_call'):
        if request_id:
            result = self.lookup(request_id, scope=uow)
            previous = result["call"]
            if result["version_id"] or previous and previous["state"] not in TERMINAL:
                return result
            if previous:
                if command["retry_of"] != previous["call_id"]:
                    raise DomainError(
                        "MODEL_EXPLICIT_RETRY_REQUIRED",
                        "该请求已有失败或取消记录。请查看原因后明确重试，或使用手工导入。",
                        409,
                    )
                if previous["stage"] != "not_sent" and not command["acknowledge_possible_charge"]:
                    raise DomainError(
                        "MODEL_CHARGE_ACK_REQUIRED",
                        "前次调用可能已经计费；重试前须明确确认可能再次收费。",
                        409,
                    )
            elif command["retry_of"]:
                raise DomainError("MODEL_RETRY_MISMATCH", "重试引用不是此分析请求的最近调用。", 409)
        config = frozen_config or self.settings.snapshot(scope=uow)
        values = config["values"]
        input_started = perf_counter()
        simulated=bool(config.get('daily_simulation'))
        credentials=SimulationCredentials() if simulated else self.settings.credentials
        if not credentials.configured(config["credential_ref"]):
            raise DomainError("MODEL_KEY_REQUIRED", "请在本机设置页录入模型密钥。", 409)
        if request_id:
            if not values["enabled"]:
                raise DomainError("MODEL_DISABLED", "请在设置页启用 API 分析。", 409)
            if not frozen_config and not self.settings.verified(config, scope=uow):
                raise DomainError("MODEL_UNVERIFIED", "当前配置尚未通过用户主动启动的字符保真合成测试。", 409)
            prompt, schema, request = self.analysis.model_packet(request_id, scope=uow)
        else:
            if not command["acknowledge_possible_charge"]:
                raise DomainError(
                    "MODEL_CHARGE_ACK_REQUIRED", "合成生成测试可能产生费用，请勾选后主动启动。", 409
                )
            prompt, schema = integrity_probe()
            request = {}
            values = {
                **values,
                "max_output_tokens": min(2048, values["max_output_tokens"]),
                "timeout_seconds": min(120, values["timeout_seconds"]),
            }
            config = {**config, "values": values}
        body = request_body(values, prompt, schema)
        input_ms = round((perf_counter() - input_started) * 1000, 3)
        transform = (FORECAST_OUTBOUND_VERSION if request.get("prompt_version") == "manual-analysis/16" else
                     ENTRY_OUTBOUND_VERSION if request.get("prompt_version") == "manual-analysis/15" else
                     DECISION_OUTBOUND_VERSION if request.get("prompt_version") == "manual-analysis/14" else
                     API_OUTBOUND_VERSION if request.get("prompt_version") in {"manual-analysis/12", "manual-analysis/13"} else
                     "api-projection/5" if request.get("prompt_version") in {"manual-analysis/10", "manual-analysis/11"} else
                     "api-projection/3" if request.get("prompt_version") == "manual-analysis/9" else
                     "api-projection/2" if request.get("prompt_version") == "manual-analysis/8" else "api-projection/1") if request_id else None
        if frozen_config and request.get('prompt_version')=='manual-analysis/23':transform='api-projection/23-macro2'
        if request.get('prompt_version')=='manual-analysis/24':transform='api-projection/24-deliberation1'
        if request.get('prompt_version')=='manual-analysis/25':transform='api-projection/25-timing1'
        if request.get('prompt_version')=='manual-analysis/26':transform='api-projection/26-document1'
        if request.get('prompt_version')=='manual-analysis/27':transform='api-projection/27-member1'
        if request.get('prompt_version')=='manual-analysis/28':transform='api-projection/28-links1'
        key = credentials.read(config["credential_ref"])
        if key.encode() in body:
            raise DomainError(
                "MODEL_SECRET_IN_INPUT", "分析资料中发现本应用密钥，已阻止外发；请检查资料来源。", 409
            )
        attachment = self.calls.prepare(body)
        identity = new_id()
        job = self.jobs.enqueue(uow, job_kind, {"call_id": identity}, max_attempts=1 if frozen_config else 10)
        record = {
            "call_id": identity,
            "attempt_id": new_id(),
            "job_id": job["job_id"],
            "request_id": request_id,
            "purpose": "analysis" if request_id else "probe",
            "stage": "not_sent",
            "state": "queued",
            "config_revision": config["revision"],
            "provider": values["provider"],
            "protocol": values["protocol"],
            "model_requested": values["model_id"],
            "model_returned": None,
            **{
                field: values[field]
                for field in (
                    "output_format",
                    "stream",
                    "timeout_seconds",
                    "max_output_tokens",
                    "max_response_bytes",
                    "max_answer_bytes",
                )
            },
            "reasoning_effort": values["reasoning_effort"],
            "prompt_version": request["prompt_version"] + "+" + API_PROMPT_VERSION if request else PROBE_VERSION,
            "answer_schema_version": request.get("answer_schema_version", "synthetic/2"),
            "outbound_transform_version": transform,
            "local_timing":{"version":"model-local/1", "input_build_ms":input_ms,
                "input_bytes":len(body), "extraction_ms":None, "validation_ms":None, "import_ms":None},
            "created_at": self.clock(),
            "started_at": None,
            "completed_received_at": None,
            "ended_at": None,
            "provider_request_id": None,
            "provider_response_id": None,
            "input": self.calls.register(attachment, uow=uow),
            "raw_response": None,
            "answer": None,
            "input_tokens": None,
            "output_tokens": None,
            "estimated_cost": None,
            "cost_currency": values["price_currency"],
            "cost_source": values["price_source"],
            "error_code": None,
            "detail": None,
            "version_id": None,
            "retry_of": command["retry_of"],
            "raw_redacted": False,
            "original_response_sha256": None,
            "transport_succeeded": False,
            "answer_valid": None,
            "candidate_count": None,
        }
        self.calls.insert(record, {**config, "outbound_transform_version": transform}, uow=uow)
        return {"call": record, "version_id": None} if request_id else record

    def cancel(self, job_id, *, uow):
        job = self.jobs.cancel(job_id, scope=uow)
        call = self.calls.for_job(job_id, scope=uow)
        if call and call["state"] not in TERMINAL:
            self.calls.update(
                call["call_id"],
                {
                    "state": "cancelled",
                    "ended_at": self.clock(),
                    "error_code": "MODEL_CANCELLED",
                    "detail": "本机已取消，不再自动导入；中转站可能仍处理或计费。",
                },
                uow=uow,
            )
        return job

    def raw(self, call_id):
        with self.db.read() as tx:
            record = self.calls.get(call_id, scope=tx)
            result = {
                "call_id": call_id,
                **{
                    field: self.calls.read(record[field], scope=tx).decode("utf-8", errors="replace")
                    if record[field]
                    else None
                    for field in ("raw_response", "answer")
                },
            }
            if record['state'] == 'failed' and record.get('error_code') in {'MODEL_INCOMPLETE', 'MODEL_UPSTREAM_FAILED'} and record.get('raw_response') and not record.get('raw_redacted'):
                diagnosis = stream_failure_diagnosis(result['raw_response'].encode('utf-8'), record['protocol'])
                if diagnosis:
                    result['failure_diagnosis'] = diagnosis
            if record['state'] == 'failed' and record.get('error_code') == 'EXPERIENCE_EFFECT_MISSING' and record.get('answer') and not record.get('raw_redacted'):
                frozen = self.analysis.model_request(record['request_id'], scope=tx, require_pending=False)['request']
                diagnosis = experience_effect_diagnosis(result['answer'], frozen['context'])
                if diagnosis:
                    result['experience_effect_diagnostics'] = diagnosis
            return result

    def check(self, kind):
        snapshot = self.settings.snapshot()
        values = snapshot["values"]
        result = self._check_result(snapshot, kind)
        try:
            key = self.settings.credentials.read(snapshot["credential_ref"])
            response = self.gateway.exchange(
                {
                    **values,
                    "timeout_seconds": min(20, values["timeout_seconds"]),
                    "max_response_bytes": 1000000,
                },
                key,
                b"",
                path=kind,
            )
            if response.error_code or not response.complete or response.status != 200:
                raise DomainError(
                    response.error_code or "MODEL_CHECK_FAILED",
                    f"查询未成功（HTTP {response.status or '未知'}）；余额和权限保持未知。",
                    502,
                )
            safe_raw, redacted = redact_response(response.raw, key)
            if redacted:
                raise DomainError("MODEL_SECRET_ECHO", "查询响应包含本应用密钥，已阻止保存返回内容。", 422)
            data = json.loads(safe_raw)
            if kind == "models":
                models = data.get("data")
                if not isinstance(models, list):
                    raise ValueError("models")
                result["model_ids"] = [
                    m["id"][:200] for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)
                ][:1000]
                result["detail"] = "鉴权与模型列表查询成功；仍须单独测试生成。"
            else:
                if not isinstance(data, dict):
                    raise ValueError("usage")
                quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
                remaining = next(
                    (
                        v
                        for v in (data.get("remaining"), quota.get("remaining"), data.get("balance"))
                        if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                    ),
                    None,
                )
                unit = next((v for v in (data.get("unit"), quota.get("unit")) if isinstance(v, str)), None)
                active = next(
                    (v for v in (data.get("is_active"), data.get("isValid")) if isinstance(v, bool)), None
                )
                result.update(
                    remaining=str(remaining)[:120] if remaining is not None else None,
                    unit=unit[:80] if unit else None,
                    is_active=active,
                    detail="查询成功；缺少的余额、单位或有效性字段均为未知。",
                )
            result["status"] = "passed"
        except (DomainError, ValueError, TypeError, AttributeError) as error:
            result.update(
                status="failed",
                detail=error.detail
                if isinstance(error, DomainError)
                else "响应格式无法识别；没有假定余额、币种或有效性。",
            )
        with self.db.write() as uow:
            self.settings.record_check(result, uow=uow)
        return self.settings.view()

    def _check_result(self, snapshot, kind):
        return {
            "checked_at": self.clock(),
            "config_revision": snapshot["revision"],
            "kind": kind,
            "status": "unverified",
            "detail": "尚未验证",
            "model_ids": [],
            "remaining": None,
            "unit": None,
            "is_active": None,
            "call_id": None,
        }

    def process_once(self, workflows, *, kind='model_call'):
        lease = self.jobs.claim("api-model-"+kind, kind=kind)
        if not lease:
            self._reconcile_finished_jobs()
            return False
        call_id = lease.input_data["call_id"]
        prepared = None
        extracted = {}
        local_timing = None
        try:
            with workflows._heartbeat(lease):
                saved = self.calls.get(call_id, internal=True)
                record, config = saved["record"], saved["configuration"]
                local_timing = dict(record["local_timing"]) if record.get("local_timing") else None
                values = config["values"]
                simulated=bool(config.get('daily_simulation'))
                credentials=SimulationCredentials() if simulated else self.settings.credentials
                gateway=self.gateway
                if simulated:
                    with self.db.read() as tx:
                        data=self.analysis.model_request(record['request_id'],scope=tx,require_pending=False)
                    if data['request']['context']['market']['source_kind']!='synthetic':
                        raise DomainError('DAILY_SIMULATION_SOURCE','模拟记录来源不符，未发送。',409)
                    gateway=simulation_gateway(data['request'])
                if record["stage"] == "sent_waiting":
                    raise DomainError(
                        "MODEL_OUTCOME_UNKNOWN", "上次发送后未保存完整响应，结果未知；不会自动重发。", 409
                    )
                if record["state"] == "cancelled":
                    self.jobs.acknowledge_cancel(lease)
                    return True
                if record["purpose"] == "probe" and record["prompt_version"] != PROBE_VERSION:
                    raise DomainError(
                        "MODEL_PROBE_POLICY_CHANGED",
                        "该合成测试使用旧版规则，不能证明字符保真；原记录保留，请在设置页主动启动新版测试。", 409,
                    )
                if record["stage"] == "not_sent":
                    if config.get('daily_group_id') and not simulated:
                        with self.db.read() as tx:
                            frozen=self.analysis.model_request(record['request_id'],scope=tx,require_pending=False)['request']
                        if not 0<=(parse_utc(self.clock())-parse_utc(frozen['market_cutoff'])).total_seconds()<=60:
                            raise DomainError('DAILY_DISPATCH_STALE','资料已超过60秒派发期限，未读取密钥或自动换包。',409)
                    if config.get('daily_profile_snapshot'):
                        from .model_selection import digest
                        old=config['daily_profile_snapshot']
                        if digest(self.settings.comparison_profile(old['profile_id']))!=digest(old):
                            raise DomainError('DAILY_PROFILE_CHANGED','封存的连接配置已经变化，本次没有换配置或重发。',409)
                    key = credentials.read(config["credential_ref"])
                    body = self.calls.read(record["input"])
                    with self.db.write() as uow:
                        self._assert_active(uow, lease)
                        if record["request_id"]:
                            data = self.analysis.model_request(record["request_id"], scope=uow)
                            if data["version_id"]:
                                self.calls.update(
                                    call_id,
                                    {
                                        "state": "succeeded",
                                        "stage": "imported",
                                        "version_id": data["version_id"],
                                        "ended_at": self.clock(),
                                        "detail": "请求已通过其他通道导入，未发送模型请求。",
                                    },
                                    uow=uow,
                                )
                                self.jobs.finish(
                                    uow, lease, {"kind": "forecast", "id": data["request"]["forecast_id"]}
                                )
                                return True
                        self.calls.reserve(values, len(body), uow=uow)
                        self.calls.update(
                            call_id,
                            {"stage": "sent_waiting", "state": "waiting", "started_at": self.clock()},
                            uow=uow,
                        )

                    def stopped():
                        if workflows._stop.is_set():
                            return True
                        try:
                            return self.jobs.inspect_lease(lease)["state"] == "cancelling"
                        except LeaseLost:
                            return True

                    def receiving():
                        with self.db.write() as uow:
                            job = self.jobs.assert_current(uow, lease, allow_cancelling=True)
                            if job["state"] != "cancelling":
                                self.calls.update(call_id, {"state": "receiving"}, uow=uow)

                    response = gateway.exchange(values, key, body, stopped=stopped, receiving=receiving)
                    received_at = self.clock() if response.complete else None
                    digest = hashlib.sha256(response.raw).hexdigest()
                    response.raw, redacted = redact_response(response.raw, key)
                    response.request_id = (
                        response.request_id.replace(key, "[REDACTED]") if response.request_id else None
                    )
                    raw = self.calls.prepare(response.raw)
                    with self.db.write() as uow:
                        job = self.jobs.assert_current(uow, lease, allow_cancelling=True)
                        patch = {
                            "raw_response": self.calls.register(raw, uow=uow),
                            "raw_redacted": redacted,
                            "original_response_sha256": digest,
                            "provider_request_id": response.request_id,
                            "transport_succeeded": response.complete and response.status == 200,
                        }
                        if response.complete:
                            patch.update(stage="response_saved", completed_received_at=received_at)
                        if job["state"] != "cancelling":
                            patch["state"] = "validating"
                        self.calls.update(
                            call_id,
                            patch,
                            uow=uow,
                            response_meta={k: v for k, v in vars(response).items() if k != "raw"},
                        )
                else:
                    response = GatewayResult(
                        raw=self.calls.read(record["raw_response"]), **saved["response_meta"]
                    )
                    redacted = record["raw_redacted"]
                if redacted:
                    raise DomainError(
                        "MODEL_SECRET_ECHO",
                        "响应包含本应用密钥，已脱敏保存并停止导入；请在中转站撤销泄露密钥后重新配置。",
                        422,
                    )
                local_started = perf_counter()
                try:
                    extracted = extract(response, values["protocol"])
                finally:
                    if local_timing is not None:
                        local_timing["extraction_ms"] = round((perf_counter()-local_started)*1000,3)
                answer_bytes = extracted.pop("answer").encode("utf-8")
                if config.get('daily_group_id') and extracted.get('model_returned') not in config['daily_accepted_models']:
                    raise DomainError('DAILY_RETURNED_MODEL_MISMATCH','返回模型声明与本次选择不符，原始响应保留，未导入。',409)
                if len(answer_bytes) > values.get("max_answer_bytes", 2000000):
                    error = DomainError(
                        "MODEL_ANSWER_TOO_LARGE", "最终答案超过独立字节上限；完整原响应已保留，未导入。", 422
                    )
                    error.model_metadata = extracted
                    raise error
                answer = self.calls.prepare(answer_bytes)
                with self.db.write() as uow:
                    self._assert_active(uow, lease)
                    self.calls.update(
                        call_id,
                        {
                            **extracted,
                            "answer": self.calls.register(answer, uow=uow),
                            "state": "validating",
                            **({"local_timing":local_timing} if local_timing else {}),
                            "estimated_cost": estimate_cost(
                                values, extracted["input_tokens"], extracted["output_tokens"]
                            ),
                        },
                        uow=uow,
                    )
                raw_answer = self.calls.read({"id": answer.id}).decode("utf-8")
                if record["purpose"] == "probe":
                    try:
                        parsed = json.loads(raw_answer)
                    except (ValueError, RecursionError) as exc:
                        raise DomainError(
                            "MODEL_PROBE_INVALID", "合成回答不是有效 JSON；字符保真未通过，原文已保留。", 422
                        ) from exc
                    errors = list(Draft202012Validator(integrity_probe()[1]).iter_errors(parsed))
                    if errors:
                        raise DomainError(
                            "MODEL_PROBE_INVALID", "合成回答的结构或字符值不符；日期、引用、负数及中文须完整保留，原文未修补。", 422,
                            [{"path": "/" + "/".join(str(item) for item in error.absolute_path),
                              "code": "MODEL_PROBE_INVALID", "message": "字段须与合成测试的指定值完全一致。"}
                             for error in errors[:50]],
                        )
                    with self.db.write() as uow:
                        self._assert_active(uow, lease)
                        self.calls.update(
                            call_id,
                            {
                                "stage": "tested",
                                "state": "succeeded",
                                "answer_valid": True,
                                "ended_at": self.clock(),
                            },
                            uow=uow,
                        )
                        check = self._check_result(config, "probe")
                        check.update(
                            status="passed",
                            call_id=call_id,
                            detail="SYNTHETIC 字符保真测试通过：日期、引用符号、负数及中文与指定值一致。仅证明本次小型测试；长回答质量、参数实际执行强度和底层模型身份未获独立验证。",
                        )
                        self.settings.record_check(check, uow=uow, validation_policy=record["prompt_version"])
                        self.jobs.finish(uow, lease, None)
                else:
                    local_started = perf_counter()
                    prepared = self.analysis.prepare_import(
                        {
                            "request_id": record["request_id"],
                            "raw_text": raw_answer,
                            "source": "model_api",
                            "provider": values["provider"],
                            "model_id": extracted["model_returned"] or values["model_id"],
                            "reasoning_effort": values["reasoning_effort"]
                            if values["reasoning_effort"] != "omit"
                            else "unknown",
                            "analysis_platform": "model_api",
                        }
                    )
                    if local_timing is not None:
                        local_timing["validation_ms"] = round((perf_counter()-local_started)*1000,3)
                    with self.db.write() as uow:
                        self._assert_active(uow, lease)
                        current = self.analysis.model_request(record["request_id"], scope=uow)
                        if current["version_id"]:
                            raise DomainError(
                                "MODEL_REQUEST_ALREADY_IMPORTED",
                                "等待期间请求已由其他回答导入，模型原文保留，未覆盖版本。",
                                409,
                            )
                        local_started = perf_counter()
                        version = workflows.commit_answer(prepared, uow=uow)
                        if local_timing is not None:
                            local_timing["import_ms"] = round((perf_counter()-local_started)*1000,3)
                        self.calls.update(
                            call_id,
                            {
                                "stage": "imported",
                                "state": "succeeded",
                                "answer_valid": True,
                                "candidate_count": len(version["answer"]["plans"]),
                                "version_id": version["version_id"],
                                "ended_at": self.clock(),
                                **({"local_timing":local_timing} if local_timing else {}),
                            },
                            uow=uow,
                        )
                        self.jobs.finish(uow, lease, {"kind": "forecast", "id": version["forecast_id"]})
        except LeaseLost:
            pass  # A successor owns recovery. The stale worker must not write any business result.
        except Exception as error:  # noqa: BLE001 - never serialize unexpected exceptions containing credentials
            # Never include arbitrary exception strings or request objects in logs/errors.
            error = (
                error
                if isinstance(error, DomainError)
                else DomainError(
                    "MODEL_LOCAL_FAILURE", "模型通道本地处理失败；请查看已保存的阶段和原文。", 500
                )
            )
            try:
                with self.db.write() as uow:
                    job = self.jobs.assert_current(uow, lease, allow_cancelling=True)
                    row = self.calls.get(call_id, scope=uow, internal=True)
                    rec = row["record"]
                    cancelled = job["state"] == "cancelling" or rec["state"] == "cancelled"
                    unknown = rec["stage"] == "sent_waiting"
                    patch = {
                        "state": "cancelled" if cancelled else "outcome_unknown" if unknown else "failed",
                        "error_code": "MODEL_CANCELLED" if cancelled else error.code,
                        "detail": "本机已取消，迟到回答不导入；不保证中转站停止或不计费。"
                        if cancelled
                        else error.detail,
                        "ended_at": self.clock(),
                        **({"local_timing":local_timing} if local_timing else {}),
                    }
                    metadata = getattr(error, "model_metadata", {}) or extracted
                    if metadata:
                        patch.update({k: v for k, v in metadata.items() if k != "answer"})
                        patch["estimated_cost"] = estimate_cost(
                            row["configuration"]["values"],
                            metadata.get("input_tokens"),
                            metadata.get("output_tokens"),
                        )
                    if prepared and prepared.error:
                        patch["answer_valid"] = False
                        self.analysis.record_failed_attempt(prepared, error, uow=uow)
                    if rec["purpose"] == "probe" and error.code == "MODEL_PROBE_INVALID":
                        patch["answer_valid"] = False
                    self.calls.update(call_id, patch, uow=uow)
                    if rec["purpose"] == "probe":
                        check = self._check_result(row["configuration"], "probe")
                        check.update(status="failed", call_id=call_id, detail=patch["detail"])
                        self.settings.record_check(check, uow=uow, validation_policy=rec["prompt_version"])
                    self.jobs.fail(
                        lease,
                        {
                            "type": "about:blank",
                            "title": "模型调用未完成",
                            "status": error.status,
                            "code": patch["error_code"],
                            "detail": patch["detail"],
                            "trace_id": new_id(),
                            "field_errors": error.field_errors,
                            "retryable": False,
                        },
                        scope=uow,
                    )
            except LeaseLost:
                pass
        return True

    def _reconcile_finished_jobs(self):
        with self.db.read() as scope:
            identities = [
                call["call_id"]
                for call in self.calls.pending(scope=scope)
                if (self.jobs.get(scope, call["job_id"]) or {}).get("state") in ("failed", "cancelled")
            ]
        if not identities:
            return
        with self.db.write() as uow:
            for identity in identities:
                record = self.calls.get(identity, scope=uow)
                job = self.jobs.get(uow, record["job_id"])
                if record["state"] in TERMINAL or job["state"] not in ("failed", "cancelled"):
                    continue
                error = job.get("error") or {}
                self.calls.update(
                    identity,
                    {
                        "state": "cancelled"
                        if job["state"] == "cancelled"
                        else "outcome_unknown"
                        if record["stage"] == "sent_waiting"
                        else "failed",
                        "error_code": error.get("code", "MODEL_CANCELLED"),
                        "detail": error.get("detail", "后台任务已取消，未导入模型回答。"),
                        "ended_at": self.clock(),
                    },
                    uow=uow,
                )
