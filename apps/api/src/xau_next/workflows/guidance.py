"""Orchestrate domain-owned advice and durable local delivery without trading capability."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from xau_next.domains.analysis.public import market_scope_key
from xau_next.shared.analysis_policy import analysis_preferences
from xau_next.shared.entry_timing import analysis_latency, entry_timing
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc
from xau_next.shared.holding import digest

LABELS = {"scalp": "超短线", "intraday": "日内"}


def wire(value):
    return value.to_dict() if isinstance(value, Snapshot) else value


class GuidanceCoordinator:
    def __init__(self, *, db, analysis, plans, portfolio, market, settings, review, notifications, events, clock):
        self.db, self.analysis, self.plans, self.portfolio = db, analysis, plans, portfolio
        self.market, self.settings, self.review = market, settings, review
        self.notifications, self.events, self.clock = notifications, events, clock

    def snapshot(self, scope, *, market=None, settings=None, portfolio=None, forecast=None, plans=None, evaluations=None, include_positions=True):
        now = self.clock()
        settings = wire(settings or self.settings.get_snapshot(scope=scope))
        market = wire(market or self.market.current(settings, scope=scope))
        portfolio = wire(portfolio or self.portfolio.get_portfolio(scope=scope))
        forecast = wire(forecast or self.analysis.latest_for_market(market, scope=scope)) if market else None
        version = wire(self.analysis.get_version(forecast["current_version_id"], scope=scope)) if forecast else None
        request = wire(self.analysis.get_request(version["request_id"], scope=scope)) if version else None
        plans = plans if plans is not None else ([wire(p) for p in self.plans.list_plans(version["version_id"], scope=scope)] if version else [])
        if evaluations is None:
            evaluations = []
            for plan in plans:
                page = self.plans.list_evaluations(plan["plan_id"], scope=scope, limit=1).to_dict()["items"]
                evaluations.extend(page)
        conditions = {}
        for item in ((version or {}).get("answer", {}).get("analysis_basis") or {}).get("reassessment_conditions", []):
            conditions.setdefault(item["trading_style"], []).append(self.plans.rule_check(
                item["condition"], market, now, settings["values"]["quote_max_age_ms"], after=version["available_at"]))
        observed_reassessments = self.analysis.observed_reassessments(version["version_id"], scope=scope) if version else {}
        candidate_deadlines = {p["definition"]["trading_style"]: p["definition"]["entry_deadline"] for p in plans if p["definition"].get("trading_style")}
        refresh = self.analysis.refresh_status(version, request, settings, now=now, conditions=conditions,
            observed_reassessments=observed_reassessments, candidate_deadlines=candidate_deadlines)
        advices = []
        for plan in plans:
            policy = next((r for r in refresh if r["trading_style"] == plan["definition"].get("trading_style")), None)
            evaluation = next((e for e in evaluations if e["plan_id"] == plan["plan_id"]), None)
            usage = wire(self.portfolio.entry_usage(plan["plan_id"], available_at=plan["available_at"],
                account_scope_id=market["account_scope_id"], symbol=market["symbol"], scope=scope)) if market else None
            advice = self.plans.entry_advice(plan, market, portfolio, settings, evaluation, policy, now=now, usage=usage)
            if request and plan["definition"].get("trading_style"):
                advice["entry_timing"] = plan.get("entry_timing") or entry_timing(
                    plan["definition"], request, plan["available_at"], enforced=False)
                advice["entry_timing"] = {**advice["entry_timing"], **analysis_latency(request, plan["available_at"])}
            old = self.plans.last_action(plan["plan_id"], scope=scope)
            if old and old["state"] == "MISSED" and advice["state"] in {"READY", "WAITING", "BLOCKED"}:
                advice.update(state="MISSED", title=old["title"], reasons=old["reasons"])
                advice.pop("stopping_reason", None)
                if old.get("stopping_reason"):
                    advice["stopping_reason"] = old["stopping_reason"]
            advices.append(advice)
        ready = [a for a in advices if a["state"] == "READY"]
        ready.sort(key=lambda a: (next(p["definition"]["role"] for p in plans if p["plan_id"] == a["plan_id"]) != "primary", a["plan_id"]))
        for item in ready[1:]:
            item.update(state="BLOCKED", title=LABELS[item["trading_style"]] + " · 等待另一方案处理",
                        reasons=["两个模式同时满足条件，按主方案优先，默认不同时叠加入场。"])
        states = {a["trading_style"]: a["state"] for a in advices if a["trading_style"]}
        plan_reasons = {a["trading_style"]: a["stopping_reason"] for a in advices if a.get("stopping_reason")}
        refresh = self.analysis.refresh_status(version, request, settings, now=now, conditions=conditions, plan_states=states,
            plan_reasons=plan_reasons, observed_reassessments=observed_reassessments, candidate_deadlines=candidate_deadlines)
        if version:
            pending = self.analysis.pending_after(version["version_id"], scope=scope)
            if pending:
                for r in refresh:
                    r["pending_request_id"] = pending.to_dict()["request_id"]
        position_advices, holding_observations = [], []
        if include_positions and portfolio and market:
            for order in portfolio["orders"]:
                if order["symbol"] != market["symbol"] or order["order"]["account_scope_id"] != market["account_scope_id"]:
                    continue
                plan = wire(self.plans.get_plan(order["plan_id"], scope=scope)) if order.get("plan_id") else None
                opened_at = self.portfolio.position_opened_at(order, scope=scope) if plan else None
                original = wire(self.analysis.get_version(plan["version_id"], scope=scope)) if plan else None
                rules = [r["condition"] for r in ((original or {}).get("answer", {}).get("analysis_basis") or {}).get("reassessment_conditions", [])
                         if r["trading_style"] == plan["definition"].get("trading_style")] if plan else []
                observed = self.portfolio.holding_events(order, opened_at, scope=scope)
                latest = self.analysis.holding_review(order, opened_at, scope=scope)
                pending = self.analysis.pending_holding_review(order, opened_at, scope=scope)
                advice = self.plans.position_advice(order, plan, market, settings, now=now, opened_at=opened_at,
                    reassessment_rules=rules, observed_events=observed, latest_review=latest,
                    pending_request_id=pending["request_id"] if pending else None)
                new_events = advice.pop("_holding_events", [])
                review_keys = advice.pop("_holding_review_keys", [])
                status = portfolio["orders_status"]
                age = (parse_utc(now) - parse_utc(status["captured_at"])).total_seconds() * 1000
                if status["state"] != "available" or not 0 <= age <= settings["values"]["quote_max_age_ms"]:
                    advice.update(state="UNKNOWN", reason_code="ORDERS_UNAVAILABLE", title="持仓事实待更新",
                                  reasons=["读取失败时不把保留的旧持仓当作当前仍存在的事实。"])
                    new_events = []
                    if advice.get("management", {}).get("latest_review"):
                        advice["management"]["latest_review"].update(current=False, stale_reasons=["当前持仓事实不可核验。"])
                holding_observations.append({"order": order, "opened_at": opened_at, "events": new_events, "review_keys": review_keys})
                position_advices.append(advice)
        scope_key = market_scope_key(market) if market else "no-market"
        return {"action_advice": advices, "analysis_refresh": refresh, "position_advice": position_advices,
                "candidate_assessments": (version or {}).get("answer", {}).get("candidate_assessments", []),
                "notifications": self.notifications.list(scope, scope_key=scope_key)}, {
                    "market": market, "portfolio": portfolio, "settings": settings, "version": version,
                    "request": request, "scope_key": scope_key, "holding_observations": holding_observations}

    def _event(self, uow, notification):
        revision = {"queued": 1, "sending": 2, "submitted": 3, "suppressed": 3, "failed": 3, "unknown": 3}[notification["state"]]
        self.events.append(uow, "resource.changed", resource={"kind": "notification", "id": notification["notification_id"]}, revision=revision)

    def _enqueue(self, uow, key, context, kind, resource_id, title, body, expires_at, extra):
        if context.get('model_label'):
            title=context['model_label']+' · '+title
            extra={**extra,'daily_model_label':context['model_label']}
        if context["market"]["source_kind"] != "mt5":
            title = "合成验收 · " + title
        notice, created = self.notifications.enqueue(uow, key, context["scope_key"],
            {"kind": kind, "resource_id": resource_id, "title": title, "body": body,
             "context": extra}, expires_at=expires_at)
        if created:
            self._event(uow, notice)

    def tick(self, *, forecast=None, model_label=None):
        """One read snapshot and one short commit; no native I/O holds the database writer."""
        with self.db.read() as scope:
            guidance, context = self.snapshot(scope,forecast=forecast,include_positions=not bool(model_label))
            context['model_label']=model_label
        if not context["market"]:
            return
        now = self.clock()
        now_dt = parse_utc(now)
        version = context["version"]
        with self.db.write() as uow:
            settings = self.settings.get_snapshot(scope=uow).to_dict()
            current = self.market.current(settings, scope=uow)
            current_forecast = (Snapshot.of(self.analysis.get_forecast(forecast['forecast_id'],scope=uow).to_dict()['forecast'])
                if forecast else self.analysis.latest_for_market(current, scope=uow) if current else None)
            current_portfolio = self.portfolio.get_portfolio(scope=uow).to_dict()
            if (not current or current.to_dict()["snapshot_id"] != context["market"]["snapshot_id"]
                    or current_portfolio["revision"] != context["portfolio"]["revision"]
                    or settings["revision"] != context["settings"]["revision"]
                    or (wire(current_forecast) or {}).get("current_version_id") != (version or {}).get("version_id")):
                return
            for advice in guidance["action_advice"]:
                old, changed, revision = self.plans.remember_action(advice, uow=uow)
                if changed:
                    self.events.append(uow, "resource.changed", resource={"kind": "action_advice", "id": advice["plan_id"]}, revision=revision)
                if advice["state"] == "READY":
                    zone = advice["allowed_zone"]
                    display_zone = ZoneInfo(settings["values"]["display_timezone"])
                    deadline = parse_utc(advice["entry_deadline"]).astimezone(display_zone).strftime("%H:%M:%S")
                    valid_until = parse_utc(advice["valid_until"]).astimezone(display_zone).strftime("%H:%M:%S")
                    body = (f"当前执行价 {advice['entry_price']}；允许范围 {zone['low']}—{zone['high']}；"
                            f"止损 {advice['stop_loss']}；目标 {advice['take_profit']}；手数 {advice['volume_lots']}；"
                            f"成交后最多 {advice['max_holding_minutes']} 分钟。最晚入场 {deadline}；"
                            f"本次提示有效至 {valid_until}（{settings['values']['display_timezone']}）。在 MT5 人工操作。")
                    self._enqueue(uow, "entry:" + advice["plan_id"], context, "entry", advice["plan_id"], advice["title"], body,
                                  advice["valid_until"], {"version_id": advice["version_id"], "state": "READY",
                                  "market_snapshot_id": advice["market_snapshot_id"], "quote_at": advice["quote_at"]})
                elif old and old["state"] == "READY":
                    self._enqueue(uow, f"withdraw:{advice['plan_id']}:{advice['state']}", context, "withdrawal", advice["plan_id"],
                                  "原入场提示已撤销", advice["title"] + "；" + "；".join(advice["reasons"]),
                                  (now_dt + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                                  {"version_id": advice["version_id"], "state": advice["state"]})
            for item in guidance["analysis_refresh"]:
                if not version or item["state"] not in {"DUE", "STALE", "REASSESS"}:
                    continue
                cause = item.get("stopping_reason")
                if cause and cause["code"] == "REASSESSMENT_CONDITION":
                    self.analysis.remember_reassessment(version["version_id"], item["trading_style"], cause, uow=uow)
                key = f"refresh:{version['version_id']}:{item['trading_style']}"
                # Existing unclassified notices already asked for an update; do not resend on upgrade.
                if self.notifications.contains(uow, key):
                    continue
                level = "preparation" if item["state"] == "DUE" else "required"
                action = next((a for a in guidance["action_advice"] if a["trading_style"] == item["trading_style"]), None)
                title = (LABELS[item["trading_style"]] + " · 可准备下一份分析" if level == "preparation" else
                         action["title"] if action else LABELS[item["trading_style"]] + (" · 分析时效已到" if item["state"] == "STALE" else " · 重评条件已确认"))
                body = "；".join(action["reasons"] if level == "required" and action else item["reasons"])
                if level == "required" and cause:
                    at = cause.get("occurred_at") or cause.get("deadline")
                    if at:
                        shown = parse_utc(at).astimezone(ZoneInfo(settings["values"]["display_timezone"])).strftime("%m/%d %H:%M:%S")
                        body += " 依据时间 " + shown + "。"
                    body += " 工作台保留对应条件、截止与下一步。"
                self._enqueue(uow, key + ":" + level, context, "refresh", version["version_id"],
                              title, body + " 请使用最新分析包取得回答后导入。",
                              (now_dt + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                              {"version_id": version["version_id"], "trading_style": item["trading_style"], "level": level})
            for item in guidance["position_advice"]:
                observation = next((o for o in context["holding_observations"] if o["order"]["order"] == item["order"]), None)
                if observation:
                    self.portfolio.remember_holding_events(observation["order"], observation["opened_at"], observation["events"], uow=uow)
                reassess = item.get("management", {}).get("reassessment_state") == "required"
                if item["state"] != "EXIT" and not (item["state"] == "HOLD" and reassess):
                    continue
                key = item["order"]["broker_id"]
                code = item["reason_code"] if item["state"] == "EXIT" else "HOLDING_REASSESS"
                event_keys = observation["review_keys"] if observation else []
                event_identity = (":" + digest(sorted(event_keys))) if code == "HOLDING_REASSESS" else ""
                self._enqueue(uow, f"position:{context['scope_key']}:{key}:{item['plan_id']}:{code}{event_identity}", context,
                              "position", key, item["title"] if item["state"] == "EXIT" else "持仓结构有变化，可手动重评目标",
                              "；".join(item["reasons"] if item["state"] == "EXIT" else item["management"]["reassessment_reasons"]), item["valid_until"],
                              {"order": item["order"], "reason_code": code, "event_keys": event_keys})
            # Reuse one fresh pending packet. If it becomes stale, append a new one; never rewrite it.
            needs = version and any(r["state"] in {"DUE", "STALE", "REASSESS"} for r in guidance["analysis_refresh"])
            if needs and not model_label:
                pending = self.analysis.pending_after(version["version_id"], scope=uow)
                market = context["market"]
                quote = market.get("quote")
                fresh = (quote and market["quote_status"]["state"] == "available" and 0 <=
                         (now_dt - parse_utc(quote["event_time"])).total_seconds() * 1000 <= settings["values"]["quote_max_age_ms"])
                if not pending and fresh:
                    command = {"kind": "new", "forecast_id": None, "base_version_id": None,
                               "market_snapshot_id": market["snapshot_id"]}
                    experience = self.review.get_context(market["market_cutoff"], scope=uow, include_candidates=True)
                    experience = self.review.enrich_context(experience, self.analysis.experience_predictions(experience, scope=uow),
                        selection_target=self.analysis.experience_target(command, market, settings, scope=uow))
                    try:
                        binding = self.market.capture_binding(market["snapshot_id"], scope=uow)
                        if not binding or not binding.get("portfolio_snapshot_id"):
                            raise DomainError("CAPTURE_BINDING_UNAVAILABLE", "行情缺少同批账户身份，等待下一次事实读取。", 409)
                        paired = self.portfolio.get_snapshot(binding["portfolio_snapshot_id"], scope=uow)
                        pending = self.analysis.create_request(command, market=Snapshot.of(market),
                            portfolio=paired, settings=Snapshot.of(settings), capture_binding=binding,
                            experience=experience, uow=uow)
                    except DomainError as error:
                        if error.code != "TIME_WINDOW_INVALID":
                            raise
                        return
                    self.events.append(uow, "resource.changed", resource={"kind": "analysis_request", "id": pending.to_dict()["request_id"]}, revision=1)
                if pending:
                    self.analysis.remember_auto_request(version["version_id"], pending.to_dict()["request_id"], uow=uow)

    def validate_delivery(self, notification):
        with self.db.read() as scope:
            forecast=None
            if notification['context'].get('daily_model_label'):
                version=self.analysis.get_version(notification['context']['version_id'],scope=scope).to_dict()
                forecast=self.analysis.get_forecast(version['forecast_id'],scope=scope).to_dict()['forecast']
                if forecast['current_version_id']!=version['version_id']:return False,'此模型已有自己的新版本，旧提示取消。'
            guidance, context = self.snapshot(scope,forecast=forecast,include_positions=not bool(forecast))
        if notification["scope_key"] != context["scope_key"]:
            return False, "来源已变化，旧通知取消。"
        if not analysis_preferences(context["settings"]["values"])["desktop_notifications"]:
            return False, "本机桌面通知已在设置中关闭；网页状态仍可查看。"
        kind, extra = notification["kind"], notification["context"]
        if kind == "entry":
            match = next((a for a in guidance["action_advice"] if a["plan_id"] == notification["resource_id"]), None)
            return bool(match and match["state"] == "READY" and match["version_id"] == extra["version_id"]), "当前已不满足该入场建议，取消旧通知。"
        if kind == "refresh":
            match = next((a for a in guidance["analysis_refresh"] if a["trading_style"] == extra["trading_style"]), None)
            states = {"DUE"} if extra.get("level") == "preparation" else {"STALE", "REASSESS"} if extra.get("level") == "required" else {"DUE", "STALE", "REASSESS"}
            return bool(match and match["version_id"] == extra["version_id"] and match["state"] in states), "分析已更新或提醒阶段已变化，旧提醒取消。"
        if kind == "withdrawal":
            match = next((a for a in guidance["action_advice"] if a["plan_id"] == notification["resource_id"]), None)
            return not match or match["state"] != "READY", "当前状态已变化。"
        match = next((a for a in guidance["position_advice"] if a["order"] == extra["order"]), None)
        if extra.get("reason_code") == "HOLDING_REASSESS":
            return bool(match and match["state"] == "HOLD" and match.get("management", {}).get("reassessment_state") == "required"), "该持仓重评提示已变化。"
        return bool(match and match["state"] == "EXIT" and match["reason_code"] == extra["reason_code"]), "持仓或退出条件已变化，旧通知取消。"

    def deliver_once(self):
        result = self.notifications.deliver_one(self.validate_delivery)
        if result:
            with self.db.write() as uow:
                self._event(uow, result)
        return result
