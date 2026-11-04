"""Analysis freshness is independent of forecast end and actual holding duration."""

from datetime import timedelta

from xau_next.shared.analysis_policy import analysis_preferences
from xau_next.shared.decision_reason import stopping_reason
from xau_next.shared.entry_timing import analysis_latency
from xau_next.shared.values import parse_utc


def refresh_status(version, request, settings, *, now, conditions=None, plan_states=None, plan_reasons=None, observed_reassessments=None, candidate_deadlines=None):
    enabled = analysis_preferences(settings["values"])["enabled_styles"]
    frozen = (request or {}).get("analysis_preferences") or analysis_preferences({})
    output = []
    for style in enabled:
        item = {"trading_style": style, "state": "MISSING", "forecast_id": None, "version_id": None,
                "market_cutoff": None, "remind_at": None, "fresh_until": None,
                "reasons": ["尚无可用分析，请从最新行情创建分析包。"], "request_kind": "new",
                "pending_request_id": None}
        if version and request:
            cutoff = parse_utc(request["market_cutoff"])
            minutes = frozen[style + "_refresh_minutes"]
            end = min(cutoff + timedelta(minutes=minutes), parse_utc(request["window_end"]))
            latency = max(60, request.get("estimated_answer_seconds", 0), analysis_latency(request, version["available_at"])["import_delay_seconds"])
            lead = min(latency, minutes * 30)
            candidate_end = (candidate_deadlines or {}).get(style)
            preparation_target = min(end, parse_utc(candidate_end)) if candidate_end else end
            remind = preparation_target - timedelta(seconds=lead)
            item.update(forecast_id=version["forecast_id"], version_id=version["version_id"],
                        market_cutoff=request["market_cutoff"],
                        fresh_until=end.isoformat().replace("+00:00", "Z"),
                        remind_at=remind.isoformat().replace("+00:00", "Z"), state="FRESH",
                        preparation_lead_seconds=lead,
                        preparation_target_at=preparation_target.isoformat().replace("+00:00", "Z"),
                        reasons=["分析仍在时效内；正常等待突破、回踩或入场检查不构成重新分析理由。"])
            if version["answer"]["schema_version"] not in {"1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"}:
                item.update(state="REASSESS", reasons=["旧回答保留为历史，需要新版分析后开启自动行动建议。"])
            elif parse_utc(now) >= end:
                item.update(state="STALE", reasons=["分析时效已到；暂停该模式新的入场通知，请导入新回答。"])
                item["stopping_reason"] = stopping_reason("ANALYSIS_EXPIRED", item["fresh_until"], deadline=item["fresh_until"])
            elif parse_utc(now) >= remind:
                item.update(state="DUE", reasons=["可开始准备下一份分析；这个提醒本身不取消当前候选，仍按其原条件和截止时间继续判断。"])
                if preparation_target < end:
                    item["reasons"].append("该候选入场截止早于分析时效，准备提醒按更早的候选截止安排。")
            evidence = (conditions or {}).get(style, [])
            checks = [value["matched"] if isinstance(value, dict) else value for value in evidence]
            saved_reason = (observed_reassessments or {}).get(style)
            if item["state"] != "STALE" and (saved_reason or any(value is True for value in checks)):
                item.update(state="REASSESS", reasons=["模型声明的结构化重评条件已出现，需要提前更新分析。"])
                matched = next((value for value in evidence if isinstance(value, dict) and value["matched"] is True), {})
                item["stopping_reason"] = saved_reason or stopping_reason("REASSESSMENT_CONDITION", matched.get("observed_at"), condition=matched.get("condition"))
            elif item["state"] != "STALE" and any(value is None for value in checks):
                reasons = list(dict.fromkeys(reason for value in evidence if isinstance(value, dict) and value["matched"] is None
                                             for reason in value["reasons"]))
                item.update(state="WAITING_DATA", reasons=(reasons or ["重评条件所需行情暂不可核验。"])
                            + ["系统等待事实恢复后自动复核；无需因此重复分析或导入。"])
            if (plan_states or {}).get(style) in {"INVALIDATED", "MISSED", "EXPIRED"} and item["state"] != "STALE":
                item.update(state="REASSESS", reasons=["该模式原方案已失效、到期或错过，需分析新的机会。"])
            if (plan_states or {}).get(style) == "USED" and item["state"] != "STALE" and item.get("stopping_reason", {}).get("code") != "REASSESSMENT_CONDITION":
                item.update(state="REASSESS", reasons=["一次性方案已使用；已有持仓继续管理，新的入场机会需要新的分析。"])
            if (plan_states or {}).get(style) == "REFRESH_REQUIRED" and item["state"] not in {"STALE", "REASSESS"}:
                item.update(state="REASSESS", reasons=["当前候选不具备入场资格，需要按最新事实重新分析；原期限不会自动延长。"])
            if item["state"] == "REASSESS" and (plan_reasons or {}).get(style):
                item["stopping_reason"] = plan_reasons[style]
        item["blocks_entry"] = item["state"] in {"STALE", "REASSESS", "MISSING", "WAITING_DATA"}
        output.append(item)
    return output
