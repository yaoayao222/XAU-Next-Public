"""Cross-domain orchestration. No domain table or ORM is accessed here."""

import logging
import hashlib
import json
import multiprocessing
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

from xau_next.domains.review.public import compute_actual, compute_prediction, compute_reference
from xau_next.domains.analysis.public import market_scope_key
from xau_next.platform.jobs.public import LeaseLost
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import new_id, parse_utc, utc_now
from xau_next.shared.holding import holding_review_context, holding_fingerprint, rule_key

from .guidance import GuidanceCoordinator

log = logging.getLogger("xau_next.workflows")
REQUEST_SUMMARY = (
    "request_id",
    "forecast_id",
    "kind",
    "base_version_id",
    "created_at",
    "market_cutoff",
    "window_end",
    "state",
)
FACT_REFRESH_COMMAND = {"include_history": False, "history_from": None, "history_to": None}
FACT_REFRESH_INTERVAL_SECONDS = 5
FACT_REFRESH_RETRY_SECONDS = 30


def wire(value):
    return value.to_dict() if isinstance(value, Snapshot) else value


def job_problem(error):
    return {
        "type": "urn:xau-next:problem:" + error.code.lower(),
        "title": "任务未完成",
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
        "trace_id": new_id(),
        "field_errors": error.field_errors,
        "retryable": bool(getattr(error, "retryable", False)),
    }


class Workflows:
    def __init__(
        self,
        *,
        db,
        settings,
        analysis,
        plans,
        market,
        portfolio,
        review,
        jobs,
        events,
        idempotency,
        contract_version,
        notifications=None,
        model_api=None,
        clock=utc_now,
        monotonic=time.monotonic,
    ):
        self.db, self.settings, self.analysis, self.plans, self.market, self.portfolio, self.review = (
            db,
            settings,
            analysis,
            plans,
            market,
            portfolio,
            review,
        )
        self.jobs, self.events, self.idempotency, self.clock = jobs, events, idempotency, clock
        self.monotonic = monotonic
        self.contract_version = contract_version
        self._stop = threading.Event()
        self._thread = None
        self._refresh_thread = None
        self._refresh_enabled = False
        self._next_refresh_attempt = 0
        self._pool = None
        self._scan_cursor = None
        self._trade_scan_cursor = None
        self._next_due_scan = 0
        self._guidance_thread = None
        self.model_api = model_api
        self._model_thread = None
        self._comparison_thread = None
        self._daily_threads = []
        self.guidance = GuidanceCoordinator(db=db, analysis=analysis, plans=plans, portfolio=portfolio,
            market=market, settings=settings, review=review, notifications=notifications, events=events,
            clock=clock) if notifications is not None else None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._next_refresh_attempt = 0
        self._refresh_enabled = True
        self._refresh_thread = threading.Thread(
            target=self._run_market_refresh, name="xau-next-fact-refresh", daemon=True
        )
        self._refresh_thread.start()
        self._thread = threading.Thread(target=self._run, name="xau-next-persistent-jobs", daemon=True)
        self._thread.start()
        if self.model_api:
            self._model_thread = threading.Thread(target=self._run_model,name="xau-next-model-io",daemon=True)
            self._model_thread.start()
            self._daily_threads=[threading.Thread(target=self._run_daily_model,name='xau-next-daily-'+str(i),daemon=True) for i in range(4)]
            for thread in self._daily_threads:thread.start()
        if getattr(self,'analysis_lab',None):
            self._comparison_thread=threading.Thread(target=self._run_comparison,name='xau-next-comparison-io',daemon=True)
            self._comparison_thread.start()
        if self.guidance:
            self.guidance.notifications.recover()
            self._guidance_thread = threading.Thread(target=self._run_guidance, name="xau-next-local-guidance", daemon=True)
            self._guidance_thread.start()

    def stop(self):
        self._stop.set()
        self._refresh_enabled = False
        if self._model_thread:
            self._model_thread.join(timeout=15)
        for thread in self._daily_threads:thread.join(timeout=15)
        if self._comparison_thread:
            self._comparison_thread.join(timeout=35)
        if self._refresh_thread:
            self._refresh_thread.join(timeout=35)
        if self._thread:
            self._thread.join(timeout=35)
        if self._guidance_thread:
            self._guidance_thread.join(timeout=15)
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)
        if hasattr(self.market, "close"):
            self.market.close()

    def _event(self, uow, kind, id, revision=1):
        self.events.append(uow, "resource.changed", resource={"kind": kind, "id": id}, revision=revision)

    def _holding_context(self, ref, market, paired, *, scope):
        if not market:
            raise DomainError("HOLDING_FACTS_UNAVAILABLE", "当前来源尚无可核验行情，请等待事实恢复；未发起模型调用。", 409)
        current = self.portfolio.get_portfolio(scope=scope).to_dict()
        cutoff = parse_utc(market["market_cutoff"])
        now = parse_utc(self.clock())
        max_age = self.settings.get_snapshot(scope=scope).to_dict()["values"]["quote_max_age_ms"]
        for status in (paired["orders_status"], current["orders_status"], market["quote_status"]):
            at = status.get("captured_at") or status.get("observed_at")
            if status["state"] != "available" or not at or not 0 <= (now - parse_utc(at)).total_seconds() * 1000 <= max_age:
                raise DomainError("HOLDING_FACTS_UNAVAILABLE", "持仓或行情资料过时，请等待新鲜事实后重评。", 409)
        if ref["kind"] != "position" or ref["account_scope_id"] != market["account_scope_id"]:
            raise DomainError("HOLDING_IDENTITY_INVALID", "仅能重评当前同账户的已成交持仓。", 409)
        selected = next((o for o in current["orders"] if o["order"] == ref and o["symbol"] == market["symbol"]), None)
        frozen = next((o for o in paired["orders"] if o["order"] == ref), None)
        if not selected or not frozen or not selected.get("plan_id"):
            raise DomainError("HOLDING_NOT_LINKED", "持仓已结束或缺少可靠的原方案关联，不能猜测原目标。", 409)
        frozen = {**frozen, "plan_id": selected["plan_id"]}
        opened_at = self.portfolio.position_opened_at(selected, scope=scope)
        if not opened_at or parse_utc(opened_at) > cutoff or holding_fingerprint(selected, opened_at) != holding_fingerprint(frozen, opened_at):
            raise DomainError("HOLDING_FACTS_CHANGED", "冻结快照与当前持仓关键事实不一致，或真实成交起点未知。", 409)
        plan = self.plans.get_plan(selected["plan_id"], scope=scope).to_dict()
        original_version = self.analysis.get_version(plan["version_id"], scope=scope).to_dict()
        original = self.analysis.get_request(original_version["request_id"], scope=scope).to_dict()
        original_market = self.market.get_snapshot(original["market_snapshot_id"], scope=scope).to_dict()
        if any(original_market.get(k) != market.get(k) for k in ("source_kind", "account_scope_id", "symbol")):
            raise DomainError("SOURCE_MISMATCH", "原方案和当前持仓行情来源不一致，不能混用。", 409)
        if parse_utc(original_version["available_at"]) > cutoff:
            raise DomainError("HOLDING_FACTS_CHANGED", "原方案在行情截止之后才可用，请等待下一份快照。", 409)
        observed = self.portfolio.holding_events(selected, opened_at, scope=scope)
        check = self.plans.position_advice(selected, plan, market, self.settings.get_snapshot(scope=scope).to_dict(),
                                          now=self.clock(), opened_at=opened_at, observed_events=observed)
        if check["state"] != "HOLD":
            raise DomainError("HOLDING_EXIT_OR_UNKNOWN", "原退出条件已触发或资料无法核验，请先处理原规则，不等待新的模型回答。", 409)
        previous = self.analysis.holding_review(selected, opened_at, scope=scope)
        if previous and parse_utc(previous["view"]["available_at"]) > cutoff:
            previous = None
        context = holding_review_context(selected, plan, opened_at, previous["view"] if previous else None)
        context["local_facts"] = {k: check["management"][k] for k in (
            "exit_price", "target_distance", "remaining_seconds", "original_stop", "original_target", "broker_stop", "broker_target")}
        context["covered_events"] = [e["rule_key"] for e in observed if parse_utc(e["observed_at"]) <= cutoff]
        for item in original_version["answer"].get("analysis_basis", {}).get("reassessment_conditions", []):
            if item["trading_style"] != plan["definition"].get("trading_style"):
                continue
            check = self.plans.rule_check(item["condition"], market, self.clock(), max_age, after=opened_at)
            if check["matched"] is True and parse_utc(check["observed_at"]) > parse_utc(opened_at):
                context["covered_events"].append("review:" + rule_key(item["condition"]))
        context["order_fact"] = frozen
        return context

    def workspace(self):
        with self.db.read() as scope:
            settings = self.settings.get_snapshot(scope=scope)
            market = wire(self.market.current(settings, scope=scope))
            forecast = wire(self.analysis.latest_for_market(market, scope=scope)) if market else None
            plans = (
                [wire(p) for p in self.plans.list_plans(forecast["current_version_id"], scope=scope)]
                if forecast
                else []
            )
            portfolio = wire(self.portfolio.get_portfolio(scope=scope))
            settings_revision = settings.to_dict()["revision"]
            evaluations = []
            for plan in plans:
                records = self.plans.list_evaluations(plan["plan_id"], limit=1, scope=scope).to_dict()[
                    "items"
                ]
                if (
                    records
                    and market
                    and records[0]["market_snapshot_id"] == market["snapshot_id"]
                    and records[0]["portfolio_snapshot_id"] == portfolio["snapshot_id"]
                    and records[0]["settings_revision"] == settings_revision
                ):
                    evaluations.append(records[0])
            pending = self.analysis.list_requests(state="awaiting_answer", limit=50, scope=scope).to_dict()[
                "items"
            ]
            active = [
                j
                for state in ("queued", "running", "cancelling")
                for j in self.jobs.list(scope, state=state, limit=50)
            ]
            cursor = self.events.cursor(scope)
            guidance = self.guidance.snapshot(scope, market=market, settings=settings, portfolio=portfolio,
                forecast=forecast, plans=plans, evaluations=evaluations)[0] if self.guidance else {}
            daily_groups=self.daily.store.list(scope=scope,source_scope=market_scope_key(market),limit=1)[0] if market and getattr(self,'daily',None) else []
            actions = (
                ["刷新事实后创建分析请求。"]
                if market is None
                else ["复制分析包取得回答，再导入并检查方案。"]
                if forecast is None
                else ["查看系统当前行动建议；满足条件时会主动通知，交易在 MT5 人工操作。"]
            )
            if daily_groups:actions=['查看本轮各模型结果与各自条件；到期成绩在历史与复盘中查看，不自动指定主回答。']
            return {
                "revision": cursor,
                "event_cursor": cursor,
                "as_of": self.clock(),
                "market": market,
                "current_forecast": forecast,
                "plans": plans[:2],
                "evaluations": evaluations[:2],
                "portfolio": portfolio,
                "active_jobs": active[:50],
                "pending_requests": [{k: v[k] for k in REQUEST_SUMMARY} for v in pending],
                "next_actions": actions,
                "fact_refresh": {
                    "enabled": self._refresh_enabled,
                    "interval_seconds": FACT_REFRESH_INTERVAL_SECONDS,
                    "retry_seconds": FACT_REFRESH_RETRY_SECONDS,
                    "latest_job": self.jobs.latest(scope, kind="market_refresh"),
                },
                **guidance,
                'daily_group':self.daily.get(daily_groups[0]['group_id'],scope=scope) if daily_groups else None,
            }

    def daily_analysis_source(self):
        from xau_next.shared.preparation_timing import preparation_span, preparation_fact
        with self.db.read() as scope:
            with preparation_span("source_read"):
                market = wire(self.market.latest(scope=scope))
            now = self.clock()
            cutoff = market["market_cutoff"] if market else None
            preparation_fact(market_snapshot_id=market["snapshot_id"] if market else None,
                             market_cutoff=cutoff,
                             source_age_ms=(parse_utc(now)-parse_utc(cutoff)).total_seconds()*1000 if cutoff else None)
            return {"market_snapshot_id": market["snapshot_id"] if market else None,
                    "market_cutoff": cutoff, "as_of": now}

    def dispatch(self, operation, body, params, key, path):
        from xau_next.shared.preparation_timing import preparation_trace, preparation_span
        with preparation_trace(operation), preparation_span("dispatch"):
            return self._dispatch(operation, body, params, key, path)

    def _dispatch(self, operation, body, params, key, path):
        if operation=='getDailyAnalysisSource':return 200,self.daily_analysis_source()
        if operation=='listDailyAnalysisGroups':return 200,self.daily.list(cursor=params.get('cursor'),limit=params.get('limit',20))
        if operation=='getDailyAnalysisGroup':return 200,self.daily.get(params['group_id'])
        if operation=='getDailyPerformance':
            from .daily_performance import performance
            return 200,performance(self,params)
        if operation=="getAnalysisLabStatus":return 200,self.analysis_lab.comparison.status()
        if operation=="getAnalysisComparison":return 200,self.analysis_lab.lab.comparison(params["comparison_id"])
        if operation=="startAnalysisComparison":
            if not key:raise DomainError("IDEMPOTENCY_KEY_REQUIRED","写操作必须提供幂等键。",422)
        if operation=='getComparisonAnswer':return 200,self.analysis_lab.comparison.answer(params['comparison_id'],params['arm'])
        if operation == "getModelSettings":
            return 200, self.model_api.settings.view()
        if operation == "listModelProfiles":
            return 200, self.model_api.settings.profiles()
        if operation == "setModelCredential":
            try:
                self.model_api.settings.set_credential(body,key)
            except DomainError:
                raise
            except Exception:  # noqa: BLE001 - credential errors must not be serialized or logged
                raise DomainError("CREDENTIAL_WRITE_FAILED","Windows 凭据写入失败；密钥未写入普通设置记录。",503) from None
            return 200, self.model_api.settings.view()
        if operation == "checkModelConnection":
            return 200, self.model_api.check(body["kind"])
        if operation == "getModelCall":
            return 200, self.model_api.calls.get(params["call_id"])
        if operation == "getModelRaw":
            return 200, self.model_api.raw(params["call_id"])
        if operation == "getRequestModelCall":
            with self.db.read() as scope:
                return 200, self.model_api.lookup(params["request_id"],scope=scope)
        if operation == "getHealth":
            return 200, {
                "status": "ok",
                "application_version": "0.1.0",
                "contract_version": self.contract_version,
                "database_ready": True,
                "server_time": self.clock(),
            }
        if operation == "getWorkspace":
            return 200, self.workspace()
        if operation in ("createExperiment", "getExperiment"):
            raise DomainError("FEATURE_NOT_ENABLED", "40 美元实验属于 V1.1，当前日常版尚未启用。", 501)
        reads = {
            "getMarketSnapshot": lambda: self.market.get_snapshot(params["snapshot_id"]),
            "getAnalysisRequest": lambda: self.analysis.get_request(params["request_id"]),
            "getPlan": lambda: self.plans.get_plan(params["plan_id"]),
            "listEvaluations": lambda: self.plans.list_evaluations(**params),
            "listForecasts": lambda: self.analysis.list_forecasts(**params),
            "getPortfolio": lambda: self.portfolio.get_portfolio(),
            "getSettings": lambda: self.settings.get_snapshot(**params),
            "listTrades": lambda: self.portfolio.list_trades(**params),
            "getTrade": lambda: self.portfolio.get_trade(**params),
            "listReviews": lambda: self._review_list_view(**params),
            "getReview": lambda: self._review_detail_view(**params),
        }
        if operation in reads:
            result = wire(reads[operation]())
            if result is None:
                raise DomainError("NOT_FOUND", "指定记录不存在，请刷新后重试。", 404)
            return 200, result
        if operation == "getJob":
            with self.db.read() as scope:
                result = self.jobs.get(scope, params["job_id"])
            if result is None:
                raise DomainError("JOB_NOT_FOUND", "任务不存在。", 404)
            return 200, result
        if operation == "listAnalysisRequests":
            result = self.analysis.list_requests(**params).to_dict()
            result["items"] = [{**{k: v[k] for k in REQUEST_SUMMARY},
                **({"holding_review_order": v["holding_review_order"]} if v.get("holding_review_order") else {})} for v in result["items"]]
            return 200, result
        if operation == "getForecast":
            with self.db.read() as scope:
                result = self.analysis.get_forecast(params["forecast_id"], scope=scope).to_dict()
                result["current_version"] = self._version(result["current_version"], scope)
                return 200, result
        if operation == "getForecastVersion":
            with self.db.read() as scope:
                return 200, self._version(
                    self.analysis.get_version(
                        params["version_id"], forecast_id=params["forecast_id"], scope=scope
                    ).to_dict(),
                    scope,
                )
        if operation == "listForecastVersions":
            result = self.analysis.list_versions(**params).to_dict()
            result["items"] = [
                {
                    **{k: v[k] for k in ("version_id", "forecast_id", "sequence", "available_at")},
                    "summary": v["answer"]["summary"],
                }
                for v in result["items"]
            ]
            return 200, result
        if not key:
            raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "写操作必须提供幂等键。", 422)
        margin_fact = None
        frozen_evaluation = None
        frozen_request = None
        lab_prepared = self.analysis_lab.prepare(body) if operation=="prepareAnalysisComparison" else None
        if operation == "createAnalysisRequest":
            with self.db.read() as scope:
                request_market = self.market.get_snapshot(body["market_snapshot_id"], scope=scope)
                if request_market is None:
                    raise DomainError("SNAPSHOT_NOT_FOUND", "指定行情快照不存在。", 404)
                binding = self.market.capture_binding(body["market_snapshot_id"], scope=scope)
                if not binding or not binding.get("portfolio_snapshot_id"):
                    raise DomainError("CAPTURE_BINDING_UNAVAILABLE", "行情没有可核验的同批账户身份；请刷新事实后创建新请求。", 409)
                request_portfolio = self.portfolio.get_snapshot(binding["portfolio_snapshot_id"], scope=scope)
                request_settings = self.settings.get_snapshot(scope=scope)
                experience = self.review.get_context(request_market.to_dict()["market_cutoff"], scope=scope, include_candidates=True)
                experience = self.review.enrich_context(experience, self.analysis.experience_predictions(experience, scope=scope),
                    selection_target=self.analysis.experience_target(body, request_market, request_settings, scope=scope))
                holding = self._holding_context(body["holding_review_order"], request_market.to_dict(), request_portfolio.to_dict(), scope=scope) if body.get("holding_review_order") else None
                frozen_request = (request_market, request_portfolio, request_settings, experience, binding, holding)
        if operation == "evaluatePlan":
            with self.db.read() as scope:
                evaluated_market = self.market.get_snapshot(body["market_snapshot_id"], scope=scope)
                if evaluated_market is None:
                    raise DomainError("SNAPSHOT_NOT_FOUND", "指定行情快照不存在。", 404)
                evaluated_portfolio = self.portfolio.get_portfolio(scope=scope)
                evaluated_settings = self.settings.get_snapshot(body["settings_revision"], scope=scope)
                selected_plan = self.plans.get_plan(params["plan_id"], scope=scope)
                preview = self.plans.preview_evaluation(
                    params["plan_id"],
                    body,
                    market=evaluated_market,
                    portfolio=evaluated_portfolio,
                    settings=evaluated_settings,
                    scope=scope,
                ).to_dict()
                frozen_evaluation = (evaluated_market, evaluated_portfolio, evaluated_settings)
            # A slow external calculation must never hold the database writer.
            if preview["entry_price"] and preview["volume_lots"]:
                calculation_input = {
                    "symbol": evaluated_market.to_dict()["symbol"],
                    "side": selected_plan.to_dict()["definition"]["side"],
                    "volume_lots": preview["volume_lots"],
                    "entry_price": preview["entry_price"],
                }
                try:
                    candidate = self.market.calculation(
                        "margin", calculation_input, evaluated_settings
                    ).to_dict()
                    m = evaluated_market.to_dict()
                    if (
                        candidate.get("account_scope_id") == m["account_scope_id"]
                        and candidate.get("source_kind") == m["source_kind"]
                        and candidate.get("symbol") == calculation_input["symbol"]
                        and candidate.get("side") == calculation_input["side"]
                        and candidate.get("entry_price") is not None
                        and candidate.get("volume_lots") is not None
                        and Decimal(candidate["entry_price"]) == Decimal(calculation_input["entry_price"])
                        and Decimal(candidate["volume_lots"]) == Decimal(calculation_input["volume_lots"])
                        and candidate.get("currency") == preview["currency"]
                        and candidate.get("captured_at") is not None
                        and 0
                        <= (parse_utc(self.clock()) - parse_utc(candidate["captured_at"])).total_seconds()
                        * 1000
                        <= evaluated_settings.to_dict()["values"]["quote_max_age_ms"]
                    ):
                        margin_fact = candidate
                except DomainError:
                    pass  # Missing margin remains explicitly unknown; it never erases known risk amounts.

        # All side effects, idempotency and events use one short transaction.
        def perform(uow):
            if operation=='prepareDailyAnalysis':return 201,self.daily.prepare(body,uow=uow)
            if operation=='startDailyAnalysis':return 202,self.daily.start(params['group_id'],body,uow=uow)
            if operation=='startAnalysisComparison':
                return 202,self.analysis_lab.comparison.queue_run(params['comparison_id'],body,uow=uow)
            if operation=='scoreAnalysisComparison':
                return 202,self.analysis_lab.comparison.queue_score(params['comparison_id'],body,uow=uow)
            if operation=="refreshAnalysisResearch":
                return 202,self.jobs.enqueue(uow,"research_capture",body,coalesce_active=True,max_attempts=1)
            if operation=="prepareAnalysisComparison":
                result=self.analysis_lab.lab.save_comparison(lab_prepared,package_kind=body["package_kind"],
                    purpose=body["purpose"],bundle_id=body["bundle_id"] if body["package_kind"]=="improved" else None,uow=uow)
                self._event(uow,"analysis_comparison",result["comparison_id"])
                return 201,result
            if operation in ("createModelProfile", "activateModelProfile"):
                cancelling = self.jobs.list(uow, state="cancelling", limit=1000)
                if self.model_api.calls.pending(scope=uow) or any(
                    job["kind"] == "model_call" for job in cancelling
                ) or len(cancelling) == 1000:
                    raise DomainError("MODEL_BUSY", "分析或合成测试进行中，请结束后再切换连接配置。", 409)
                if operation == "createModelProfile":
                    self.model_api.settings.create_profile(body, uow=uow)
                else:
                    self.model_api.settings.activate_profile(params["profile_id"], body["expected_revision"], uow=uow)
                result = self.model_api.settings.view(scope=uow)
                self._event(uow, "settings", "model", result["revision"])
                return (201 if operation == "createModelProfile" else 200), result
            if operation == "updateModelSettings":
                self.model_api.settings.update(body,uow=uow)
                result = self.model_api.settings.view(scope=uow)
                self._event(uow,"settings","model",result["revision"])
                return 200, result
            if operation in ("startModelAnalysis","startModelProbe"):
                if operation == "startModelAnalysis":
                    if self.daily.store.request_metadata(params['request_id'],scope=uow):
                        raise DomainError('DAILY_GROUP_RUN_REQUIRED','此请求属于模型组，请从该轮分析明确授权后发送。',409)
                    request = self.analysis.model_request(params["request_id"], scope=uow)["request"]
                    binding = request.get("context", {}).get("holding_review")
                    if binding:
                        settings = self.settings.get_snapshot(scope=uow)
                        current_market = wire(self.market.current(settings, scope=uow))
                        current_portfolio = self.portfolio.get_portfolio(scope=uow).to_dict()
                        current_binding = self._holding_context(request["holding_review_order"], current_market, current_portfolio, scope=uow)
                        if current_binding["fingerprint"] != binding["fingerprint"]:
                            raise DomainError("HOLDING_FACTS_CHANGED", "持仓关键事实已变化，请重新准备重评包；未发起模型调用。", 409)
                return 202, self.model_api.enqueue(params.get("request_id"),body,uow=uow)
            if operation in ("refreshMarket", "importAnalysisAnswer", "refreshReviews"):
                kinds = {
                    "refreshMarket": "market_refresh",
                    "importAnalysisAnswer": "answer_import",
                    "refreshReviews": "review_refresh",
                }
                if operation == "refreshReviews" and parse_utc(body["through"]) > parse_utc(self.clock()):
                    raise DomainError("FUTURE_REVIEW", "复盘截止时间不能晚于当前服务器时间。")
                if operation == "refreshMarket" and (
                    bool(body["history_from"]) != bool(body["history_to"])
                    or (body["include_history"] and not body["history_from"])
                ):
                    raise DomainError("HISTORY_RANGE_REQUIRED", "历史读取需要同时提供开始和结束时间。")
                result = self.jobs.enqueue(
                    uow, kinds[operation], body, coalesce_active=operation in {"refreshMarket", "refreshReviews"}
                )
                return 202, result
            if operation == "createAnalysisRequest":
                market, portfolio, settings, experience, binding, holding = frozen_request
                if holding:
                    verified = self._holding_context(body["holding_review_order"], market.to_dict(), portfolio.to_dict(), scope=uow)
                    if verified["fingerprint"] != holding["fingerprint"]:
                        raise DomainError("HOLDING_FACTS_CHANGED", "构包前持仓已变化，请刷新后重评。", 409)
                result = self.analysis.create_request(
                    body,
                    market=market,
                    portfolio=portfolio,
                    settings=settings,
                    experience=experience,
                    capture_binding=binding,
                    holding_context=holding,
                    uow=uow,
                ).to_dict()
                self._event(uow, "analysis_request", result["request_id"])
                return 201, result
            if operation == "evaluatePlan":
                evaluated_market, evaluated_portfolio, evaluated_settings = frozen_evaluation
                result = self.plans.evaluate_plan(
                    params["plan_id"],
                    body,
                    market=evaluated_market,
                    portfolio=evaluated_portfolio,
                    settings=evaluated_settings,
                    margin_fact=margin_fact,
                    uow=uow,
                ).to_dict()
                self._event(uow, "evaluation", result["evaluation_id"])
                return 201, result
            if operation == "updateSettings":
                result = self.settings.update_settings(body, uow=uow).to_dict()
                self._event(uow, "settings", "current", result["revision"])
                return 200, result
            if operation == "confirmPlanObservation":
                result = self.plans.confirm_observation(params["plan_id"], body, uow=uow).to_dict()
                self._event(uow, "plan", params["plan_id"])
                return 201, result
            if operation == "cancelJob":
                return 202, self.model_api.cancel(params["job_id"],uow=uow) if self.model_api else self.jobs.cancel(params["job_id"], scope=uow)
            if operation == "recordDecision":
                result = self.portfolio.record_decision(body, uow=uow).to_dict()
                self._event(uow, "portfolio", result.get("decision_id", "current"))
                return 201, result
            if operation == "createAssociation":
                context = None
                if body["action"] == "link":
                    plan = self.plans.get_plan(body["plan_id"], scope=uow).to_dict()
                    version = self.analysis.get_version(plan["version_id"], scope=uow).to_dict()
                    market = self.market.get_snapshot(
                        version["answer"]["market_snapshot_id"], scope=uow
                    ).to_dict()
                    context = Snapshot.of(
                        {
                            "plan": plan,
                            "account_scope_id": market["account_scope_id"],
                            "symbol": market["symbol"],
                        }
                    )
                result = self.portfolio.create_association(body, plan_snapshot=context, uow=uow).to_dict()
                self._event(uow, "portfolio", result.get("association_id", "current"))
                return 201, result
            raise DomainError("OPERATION_UNAVAILABLE", "该操作尚未接线。", 501)

        result = self.idempotency.execute(
            "PUT" if operation in ("updateSettings","updateModelSettings") else "POST", path, key, body, perform
        )
        return result.status, result.body

    def _version(self, version, scope):
        version["plans"] = [wire(p) for p in self.plans.list_plans(version["version_id"], scope=scope)]
        return version

    def _sync_capture_portfolio(self, capture, uow):
        market = capture.market.to_dict()
        trades = tuple(
            Snapshot.of(
                {
                    **trade.to_dict(),
                    "source_kind": market["source_kind"],
                    "market_snapshot_id": market["snapshot_id"],
                }
            )
            for trade in capture.deals
        )
        return self.portfolio.sync_facts(capture.portfolio, trades=trades, uow=uow)

    def _automatic_evaluations(self, uow, market, plans, margin_facts=None):
        portfolio = self.portfolio.get_portfolio(scope=uow)
        settings = self.settings.get_snapshot(scope=uow)
        for plan in plans:
            p = wire(plan)
            command = {
                "market_snapshot_id": wire(market)["snapshot_id"],
                "settings_revision": wire(settings)["revision"],
                "entry_price_override": None,
                "volume_lots_override": None,
                "sizing_mode": "fixed_volume",
            }
            quote = wire(market).get("quote")
            if p["definition"].get("trading_style") and quote:
                command["entry_price_override"] = quote["ask" if p["definition"]["side"] == "long" else "bid"]
            margin_fact = (margin_facts or {}).get(p["plan_id"])
            if margin_fact and margin_fact.get("settings_revision") != wire(settings)["revision"]:
                margin_fact = None
            try:
                result = self.plans.evaluate_plan(
                    p["plan_id"], command, market=market, portfolio=portfolio, settings=settings, uow=uow,
                    margin_fact=margin_fact
                ).to_dict()
                self._event(uow, "evaluation", result["evaluation_id"])
            except DomainError as exc:
                # Current source or contract may differ from the frozen original plan.
                # The plan remains viewable; a deliberate manual trial shows the precise field issue.
                if exc.code not in {
                    "ACCOUNT_MISMATCH",
                    "PRICE_GRID_INVALID",
                    "PRICE_ORDER_INVALID",
                    "VOLUME_GRID_INVALID",
                    "CONTRACT_INVALID",
                    "AMOUNT_RANGE_UNSUPPORTED",
                }:
                    raise

    def _current_margin_facts(self, capture, settings):
        """Read-only margin facts for exact current entry prices, outside any DB writer."""
        market, portfolio, config = wire(capture.market), wire(capture.portfolio), wire(settings)
        quote = market.get("quote")
        if not quote or market["quote_status"]["state"] != "available":
            return {}
        with self.db.read() as scope:
            forecast = self.analysis.latest_for_market(market, scope=scope)
            if not forecast:
                return {}
            plans = self.plans.list_plans(forecast.to_dict()["current_version_id"], scope=scope)
            candidates = []
            for item in plans:
                plan = item.to_dict()
                p = plan["definition"]
                if not p.get("trading_style") or plan["lifecycle"] in {"EXPIRED", "INVALIDATED", "SUPERSEDED"}:
                    continue
                price = quote["ask" if p["side"] == "long" else "bid"]
                zone = p["entry"]["zone"]
                if not zone or not Decimal(zone["low"]) <= Decimal(price) <= Decimal(zone["high"]):
                    continue
                command = {"market_snapshot_id": market["snapshot_id"], "settings_revision": config["revision"],
                           "entry_price_override": price, "volume_lots_override": None, "sizing_mode": "fixed_volume"}
                try:
                    preview = self.plans.preview_evaluation(plan["plan_id"], command, market=market,
                        portfolio=portfolio, settings=config, scope=scope).to_dict()
                except DomainError:
                    continue
                if preview["volume_lots"] and preview["budget_check"]["state"] == "satisfied":
                    candidates.append((plan, preview))
        facts = {}
        for plan, preview in candidates:
            values = {"symbol": market["symbol"], "side": plan["definition"]["side"],
                      "entry_price": preview["entry_price"], "volume_lots": preview["volume_lots"]}
            try:
                fact = self.market.calculation("margin", values, settings).to_dict()
                if (fact.get("account_scope_id") == market["account_scope_id"]
                        and fact.get("source_kind") == market["source_kind"]
                        and fact.get("currency") == preview["currency"]
                        and all(fact.get(k) == values[k] for k in ("symbol", "side"))
                        and all(fact.get(k) is not None and Decimal(fact[k]) == Decimal(values[k]) for k in ("entry_price", "volume_lots"))
                        and fact.get("captured_at") and 0 <= (parse_utc(self.clock()) - parse_utc(fact["captured_at"])).total_seconds() * 1000
                            <= config["values"]["quote_max_age_ms"]):
                    facts[plan["plan_id"]] = {**fact, "settings_revision": config["revision"]}
            except DomainError:
                pass
        return facts

    @contextmanager
    def _heartbeat(self, lease):
        stop = threading.Event()

        def pulse():
            while not stop.wait(10):
                try:
                    self.jobs.heartbeat(lease)
                except LeaseLost:
                    return
                except Exception:
                    log.exception("Job heartbeat failed: %s", lease.job_id)
                    return

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def process_once(self, *, kind=None, exclude_kinds=()):
        lease = self.jobs.claim(
            "api-facts" if kind == "market_refresh" else "api-main", kind=kind, exclude_kinds=exclude_kinds
        )
        if lease is None:
            return False
        prepared = None
        failed = False
        try:
            with self._heartbeat(lease):
                if lease.kind == "market_refresh":
                    source_settings = self.settings.get_snapshot()
                    prepared = self.market.capture(lease.input_data, source_settings)
                    margins = self._current_margin_facts(prepared, source_settings)
                    with self.db.write() as uow:
                        before = source_settings.to_dict()["values"]
                        after = self.settings.get_snapshot(scope=uow).to_dict()["values"]
                        if any(before[field] != after[field] for field in ("symbol", "connection")):
                            raise DomainError(
                                "SOURCE_SETTINGS_CHANGED",
                                "读取期间数据源设置已变化，将重新读取当前来源。",
                                409,
                            )
                        market = self.market.commit_capture(prepared, uow=uow, settings_revision=source_settings.to_dict()["revision"])
                        self._sync_capture_portfolio(prepared, uow)
                        refreshed = self.plans.refresh_lifecycles(market, uow=uow)
                        self._automatic_evaluations(uow, market, refreshed, margins)
                        self._event(uow, "market_snapshot", market.to_dict()["snapshot_id"])
                        self._event(uow, "portfolio", prepared.portfolio.to_dict()["snapshot_id"])
                        self.jobs.finish(
                            uow, lease, {"kind": "market_snapshot", "id": market.to_dict()["snapshot_id"]}
                        )
                elif lease.kind == "research_capture":
                    value,raws=self.analysis_lab.capture(lease.input_data["month"])
                    if self.jobs.inspect_lease(lease)["state"]=="cancelling":
                        self.jobs.acknowledge_cancel(lease)
                        return True
                    with self.db.write() as uow:
                        self.jobs.assert_current(uow,lease)
                        bundle=self.analysis_lab.lab.save_bundle(value,raws,uow=uow)
                        self._event(uow,"research_bundle",bundle["bundle_id"])
                        self.jobs.finish(uow,lease,{"kind":"research_bundle","id":bundle["bundle_id"]})
                elif lease.kind == "answer_import":
                    prepared = self.analysis.prepare_import(lease.input_data)
                    with self.db.write() as uow:
                        version = self.commit_answer(prepared,uow=uow)
                        self.jobs.finish(uow, lease, {"kind": "forecast", "id": version["forecast_id"]})
                elif lease.kind=='comparison_run':
                    self.analysis_lab.comparison.run(lease)
                elif lease.kind=='comparison_score':
                    self.analysis_lab.comparison.score(lease)
                elif lease.kind == "review_refresh":
                    self.run_reviews(lease)
                else:
                    raise DomainError("FEATURE_NOT_ENABLED", "当前版本未启用这个任务。", 501)
        except LeaseLost:
            log.info("Job lease no longer current: %s", lease.job_id)
        except Exception as exc:
            failed = True
            error = (
                exc
                if isinstance(exc, DomainError)
                else DomainError("TASK_FAILED", "处理失败，请查看本机诊断日志后重试。", 500)
            )
            if not isinstance(exc, DomainError):
                log.exception("Task %s failed", lease.job_id)
            try:
                with self.db.write() as uow:
                    if lease.kind == "answer_import" and prepared:
                        self.analysis.record_failed_attempt(prepared, error, uow=uow)
                    self.jobs.fail(lease, job_problem(error), scope=uow)
            except LeaseLost:
                pass
        finally:
            if lease.kind == "market_refresh":
                self._next_refresh_attempt = self.monotonic() + (
                    FACT_REFRESH_RETRY_SECONDS if failed else FACT_REFRESH_INTERVAL_SECONDS
                )
        return True

    def commit_answer(self, prepared, *, uow):
        result = self.analysis.commit_import(prepared, uow=uow)
        version = result.version.to_dict()
        request = self.analysis.get_request(prepared.request_id, scope=uow).to_dict()
        if request.get("holding_review_order"):
            self._event(uow, "analysis_request", prepared.request_id, 2)
            return version
        if not result.repeated and version["previous_version_id"]:
            self.plans.supersede(
                version["previous_version_id"], uow=uow, at=version["available_at"]
            )
        input_market = self.market.get_snapshot(
            version["answer"]["market_snapshot_id"], scope=uow
        )
        registered = self.plans.register(
            result.plans, uow=uow, market_identity=wire(input_market)
        )
        if not result.repeated:
            latest = self.market.latest(scope=uow)
            if latest:
                self._automatic_evaluations(uow, latest, registered)
        self._event(uow, "forecast", version["forecast_id"], version["sequence"])
        return version

    @staticmethod
    def _review_bounds(start, end):
        # Fixed historical bounds: a later wall clock must not create a different work identity.
        start = (parse_utc(start) - timedelta(minutes=2)).replace(second=0, microsecond=0)
        end = (parse_utc(end) + timedelta(minutes=3)).replace(second=0, microsecond=0)
        return tuple(at.isoformat().replace("+00:00", "Z") for at in (start, end))

    def _review_token(self, version, start, end, scope):
        snapshot_id = version["answer"].get("market_snapshot_id")
        if not snapshot_id or parse_utc(start) >= parse_utc(end):
            return None
        return self.market.review_evidence_token(snapshot_id, *self._review_bounds(start, end), scope=scope)

    def _review_list_view(self, **params):
        # Historical attachments can contain millions of ticks. Decode only the selected detail.
        return self.review.list_reviews(**params)

    def _review_detail_view(self, **params):
        with self.db.read() as scope:
            return Snapshot.of(self._review_annotation(self.review.get_review(**params,scope=scope).to_dict(),scope))

    def _review_annotation(self, review, scope):
        """A dated read-only view. Never commits a review revision or enters experience totals."""
        if review['target_kind']!='forecast':
            return review
        key=(review['review_id'],review['revision'])
        cache=getattr(self,'_review_view_cache',{})
        if key in cache:
            return {**review,'diagnostics':cache[key]}
        from xau_next.domains.review.public import diagnose_review
        version=self.analysis.get_version(review['inputs']['forecast_version_id'],scope=scope).to_dict()
        basis=self.analysis.review_basis(version['version_id'],scope=scope).to_dict()
        evidence=None
        for source in review['inputs']['market_snapshot_ids']:
            evidence=self.market.saved_review_evidence(source,endpoint_at=review['window_end'],
                max_age_ms=basis['max_age_ms'],scope=scope)
            if evidence: break
        diag=diagnose_review(review,version,basis,wire(evidence) if evidence else None,as_of=self.clock())
        if len(cache)>=256: cache.clear()
        cache[key]=diag
        self._review_view_cache=cache
        return {**review,'diagnostics':diag}

    @staticmethod
    def _trade_token(detail):
        return hashlib.sha256(json.dumps(detail, sort_keys=True, separators=(",", ":"),
                                         allow_nan=False).encode()).hexdigest()

    def run_reviews(self, lease):
        through = lease.input_data["through"]
        selected = lease.input_data.get("forecast_id")
        versions = []
        trade_inputs = []
        skipped = 0
        with self.db.read() as scope:
            if lease.input_data.get("actual_only"):
                forecasts = []
            elif selected:
                forecasts = [self.analysis.get_forecast(selected, scope=scope).to_dict()["forecast"]]
            else:
                forecasts = []
                cursor = None
                while True:
                    page = self.analysis.list_forecasts(limit=100, cursor=cursor, scope=scope).to_dict()
                    forecasts.extend(page["items"])
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
            for forecast in forecasts:
                cursor = None
                while True:
                    page = self.analysis.list_versions(
                        forecast["forecast_id"], limit=100, cursor=cursor, scope=scope
                    ).to_dict()
                    for version in page["items"]:
                        plans = [wire(p) for p in self.plans.list_plans(version["version_id"], scope=scope)]
                        due_h = [
                            h
                            for h in version["answer"]["horizons"]
                            if parse_utc(h["end_at"]) <= parse_utc(through)
                        ]
                        due_p = [
                            p
                            for p in plans
                            if parse_utc(p["definition"]["exit_deadline"]) + timedelta(seconds=5)
                            <= parse_utc(through)
                        ]
                        pending_h = [h for h in due_h if self.review.refresh_needed(
                            "forecast", version["forecast_id"], version_id=version["version_id"],
                            horizon=h["horizon_minutes"], evidence_token=self._review_token(
                                version, version["answer"]["market_cutoff"], h["end_at"], scope), scope=scope)]
                        pending_p = [p for p in due_p if self.review.refresh_needed(
                            "reference_plan", p["plan_id"], version_id=version["version_id"],
                            evidence_token=self._review_token(version,
                                max(p["available_at"], p["definition"]["valid_from"], key=parse_utc),
                                p["definition"]["exit_deadline"], scope), scope=scope)]
                        skipped += len(due_h) + len(due_p) - len(pending_h) - len(pending_p)
                        due_h, due_p = pending_h, pending_p
                        if due_h or due_p:
                            versions.append((version, due_h, due_p))
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
            if lease.input_data.get("trade_id"):
                trade_ids = [lease.input_data["trade_id"]]
            elif selected is None:
                trade_ids = []
                cursor = None
                while True:
                    page = self.portfolio.list_trades(limit=100, cursor=cursor, scope=scope).to_dict()
                    trade_ids.extend(
                        trade["trade_id"]
                        for trade in page["items"]
                        if trade["opened_at"] is None or parse_utc(trade["opened_at"]) <= parse_utc(through)
                    )
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
            else:
                trade_ids = []  # Forecast-specific work never recomputes unrelated actual trades.
            for trade_id in trade_ids:
                detail = self.portfolio.get_trade(trade_id, scope=scope).to_dict()
                metrics = self.portfolio.get_trade_metrics(trade_id, scope=scope).to_dict()
                if not self.review.refresh_needed("actual_trade", trade_id,
                        evidence_token=self._trade_token(detail), scope=scope):
                    skipped += 1
                    continue
                trade_inputs.append(
                    Snapshot.of(
                        {
                            **detail,
                            "source_kind": metrics.get("source_kind", "unknown"),
                            "available_at": metrics["available_at"],
                        }
                    )
                )
            settings = self.settings.get_snapshot(scope=scope)
        total = sum(len(h) + len(p) for _, h, p in versions) + len(trade_inputs)
        done = 0
        result_ref = None

        def progress(stage):
            self.jobs.heartbeat(lease, progress=done / max(total, 1),
                                message=f"补算 {done}/{total} · {stage} · 跳过已完成或待复查 {skipped} 项")

        def commit_item(prepared, pending_captures, evidence_window=None, trade_token=None):
            nonlocal done, result_ref
            # Never keep the writer while calculating or saving an entire backlog.
            with self.db.write() as uow:
                self.jobs.assert_current(uow, lease)
                for capture in pending_captures:
                    captured = self.market.commit_capture(capture, uow=uow)
                    self._sync_capture_portfolio(capture, uow)
                    self._event(uow, "market_snapshot", captured.to_dict()["snapshot_id"])
                token = (self._review_token(*evidence_window, uow)
                         if evidence_window is not None else trade_token)
                result = self.review.commit_refresh(prepared, uow=uow, evidence_token=token).to_dict()
                self._event(uow, "review", result["review_id"], result["revision"])
                result_ref = {"kind": "review", "id": result["review_id"]}
                self.jobs.assert_current(uow, lease)
            pending_captures.clear()
            done += 1
            progress("已保存检查点")

        progress("正在读取历史行情")
        # Long market reads and CPU calculations occur outside every business transaction.
        for version, horizons, plans in versions:
            original = self.market.get_snapshot(version["answer"]["market_snapshot_id"]).to_dict()
            review_basis = self.analysis.review_basis(version['version_id']).to_dict()
            windows = {}
            captures = []

            def target_window(requested_start, requested_end, *, windows=windows, original=original):
                # Source completeness belongs to this target's evidence request.
                # Reusing a longer request's aggregate tick/bar status contaminates
                # already-complete short windows with unrelated later gaps.
                start = (parse_utc(requested_start) - timedelta(minutes=2)).replace(second=0, microsecond=0)
                end = min(parse_utc(requested_end) + timedelta(minutes=3), parse_utc(self.clock()))
                end = end.replace(second=0, microsecond=0)
                key = (start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"))
                if key not in windows:
                    capture = self.market.capture(
                        {"include_history": True, "history_from": key[0], "history_to": key[1]}, settings
                    )
                    captured_market = capture.market.to_dict()
                    if (captured_market["account_scope_id"], captured_market["symbol"]) != (
                        original["account_scope_id"],
                        original["symbol"],
                    ):
                        raise DomainError(
                            "ACCOUNT_MISMATCH", "当前终端无法为另一账户来源的旧预测提供同源复盘。", 409
                        )
                    captures.append(capture)
                    window = self.market.window_from_capture(capture, *key).to_dict()
                    window["contract"] = captured_market["contract"]
                    windows[key] = window
                return windows[key]

            for horizon in horizons:
                try:
                    window = target_window(version["answer"]["market_cutoff"], horizon["end_at"])
                    progress(f"核对 {horizon['horizon_minutes']} 分钟行情")
                    prepared = (
                        self._compute(
                            compute_prediction,
                            version,
                            window,
                            horizon_minutes=horizon["horizon_minutes"],
                            as_of=self.clock(),
                            review_basis=review_basis,
                        )
                    )
                    commit_item(prepared, captures,
                                (version, version["answer"]["market_cutoff"], horizon["end_at"]))
                except DomainError as exc:
                    if exc.code != "REVIEW_NOT_DUE":
                        raise
            for plan in plans:
                effective = max(plan["available_at"], plan["definition"]["valid_from"], key=parse_utc)
                already_expired = parse_utc(effective) >= parse_utc(plan["definition"]["entry_deadline"])
                already_terminated = plan.get("lifecycle") in {"INVALIDATED", "SUPERSEDED"} and (
                    parse_utc(plan["lifecycle_at"]) <= parse_utc(effective)
                )
                # Expiry before availability is a timing fact; it needs no new
                # (possibly reversed) market request and cannot create a fill.
                window = (
                    original
                    if already_expired or already_terminated
                    else target_window(effective, plan["definition"]["exit_deadline"])
                )
                progress("核对参考方案")
                prepared = self._compute(compute_reference, plan, window, settings=settings, as_of=self.clock())
                commit_item(prepared, captures, (version, effective, plan["definition"]["exit_deadline"]))
        for trade in trade_inputs:
            progress("核对实际交易")
            detail = trade.to_dict()
            token = self._trade_token({k: detail[k] for k in ("trade", "deals")})
            commit_item(self._compute(compute_actual, trade, as_of=self.clock()), [], trade_token=token)
        with self.db.write() as uow:
            self.jobs.finish(uow, lease, result_ref,
                             message=f"本轮补算完成：处理 {done} 项，跳过 {skipped} 项；资料不足项等待新证据，不循环重算。")

    def _compute(self, function, *args, **kwargs):
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        return self._pool.submit(function, *args, **kwargs).result(timeout=120)

    def enqueue_due_reviews(self):
        """Persistent jobs, not an in-memory timer, record completed due work."""
        through = self.clock()
        with self.db.write() as uow:
            expired = self.plans.refresh_lifecycles(None, uow=uow, only_changed=True)
            for plan in expired:
                p = plan.to_dict()
                self._event(uow, "plan", p["plan_id"], p["revision"])
        schedules = []
        # Scanning/decompressing history never holds the single writer or blocks heartbeats.
        with self.db.read() as uow:
            page = self.analysis.list_forecasts(limit=50, cursor=self._scan_cursor, scope=uow).to_dict()
            self._scan_cursor = page["next_cursor"]
            count = 0
            for forecast in page["items"]:
                if self.jobs.active_review(uow, forecast["forecast_id"]):
                    continue
                current = self.analysis.get_version(forecast["current_version_id"], scope=uow).to_dict()
                due, evidence = [], []
                for h in current["answer"]["horizons"]:
                    if parse_utc(h["end_at"]) + timedelta(minutes=3) > parse_utc(through):
                        continue
                    token = self._review_token(current, current["answer"]["market_cutoff"], h["end_at"], uow)
                    if self.review.refresh_needed("forecast", current["forecast_id"],
                            version_id=current["version_id"], horizon=h["horizon_minutes"],
                            evidence_token=token, scope=uow):
                        due.append(h["end_at"])
                        evidence.append(["forecast", h["horizon_minutes"], token])
                for plan in self.plans.list_plans(current["version_id"], scope=uow):
                    p = plan.to_dict()
                    end = p["definition"]["exit_deadline"]
                    token = self._review_token(current,
                        max(p["available_at"], p["definition"]["valid_from"], key=parse_utc), end, uow)
                    if (parse_utc(end) + timedelta(seconds=5) <= parse_utc(through)
                            and self.review.refresh_needed("reference_plan", p["plan_id"],
                                version_id=current["version_id"], evidence_token=token, scope=uow)):
                        due.append(end)
                        evidence.append(["reference_plan", p["plan_id"], token])
                if not due:
                    continue
                key = "due:" + current["version_id"] + ":" + max(due)
                # A deterministic through bound makes restart enqueue hashes identical.
                bound = (max(map(parse_utc, due)) + timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
                if parse_utc(bound) > parse_utc(through):
                    continue
                fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
                schedules.append((key + ":evidence:" + fingerprint,
                                  {"forecast_id": forecast["forecast_id"], "through": bound}))
                count += 1
            trade_page = self.portfolio.list_trades(
                limit=50, cursor=self._trade_scan_cursor, scope=uow
            ).to_dict()
            self._trade_scan_cursor = trade_page["next_cursor"]
            for trade in trade_page["items"]:
                facts = self.portfolio.get_trade_metrics(trade["trade_id"], scope=uow).to_dict()
                if parse_utc(facts["available_at"]) > parse_utc(through):
                    continue
                token = self._trade_token(self.portfolio.get_trade(trade["trade_id"], scope=uow).to_dict())
                if not self.review.refresh_needed("actual_trade", trade["trade_id"], evidence_token=token, scope=uow):
                    continue
                generation = self.review.refresh_generation("actual_trade", trade["trade_id"], scope=uow)
                key = f"actual:{trade['trade_id']}:evidence:{token}:after:{generation}"
                bound = max(
                    facts["available_at"],
                    facts.get("association_available_at") or facts["available_at"],
                    key=parse_utc,
                )
                schedules.append((key, {
                        "forecast_id": None,
                        "through": bound,
                        "actual_only": True,
                        "trade_id": trade["trade_id"],
                    }))
                count += 1
        for key, values in schedules:
            with self.db.write() as uow:
                self._schedule_review(uow, key, values, through)
        return count

    def _schedule_review(self, uow, key, input_data, now):
        job = self.jobs.enqueue(uow, "review_refresh", input_data, dedup_key=key)
        if (
            job["state"] == "failed"
            and job.get("error")
            and job["error"].get("retryable")
            and (parse_utc(now) - parse_utc(job["updated_at"])).total_seconds() >= 30
        ):
            try:
                self.jobs.retry(job["job_id"], scope=uow)
            except DomainError as exc:
                if exc.code != "JOB_NOT_RETRYABLE":
                    raise

    def schedule_market_refresh(self):
        """One pending ordinary capture; missed periods never create a catch-up queue."""
        now = self.monotonic()
        if self._stop.is_set() or now < self._next_refresh_attempt:
            return None
        with self.db.write() as uow:
            job = self.jobs.enqueue(uow, "market_refresh", FACT_REFRESH_COMMAND, coalesce_active=True)
        self._next_refresh_attempt = now + FACT_REFRESH_INTERVAL_SECONDS
        return job

    def _run_market_refresh(self):
        # CPU reviews run on the other worker. All SDK calls still use the bridge's
        # serial channel and all business commits use the existing single writer.
        while not self._stop.is_set():
            try:
                self.schedule_market_refresh()
                if not self.process_once(kind="market_refresh"):
                    self._stop.wait(0.2)
            except Exception:
                log.exception("Fact refresh worker iteration failed")
                self._next_refresh_attempt = self.monotonic() + FACT_REFRESH_RETRY_SECONDS
                self._stop.wait(1)

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.review is not None and time.monotonic() >= self._next_due_scan:
                    self.enqueue_due_reviews()
                    if getattr(self,'analysis_lab',None):self.analysis_lab.comparison.enqueue_due()
                    self._next_due_scan = time.monotonic() + 30
                if not self.process_once(exclude_kinds=("market_refresh",)):
                    self._stop.wait(0.2)
            except Exception:
                log.exception("Persistent worker iteration failed")
                self._stop.wait(1)

    def _run_guidance(self):
        while not self._stop.is_set():
            try:
                self.guidance.tick()
                if getattr(self,'daily',None):self.daily.tick_guidance()
                self.guidance.deliver_once()
            except Exception:
                log.exception("Local guidance iteration failed")
            self._stop.wait(1)

    def _run_model(self):
        while not self._stop.is_set():
            try:
                if not self.model_api.process_once(self):
                    self._stop.wait(0.2)
            except Exception:  # noqa: BLE001 - provider exceptions may contain credential-bearing requests
                log.error("Model I/O worker stopped this iteration; no automatic HTTP resend")
                self._stop.wait(1)

    def _run_daily_model(self):
        while not self._stop.is_set():
            try:
                if not self.model_api.process_once(self,kind='daily_model_call'):self._stop.wait(0.2)
            except Exception:
                log.error('Daily model worker failed; durable send state prevents replay')
                self._stop.wait(1)

    def _run_comparison(self):
        while not self._stop.is_set():
            try:
                if not self.process_once(kind='comparison_run'):
                    self.analysis_lab.comparison.recover()
                    self._stop.wait(1)
            except Exception:
                log.error('Comparison worker iteration ended; no automatic model resend')
                self._stop.wait(1)
