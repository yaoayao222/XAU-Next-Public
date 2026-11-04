from __future__ import annotations

from xau_next.shared.preparation_timing import preparation_span

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from xau_next.platform.db.public import Database, ReadScope, UnitOfWork
from xau_next.platform.files.public import AttachmentStore, PreparedAttachment
from xau_next.shared.analysis_policy import analysis_preferences, default_analysis_horizons, new_analysis_preferences
from xau_next.shared.near_term import build_near_term
from xau_next.shared.experience_similarity import market_signature
from xau_next.shared.scalp import assess_scalp
from xau_next.shared.entry_timing import entry_timing, timing_reference
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import new_id, parse_utc, utc_now

from .model_api import outbound_packet
from .package import ANSWER_SCHEMA_VERSION, PROMPT_VERSION, build_packet, frozen_answer_schema
from .refresh import refresh_status
from .quality import new_quality_context
from .statistics import build_statistics, decimal
from .validation import failure, validate_answer
from .json_document import parse_document
from .holding_reviews import HoldingReviews

REQUEST_FIELDS = ("request_id", "forecast_id", "kind", "base_version_id", "market_snapshot_id",
                  "market_cutoff", "window_start", "window_end", "horizon_minutes", "created_at", "state",
                  "answer_schema_version", "packet_text", "prompt_version")
VERSION_FIELDS = ("version_id", "forecast_id", "sequence", "request_id", "previous_version_id", "available_at",
                  "recorded_at", "answer", "origin")
FORECAST_FIELDS = ("forecast_id", "current_version_id", "window_start", "window_end", "summary",
                   "available_at", "revision", "horizon_minutes")
SOURCE_FIELDS = ("account_scope_id", "symbol", "source_kind")


@dataclass(frozen=True)
class PreparedImport:
    request_id: str
    raw_text: str
    attachment: PreparedAttachment
    origin: Snapshot
    answer: Snapshot | None
    error: DomainError | None


@dataclass(frozen=True)
class ImportCommit:
    version: Snapshot
    plans: tuple[Snapshot, ...]
    repeated: bool


def _data(value):
    return value.to_dict() if isinstance(value, Snapshot) else json.loads(json.dumps(value))


def _clean(record):
    return {key: value for key, value in record.items() if key not in ("id", "revision", "recorded_at")}


def _time(value):
    return value.isoformat().replace("+00:00", "Z")


def market_scope_key(market: Snapshot | dict) -> str:
    value = _data(market)
    identity = json.dumps([value[key] for key in SOURCE_FIELDS], ensure_ascii=False, separators=(",", ":"))
    return "market-scope-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


class AnalysisService(HoldingReviews):
    """Analysis owns requests/answers only; callers register plans in the same opaque UoW."""

    refresh_status = staticmethod(refresh_status)

    def model_request(self, request_id, *, scope, require_pending=True):
        row = self._required(scope,"request",request_id)
        previous = self._docs.get(scope,"request_result",request_id)
        if previous:
            return {"request":row,"version_id":previous["version_id"]}
        if require_pending:
            current = self._request_snapshot(scope,row).to_dict()
            if current["state"] != "awaiting_answer":
                raise DomainError("MODEL_REQUEST_UNAVAILABLE","请求已结束或基础版本已变化，请准备新请求。",409)
            preferences = analysis_preferences(row.get("context",{}).get("settings",{}).get("values",{}))
            minutes = min(preferences[style + "_refresh_minutes"] for style in preferences["enabled_styles"])
            if parse_utc(self._clock()) >= parse_utc(row["market_cutoff"]) + timedelta(minutes=minutes):
                raise DomainError("MODEL_ANALYSIS_EXPIRED","冻结资料已超过启用模式的分析时效，请准备新请求。",409)
        return {"request":row,"version_id":None}

    def model_packet(self, request_id, *, scope):
        data = self.model_request(request_id,scope=scope)
        schema = frozen_answer_schema(data["request"])
        schema["properties"]["schema_version"] = {"const":data["request"]["answer_schema_version"],"type":"string"}
        prompt = outbound_packet(data["request"]["packet_text"])
        terminal_path = data["request"].get("context",{}).get("settings",{}).get("values",{}).get("connection",{}).get("terminal_path")
        if terminal_path:
            prompt = prompt.replace(terminal_path,"[本机路径已移除]").replace(json.dumps(terminal_path)[1:-1],"[本机路径已移除]")
        return prompt, schema, data["request"]

    def auto_request_for(self, version_id, *, scope=None):
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            row = self._docs.get(tx, "auto_refresh_request", version_id)
            return self.get_request(row["request_id"], scope=tx) if row else None

    def pending_after(self, version_id, *, scope):
        for row in self._docs.list(scope, "request", state="awaiting_answer", limit=100):
            previous = row.get("context", {}).get("previous_analysis") or {}
            if previous.get("version_id") == version_id or row.get("base_version_id") == version_id:
                result = self._request_snapshot(scope, row)
                policy = row.get("analysis_preferences") or analysis_preferences({})
                lifetime = min(policy[s + "_refresh_minutes"] for s in policy["enabled_styles"])
                age = (parse_utc(self._clock()) - parse_utc(row["market_cutoff"])).total_seconds()
                if result.to_dict()["state"] == "awaiting_answer" and 0 <= age < lifetime * 60:
                    return result
        return None

    def remember_auto_request(self, version_id, request_id, *, uow):
        row = self._docs.get(uow, "auto_refresh_request", version_id)
        if row is None:
            self._docs.insert(uow, "auto_refresh_request", version_id, {"request_id": request_id})
        elif row["request_id"] != request_id:
            self._docs.update(uow, "auto_refresh_request", version_id, {"request_id": request_id}, row["revision"])

    def observed_reassessments(self, version_id, *, scope):
        return {row["trading_style"]: row["reason"] for row in self._docs.list(scope, "reassessment_observation", parent_id=version_id, limit=2)}

    def remember_reassessment(self, version_id, style, reason, *, uow):
        identity = version_id + ":" + style
        if not self._docs.get(uow, "reassessment_observation", identity):
            self._docs.insert(uow, "reassessment_observation", identity,
                {"version_id": version_id, "trading_style": style, "reason": reason}, parent_id=version_id)

    def _timing_reference(self, market, cutoff, scope):
        samples = []
        for version in self._docs.list(scope, "version", limit=100):
            if parse_utc(version["available_at"]) > cutoff:
                continue
            req = self._required(scope, "request", version["request_id"])
            if any(req["context"]["market"].get(key) != market.get(key) for key in SOURCE_FIELDS):
                continue
            samples.append({"version_id": version["version_id"], "schema_version": version["answer"]["schema_version"],
                "market_cutoff": req["market_cutoff"], "created_at": req["created_at"], "available_at": version["available_at"]})
        samples.sort(key=lambda row: parse_utc(row["available_at"]), reverse=True)
        guided = [s for s in samples if s["schema_version"] != "1.0.0"]
        return timing_reference(guided[:5] or samples[:5])

    def experience_predictions(self, context, *, scope):
        value = _data(context)
        output = {}
        for case in value.get("_case_candidates", value.get("cases", [])):
            id = case["inputs"].get("forecast_version_id")
            if id in output:
                continue
            row = self._docs.get(scope, "version", id) if id else None
            if row and parse_utc(row["available_at"]) <= parse_utc(value["cutoff"]):
                request = self._docs.get(scope, "request", row["request_id"])
                original_market = (request or {}).get("context", {}).get("market", {})
                output[id] = {"available_at": row["available_at"], "answer": row["answer"],
                    "forecast_id": row["forecast_id"], "market_cutoff": row["answer"]["market_cutoff"],
                    "market_signature": market_signature(original_market)}
        return output

    def experience_target(self, command, market, settings, *, scope):
        """Use exactly the requested/inherited horizons; no current settings for old packets."""
        command, market, settings = _data(command), _data(market), _data(settings)
        horizons = command.get("horizon_minutes")
        if horizons is None and command["kind"] == "update":
            horizons = self._required(scope, "forecast", command["forecast_id"])["horizon_minutes"]
        if horizons is None:
            horizons = default_analysis_horizons(settings["values"])
        return {"horizon_minutes": horizons, "market_signature": market_signature(market),
                "review_method": "prediction_review/3"}

    def __init__(self, db: Database, files: AttachmentStore, answer_schema: dict,
                 clock: Callable[[], str] = utc_now):
        self._db, self._files, self._schema, self._clock = db, files, _data(answer_schema), clock
        self._docs = db.documents("analysis")

    def create_request(self, command: dict, *, market: Snapshot, portfolio: Snapshot, settings: Snapshot,
                       experience: Snapshot | None = None, uow: UnitOfWork | None = None,
                       capture_binding: dict | None = None, holding_context: dict | None = None,
                       prompt_version="manual-analysis/14", research_context=None, persist=True, daily=False) -> Snapshot:
        command = _data(command)
        holding_context = _data(holding_context) if holding_context else None
        market_data, portfolio_data, settings_data = _data(market), _data(portfolio), _data(settings)
        cutoff = parse_utc(market_data["market_cutoff"])
        holding_order = command.get("holding_review_order")
        if holding_order:
            if command["kind"] != "new" or not holding_context:
                raise failure("HOLDING_REQUEST_INVALID", "/holding_review_order", "持仓重评须从当前已关联持仓手动创建。")
            if "horizon_minutes" in command:
                raise failure("HOLDING_REQUEST_INVALID", "/horizon_minutes", "持仓重评期限由原持仓剩余期限计算，不接受自定义延期。")
            remaining = int((parse_utc(holding_context["deadline"]) - cutoff).total_seconds()) // 60
            if remaining < 1:
                raise DomainError("HOLDING_DEADLINE", "原持仓退出期限已到或不足一分钟，请按原退出规则处理。", 409)
            command["horizon_minutes"] = [remaining]
            # Only the selected holding is sent. Association identity is request-time context.
            portfolio_data["orders"] = [holding_context.pop("order_fact")]
        aligned_policy = ANSWER_SCHEMA_VERSION in {"1.6.0", "1.7.0"}
        if command["market_snapshot_id"] != market_data["snapshot_id"]:
            raise failure("REQUEST_MISMATCH", "/market_snapshot_id", "请求指定的市场快照与冻结事实不一致。")
        if portfolio_data["account_scope_id"] != market_data["account_scope_id"]:
            raise failure("ACCOUNT_MISMATCH", "/portfolio", "市场和订单必须来自同一账户身份。")
        if command["kind"] not in ("new", "update"):
            raise failure("REQUEST_INVALID", "/kind", "请求类型必须是 new 或 update。")
        horizons = command.get("horizon_minutes")
        if "horizon_minutes" not in command and command["kind"] == "new":
            horizons = default_analysis_horizons(settings_data["values"])
        if horizons is not None or "horizon_minutes" in command:
            if not isinstance(horizons, list) or not horizons or any(
                    not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10080 for value in horizons) or len(horizons) != len(set(horizons)):
                raise failure("TIME_WINDOW_INVALID", "/horizon_minutes", "预测周期须为 1 至 10080 分钟的不重复整数。")
            if len(horizons) > 6:
                raise failure("TIME_WINDOW_INVALID", "/horizon_minutes", "合并后的预测周期超过6项，不会静默截断；请调整观察窗口设置或显式提交不超过6项的自定义周期。")
        if command["kind"] == "new" and (command["forecast_id"] is not None or command["base_version_id"] is not None):
            raise failure("REQUEST_MISMATCH", "/forecast_id", "新分析不能绑定已有预测或基础版本。")
        if command["kind"] == "update" and (not command["forecast_id"] or not command["base_version_id"]):
            raise failure("REQUEST_MISMATCH", "/base_version_id", "更新分析必须绑定预测及基础版本。")
        # These are detached input copies; stripping future evidence never alters saved source data.
        excluded = [order for order in portfolio_data.get("orders", [])
                    if parse_utc(order["event_time"]) > cutoff or
                    (aligned_policy and order.get("captured_at") and parse_utc(order["captured_at"]) > cutoff)]
        portfolio_data["orders"] = [order for order in portfolio_data.get("orders", [])
                                    if order not in excluded]
        for order in portfolio_data["orders"]:
            if order["order"]["account_scope_id"] != market_data["account_scope_id"]:
                raise failure("ACCOUNT_MISMATCH", "/portfolio/orders", "订单身份与冻结账户不一致。")
        identities = [tuple(order["order"][key] for key in ("account_scope_id", "kind", "broker_id"))
                      for order in portfolio_data["orders"]]
        if len(identities) != len(set(identities)) or len(identities) > 200:
            raise failure("ORDER_COVERAGE_INVALID", "/portfolio/orders", "订单身份必须唯一，单次分析最多覆盖 200 个对象。")
        account_time = portfolio_data.get("account_status", {}).get("observed_at")
        account_captured = portfolio_data.get("account_status", {}).get("captured_at")
        if ((account_time and parse_utc(account_time) > cutoff) or
                (aligned_policy and account_captured and parse_utc(account_captured) > cutoff)):
            portfolio_data["account"] = None
            portfolio_data["account_status"] = {**portfolio_data["account_status"], "state": "unavailable",
                "reasons": ["账户事实晚于分析截止点，未发送。"]}
        if excluded:
            portfolio_data["orders_status"] = {**portfolio_data.get("orders_status", {}), "state": "incomplete",
                "reasons": ["晚于分析截止点的订单事实已排除。"]}
        orders_time = portfolio_data.get("orders_status", {}).get("observed_at")
        orders_captured = portfolio_data.get("orders_status", {}).get("captured_at")
        if aligned_policy and ((orders_time and parse_utc(orders_time) > cutoff) or
                (orders_captured and parse_utc(orders_captured) > cutoff)):
            portfolio_data["orders"] = []
            portfolio_data["orders_status"] = {**portfolio_data["orders_status"], "state":"unavailable",
                "reasons":["订单读取晚于分析截止点，不能用未来订单或空数组表示当时空仓。"]}
        context = {"market": market_data, "portfolio": portfolio_data, "settings": settings_data,
                   "experience": self._eligible_experience(_data(experience) if experience else None, cutoff),
                   "evidence_refs": [market_data["snapshot_id"], portfolio_data["snapshot_id"]]}
        if holding_order:
            if len(portfolio_data["orders"]) != 1 or portfolio_data["orders"][0]["order"] != holding_order:
                raise DomainError("HOLDING_FACTS_UNAVAILABLE", "同批持仓资料晚于行情截止或不可核验，未创建重评请求。", 409)
            context["holding_review"] = holding_context
            context["evidence_refs"].append(holding_context["original_version_id"])
        exp = context["experience"]
        if ANSWER_SCHEMA_VERSION in {"1.4.0", "1.5.0", "1.6.0", "1.7.0"} and exp and exp.get("review_reference_total") == 0 and not exp.get("cases"):
            context["experience"] = exp = None
        if exp:
            context["evidence_refs"].extend(exp.get("review_refs", exp.get("references", [])))
            if exp.get("digest_id"):
                context["evidence_refs"].append(exp["digest_id"])
        context["evidence_refs"] = sorted(set(context["evidence_refs"]))
        with (nullcontext(uow) if uow else self._db.write()) as tx:
            if holding_order:
                selected = portfolio_data["orders"][0]
                pending = self.pending_holding_review(selected, holding_context["opened_at"], scope=tx)
                if pending:
                    return Snapshot.of(pending)
            forecast_id = command["forecast_id"] or new_id()
            window_start = market_data["market_cutoff"]
            if command["kind"] == "update":
                forecast = self._required(tx, "forecast", forecast_id)
                if forecast["current_version_id"] != command["base_version_id"]:
                    raise DomainError("VERSION_CONFLICT", "基础版本已变化，请基于当前版本创建更新。", 409)
                window_start, window_end = forecast["window_start"], forecast["window_end"]
                if horizons is None:
                    horizons = forecast["horizon_minutes"]
                if set(horizons) != set(forecast["horizon_minutes"]):
                    raise failure("TIME_WINDOW_INVALID", "/horizon_minutes", "更新必须保持原预测周期与端点。")
                previous_request = self._required(tx, "request", self._required(tx, "version", command["base_version_id"])["request_id"])
                original_market = previous_request["context"]["market"]
                if any(market_data.get(key) != original_market.get(key) for key in SOURCE_FIELDS):
                    raise DomainError("SOURCE_MISMATCH", "更新分析必须保持基础版本的账户、品种和事实来源。", 409)
                if cutoff < parse_utc(previous_request["market_cutoff"]):
                    raise failure("TIME_WINDOW_INVALID", "/market_cutoff", "更新截止点不能早于基础版本的截止点。")
                if cutoff >= parse_utc(window_end):
                    raise DomainError("EXPIRED_REQUEST", "原预测窗口已经结束，请创建新分析。", 409)
            else:
                window_end = _time(cutoff + timedelta(minutes=max(horizons)))
            request = {"request_id": new_id(), "forecast_id": forecast_id, "kind": command["kind"],
                "base_version_id": command["base_version_id"], "market_snapshot_id": market_data["snapshot_id"],
                "market_cutoff": market_data["market_cutoff"], "window_start": window_start, "window_end": window_end,
                "horizon_minutes": sorted(horizons), "created_at": self._clock(), "state": "awaiting_answer",
                "answer_schema_version": ANSWER_SCHEMA_VERSION,
                "prompt_version": prompt_version, "analysis_preferences": analysis_preferences(settings_data["values"])}
            if holding_order:
                request["holding_review_order"] = holding_order
            if ANSWER_SCHEMA_VERSION in {"1.5.0", "1.6.0", "1.7.0"}:
                request["analysis_preferences"] = new_analysis_preferences(settings_data["values"])
                context["near_term"] = build_near_term(market_data, request["market_cutoff"])
            with preparation_span("timing_reference"):
                context["timing_reference"] = self._timing_reference(market_data, cutoff, tx)
            request["estimated_answer_seconds"] = context["timing_reference"]["planning_delay_seconds"]
            previous = (Snapshot.of(self.get_forecast(command['forecast_id'],scope=tx).to_dict()['forecast']) if daily and command['kind']=='update'
                        else self.latest_for_market(market_data, scope=tx))
            if previous:
                prior = self.version_at(previous.to_dict()["forecast_id"], request["market_cutoff"], scope=tx)
                if prior:
                    prior = prior.to_dict()
                    context["previous_analysis"] = {"version_id": prior["version_id"],
                        "available_at": prior["available_at"], "market_cutoff": prior["answer"]["market_cutoff"],
                        "summary": prior["answer"]["summary"], "horizons": [
                            {**h, "phases": [{**p, "note": p.get("note", "")[:160]} for p in h["phases"]]} for h in prior["answer"]["horizons"]],
                        "plans": prior["answer"]["plans"], "analysis_basis": prior["answer"].get("analysis_basis"),
                        "candidate_assessments": prior["answer"].get("candidate_assessments")}
                    context["previous_analysis"]["summary_policy"] = "phase-note-first-160/1; complete prior answer preserved"
                    context["evidence_refs"].append(prior["version_id"])
            if ANSWER_SCHEMA_VERSION in {"1.5.0", "1.6.0", "1.7.0"}:
                context["previous_candidate_suitability"] = [
                    {"plan_key": plan["plan_key"], "diagnostic": assess_scalp(plan, market_data,
                        request["analysis_preferences"]["scalp_preferences"], now=request["market_cutoff"],
                        max_age_ms=settings_data["values"]["quote_max_age_ms"])}
                    for plan in (context.get("previous_analysis") or {}).get("plans", [])
                    if plan.get("trading_style") == "scalp"]
            with preparation_span("market_statistics"):
                context["statistics"] = build_statistics(market_data, horizons)
            if ANSWER_SCHEMA_VERSION in {"1.6.0", "1.7.0"}:
                with preparation_span("forecast_evidence"):
                    context.update(new_quality_context(market_data, horizons, context["experience"], window_start))
                context['prediction_policy']['endpoint_max_age_ms']=settings_data['values']['quote_max_age_ms']
                context["snapshot_alignment"] = {
                    "version":"capture-alignment/1", "market_snapshot_id":market_data["snapshot_id"],
                    "portfolio_snapshot_id":portfolio_data["snapshot_id"],
                    "binding":"same_capture_evidence" if capture_binding else "caller_supplied_detached_snapshots",
                    "capture_settings_revision":(capture_binding or {}).get("capture_settings_revision"),
                    "request_settings_revision":settings_data["revision"],
                    "settings_semantics":"request-time parameters; source capture revision separate; not historical market facts",
                    "market_cutoff":market_data["market_cutoff"],
                    "account_status":portfolio_data.get("account_status"), "orders_status":portfolio_data.get("orders_status")}
            baselines = []
            for baseline in context["statistics"]["frozen_baselines"]:
                end = parse_utc(window_start) + timedelta(minutes=baseline["horizon_minutes"])
                remaining = Decimal(str((end - cutoff).total_seconds())) / 60
                if remaining <= 0:
                    continue
                anchor = Decimal(baseline["anchor_price"])
                baseline["fixed_drift"] = decimal(anchor + (Decimal(baseline["fixed_drift"]) - anchor) * remaining / baseline["horizon_minutes"])
                baseline.update(end_at=_time(end), remaining_minutes=decimal(remaining))
                baselines.append(baseline)
            context["statistics"]["frozen_baselines"] = baselines
            if research_context:
                if persist or prompt_version not in {"manual-analysis/23", "manual-analysis/24", "manual-analysis/25", "manual-analysis/26", "manual-analysis/27", "manual-analysis/28"} or (command["kind"]!="new" and not daily) or holding_order:
                    raise DomainError("RESEARCH_ISOLATION_REQUIRED", "改进包只在独立比较记录中准备，不覆盖日常请求。", 409)
                context.update(_data(research_context))
                from .shared_search import bind_shared_research_refs
                context=bind_shared_research_refs({**request,"prompt_version":"manual-analysis/23"} if prompt_version in {"manual-analysis/24", "manual-analysis/25", "manual-analysis/26", "manual-analysis/27", "manual-analysis/28"} else request,context)
            with preparation_span("prototype_packet_build"):
                request["packet_text"] = build_packet(request, context, self._schema)
            if not persist:
                return Snapshot.of({**request,"context":context})
            self._docs.insert(tx, "request", request["request_id"], {**request, "context": context},
                              parent_id=forecast_id, state="awaiting_answer")
            if holding_order:
                self._remember_holding_request(request, holding_context, tx)
            return Snapshot.of(request)

    def save_daily_request(self, prepared, *, group_id, model_key, uow):
        """Persist a fresh branch, preserving shared context and each model's own base.

        Only fresh identity fields differ for a new round; no saved answer or
        historical request is ever relabelled. Ordinary import/review stays intact.
        """
        value=_data(prepared)
        value['request_id']=new_id()
        if value['kind']=='new':value['forecast_id']=new_id()
        context=value.pop('context');value.pop('packet_text',None)
        with preparation_span('branch_packet_build'):
            value['packet_text']=build_packet(value,context,self._schema)
        self._docs.insert(uow,'request',value['request_id'],{**value,'context':context},parent_id=value['forecast_id'],state='awaiting_answer')
        self._docs.insert(uow,'daily_request',value['request_id'],{'group_id':group_id,'model_key':model_key,
            'forecast_id':value['forecast_id']},parent_id=group_id)
        if not self._docs.get(uow,'daily_forecast',value['forecast_id']):
            self._docs.insert(uow,'daily_forecast',value['forecast_id'],{'group_id':group_id,'model_key':model_key})
        return Snapshot.of(value)

    @staticmethod
    def _eligible_experience(value, cutoff):
        if not value:
            return None
        # A digest is only available after every cited result. A missing bound is not permission to use it.
        available = value.get("available_at")
        if available is None or parse_utc(available) > cutoff:
            return None
        for record in value.get("reviews", []):
            if not record.get("available_at") or parse_utc(record["available_at"]) > cutoff:
                return None
        return value

    def prepare_import(self, command: dict) -> PreparedImport:
        command = _data(command)
        raw = command["raw_text"]
        attachment = self._files.prepare(raw.encode("utf-8"), media_type="application/json")
        origin = {key: command.get(key) for key in ("source", "provider", "model_id", "reasoning_effort", "analysis_platform")}
        origin["raw_sha256"] = attachment.sha256
        answer, error = None, None
        try:
            with self._db.read() as scope:
                request = self._required(scope, "request", command["request_id"])
                origin.update(prompt_version=request["prompt_version"], answer_schema_version=request["answer_schema_version"])
                previous = self._docs.get(scope, "request_result", command["request_id"])
                if previous and previous["raw_sha256"] != attachment.sha256:
                    raise DomainError("REQUEST_CONTENT_CONFLICT", "该请求已经导入不同原文，请创建新的更新请求。", 409)
                parsed = parse_document(raw)
                validate_answer(parsed, frozen_answer_schema(request), request, request["context"])
                answer = Snapshot.of(parsed)
        except DomainError as exc:
            error = exc
        return PreparedImport(command["request_id"], raw, attachment, Snapshot.of(origin), answer, error)

    @staticmethod
    def _unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    def commit_import(self, prepared: PreparedImport, *, uow: UnitOfWork,
                      available_at: str | None = None) -> ImportCommit:
        # Re-check under the write coordinator even if preparation observed an older state.
        prior = self._docs.get(uow, "request_result", prepared.request_id)
        if prior:
            if prior["raw_sha256"] != prepared.attachment.sha256:
                raise DomainError("REQUEST_CONTENT_CONFLICT", "该请求已有不同原文，请创建更新请求。", 409)
            saved = self._required(uow, "version", prior["version_id"])
            return ImportCommit(self._version_snapshot(uow, saved),
                                tuple(Snapshot.of(item) for item in saved["plan_drafts"]), True)
        if prepared.error:
            raise prepared.error
        request = self._required(uow, "request", prepared.request_id)
        now = available_at or self._clock()
        if parse_utc(now) >= parse_utc(request["window_end"]):
            raise DomainError("EXPIRED_REQUEST", "原预测窗口已结束，原回答已保留但不能成为新的可用预测。", 409)
        if parse_utc(now) < parse_utc(request["market_cutoff"]):
            raise DomainError("TIME_WINDOW_INVALID", "服务端可用时间不能早于分析截止点。")
        forecast = self._docs.get(uow, "forecast", request["forecast_id"])
        if (forecast["current_version_id"] if forecast else None) != request["base_version_id"]:
            raise DomainError("VERSION_CONFLICT", "基础版本已被另一回答更新，请刷新后重新创建请求。", 409)
        if forecast:
            base = self._required(uow, "version", forecast["current_version_id"])
            base_market = self._required(uow, "request", base["request_id"])["context"]["market"]
            if any(request["context"]["market"].get(key) != base_market.get(key) for key in SOURCE_FIELDS):
                raise DomainError("SOURCE_MISMATCH", "该更新请求的事实来源与基础版本不一致，不能导入。", 409)
        version_id = new_id()
        answer = prepared.answer.to_dict()
        # Draft identity is persisted with the version to make retried orchestration deterministic.
        drafts = [{"plan_id": new_id(), "version_id": version_id, "forecast_id": request["forecast_id"],
                   "available_at": now, "definition": plan} for plan in answer["plans"]]
        if answer["schema_version"] in {"1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
            for draft in drafts:
                draft["entry_timing"] = entry_timing(draft["definition"], request, now)
                preferences = request.get("analysis_preferences", {}).get("scalp_preferences")
                if answer["schema_version"] in {"1.5.0", "1.6.0", "1.7.0"} and preferences and draft["definition"].get("trading_style") == "scalp":
                    draft["scalp_preferences"] = preferences
                    draft["scalp_suitability"] = assess_scalp(draft["definition"], request["context"]["market"],
                        preferences, now=request["market_cutoff"],
                        max_age_ms=request["context"]["settings"]["values"]["quote_max_age_ms"])
        attachment = self._files.register(uow, prepared.attachment)
        origin = prepared.origin.to_dict()
        origin["import_latency_seconds"] = max(0, int((parse_utc(now) - parse_utc(request["created_at"])).total_seconds()))
        version = {"version_id": version_id, "forecast_id": request["forecast_id"],
                   "sequence": self._required(uow, "version", forecast["current_version_id"])["sequence"] + 1 if forecast else 1,
                   "request_id": prepared.request_id,
                   "previous_version_id": request["base_version_id"], "available_at": now,
                   "answer": answer, "origin": origin, "raw_attachment_id": attachment.id,
                   "plan_drafts": drafts, "baselines": request.get("context", {}).get("statistics", {}).get("frozen_baselines", [])}
        saved = self._docs.insert(uow, "version", version_id, version,
                                  parent_id=request["forecast_id"], available_at=now)
        self._docs.insert(uow, "request_result", prepared.request_id,
                          {"raw_sha256": attachment.sha256, "version_id": version_id})
        summary = {"forecast_id": request["forecast_id"], "current_version_id": version_id,
                   "window_start": request["window_start"], "window_end": request["window_end"],
                   "horizon_minutes": request["horizon_minutes"], "summary": answer["summary"], "available_at": now}
        source = request["context"]["market"]
        summary.update({key: source[key] for key in SOURCE_FIELDS})
        if request.get("holding_review_order"):
            self._commit_holding_review(request, version, uow)
        elif forecast:
            self._docs.update(uow, "forecast", request["forecast_id"], summary, forecast["revision"],
                              parent_id=market_scope_key(source))
        else:
            self._docs.insert(uow, "forecast", request["forecast_id"], summary,
                              parent_id=market_scope_key(source), available_at=now)
        self._docs.update(uow, "request", prepared.request_id,
                          {**_clean(request), "state": "imported"}, request["revision"], state="imported")
        self._docs.insert(uow, "import_attempt", new_id(),
            {"request_id": prepared.request_id, "raw_attachment_id": attachment.id, "raw_sha256": attachment.sha256,
             "attempted_at": now, "state": "succeeded", "version_id": version_id, "error": None},
            parent_id=prepared.request_id, state="succeeded", available_at=now)
        return ImportCommit(self._version_snapshot(uow, saved), tuple(Snapshot.of(item) for item in drafts), False)

    def record_failed_attempt(self, prepared: PreparedImport, error: DomainError,
                              *, uow: UnitOfWork | None = None) -> Snapshot:
        with (nullcontext(uow) if uow else self._db.write()) as tx:
            attachment = self._files.register(tx, prepared.attachment)
            attempt_id = new_id()
            row = self._docs.insert(tx, "import_attempt", attempt_id,
                {"attempt_id": attempt_id, "request_id": prepared.request_id, "raw_attachment_id": attachment.id,
                 "raw_sha256": attachment.sha256, "attempted_at": self._clock(), "state": "failed",
                 "version_id": None, "error": {"code": error.code, "detail": error.detail,
                                                "status": error.status, "field_errors": error.field_errors},
                 "origin": prepared.origin.to_dict()}, parent_id=prepared.request_id, state="failed")
            return Snapshot.of(row)

    def review_basis(self, version_id, *, scope=None):
        """Detached frozen facts for deterministic review; never uses current settings/quotes."""
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            version=self._required(tx,'version',version_id)
            request=self._required(tx,'request',version['request_id'])
            context=request['context']
            return Snapshot.of({'method_version':context.get('prediction_policy',{}).get(
                'review_method','prediction_review/2'), 'market':context['market'],
                'max_age_ms':context['settings']['values']['quote_max_age_ms']})

    def get_request(self, request_id: str, *, scope: ReadScope | None = None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            return self._request_snapshot(tx, self._required(tx, "request", request_id))

    def list_requests(self, *, state=None, limit=50, cursor=None, scope=None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            # Dynamic expired/superseded states are derived from authoritative time/current version.
            stored_state = ("awaiting_answer" if state in ("awaiting_answer", "expired", "superseded")
                            else "imported" if state == "imported" else None)
            rows, next_cursor = [], cursor
            while len(rows) < limit + 1:
                batch = self._docs.list(tx, "request", state=stored_state,
                                        limit=min(1000, limit + 1), cursor=next_cursor)
                if not batch:
                    break
                for item in batch:
                    snapshot = self._request_snapshot(tx, item).to_dict()
                    if state is None or snapshot["state"] == state:
                        rows.append(snapshot)
                        if len(rows) == limit + 1:
                            break
                next_cursor = batch[-1]["id"]
                if len(batch) < limit + 1:
                    break
            return Snapshot.of({"items": rows[:limit],
                                "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None})

    def get_version(self, version_id: str, *, forecast_id=None, scope=None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            row = self._required(tx, "version", version_id)
            if forecast_id is not None and row["forecast_id"] != forecast_id:
                raise DomainError("NOT_FOUND", "指定预测中不存在该版本。", 404)
            return self._version_snapshot(tx, row)

    def list_versions(self, forecast_id, *, limit=50, cursor=None, scope=None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            self._required(tx, "forecast", forecast_id)
            rows = self._docs.list(tx, "version", parent_id=forecast_id, limit=limit + 1, cursor=cursor)
            return Snapshot.of({"items": [self._version_snapshot(tx, row).to_dict() for row in rows[:limit]],
                                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    def get_forecast(self, forecast_id, *, scope=None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            row = self._required(tx, "forecast", forecast_id)
            return Snapshot.of({"forecast": {key: row[key] for key in FORECAST_FIELDS},
                "current_version": self._version_snapshot(tx, self._required(tx, "version", row["current_version_id"])).to_dict()})

    def list_forecasts(self, *, limit=50, cursor=None, scope=None) -> Snapshot:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            rows = self._docs.list(tx, "forecast", limit=limit + 1, cursor=cursor)
            return Snapshot.of({"items": [{key: row[key] for key in FORECAST_FIELDS} for row in rows[:limit]],
                                "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    def latest_for_market(self, market: Snapshot | dict | None, *, scope=None) -> Snapshot | None:
        if market is None:
            return None
        source = _data(market)
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            rows = self._docs.list(tx, "forecast", parent_id=market_scope_key(source), limit=1)
            if rows:
                if self._docs.get(tx,'daily_forecast',rows[0]['forecast_id']):
                    return None  # No last-arriving parallel answer becomes a primary forecast.
                return Snapshot.of({key: rows[0][key] for key in FORECAST_FIELDS})
            # Only early implementation records without an index are eligible for
            # this fallback. Indexed forecasts from other sources are never scanned.
            cursor, latest = None, None
            while True:
                old = self._docs.list(tx, "forecast", parent_is_null=True, limit=1000, cursor=cursor)
                for row in old:
                    version = self._docs.get(tx, "version", row["current_version_id"])
                    request = self._docs.get(tx, "request", version["request_id"]) if version else None
                    original = request.get("context", {}).get("market", {}) if request else {}
                    if any(original.get(key) != source[key] for key in SOURCE_FIELDS):
                        continue
                    if latest is None or (parse_utc(row["available_at"]), row["id"]) > (
                            parse_utc(latest["available_at"]), latest["id"]):
                        latest = row
                if len(old) < 1000:
                    break
                cursor = old[-1]["id"]
            return Snapshot.of({key: latest[key] for key in FORECAST_FIELDS}) if latest else None

    def version_at(self, forecast_id, as_of, *, scope=None) -> Snapshot | None:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            candidates, cursor = [], None
            while True:
                rows = self._docs.list(tx, "version", parent_id=forecast_id, available_before=as_of,
                                       limit=1000, cursor=cursor)
                candidates.extend(rows)
                if len(rows) < 1000:
                    break
                cursor = rows[-1]["id"]
            if not candidates:
                return None
            row = max(candidates, key=lambda value: (parse_utc(value["available_at"]), value["sequence"]))
            return self._version_snapshot(tx, row)

    def list_import_attempts(self, request_id, *, scope=None) -> tuple[Snapshot, ...]:
        with (nullcontext(scope) if scope else self._db.read()) as tx:
            return tuple(Snapshot.of({**row, "raw_text": self._files.read(tx, row["raw_attachment_id"]).decode("utf-8")})
                         for row in self._docs.list(tx, "import_attempt", parent_id=request_id, limit=1000))

    def _required(self, scope, kind, id):
        row = self._docs.get(scope, kind, id)
        if row is None:
            raise DomainError("NOT_FOUND", "请求、预测或版本不存在，请刷新后重试。", 404)
        return row

    def _request_snapshot(self, scope, row):
        result = {key: row[key] for key in REQUEST_FIELDS}
        if row.get("holding_review_order"):
            result["holding_review_order"] = row["holding_review_order"]
            saved = self._docs.get(scope, "request_result", row["request_id"])
            if saved:
                result["holding_result"] = self._version_snapshot(scope, self._required(scope, "version", saved["version_id"])).to_dict()
        if "analysis_preferences" in row:
            result["analysis_preferences"] = row["analysis_preferences"]
        if "estimated_answer_seconds" in row:
            result["estimated_answer_seconds"] = row["estimated_answer_seconds"]
        if result["state"] == "awaiting_answer":
            forecast = self._docs.get(scope, "forecast", row["forecast_id"])
            if forecast and forecast["current_version_id"] != row["base_version_id"]:
                result["state"] = "superseded"
            elif parse_utc(self._clock()) >= parse_utc(row["window_end"]):
                result["state"] = "expired"
        return Snapshot.of(result)

    def _version_snapshot(self, scope, row):
        return Snapshot.of({**{key: row[key] for key in VERSION_FIELDS},
                            **({"baselines": row["baselines"]} if "baselines" in row else {}),
                            "raw_text": self._files.read(scope, row["raw_attachment_id"]).decode("utf-8"),
                            "plans": []})
