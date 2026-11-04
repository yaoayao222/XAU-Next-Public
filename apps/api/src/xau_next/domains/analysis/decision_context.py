"""Bounded cutoff-safe observations for revisiting a thesis; never a trading signal."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from xau_next.shared.closed_bars import HISTORY_GAPS, PERIODS
from xau_next.shared.condition_observation import rule_check
from xau_next.shared.near_term import build_near_term
from xau_next.shared.values import parse_utc

VERSION = "decision-evidence/1"
TIMEFRAMES = ("M1", "M5", "M15", "H1")


def stamp(at):
    return at.isoformat().replace("+00:00", "Z")


def price(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def relation(values):
    if all(a < b for a, b in zip(values, values[1:])):
        return "rising"
    if all(a > b for a, b in zip(values, values[1:])):
        return "falling"
    return "flat" if len(set(values)) == 1 else "mixed"


def window(market, timeframe, cutoff):
    """Three aligned latest closed bars; never fall back to an older good window."""
    period = PERIODS[timeframe]
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    seconds = int((cutoff - epoch).total_seconds())
    end = epoch + timedelta(seconds=seconds - seconds % period)
    start = end - timedelta(seconds=3 * period)
    result = {"timeframe": timeframe, "state": "unknown", "price_basis": None,
              "window_start": stamp(start), "window_end": stamp(end), "sample_count": 0,
              "source_state": None, "change": None, "highs": None, "lows": None,
              "closes": None, "high_relation": None, "low_relation": None,
              "close_relation": None, "reason": None}
    candidates = [s for s in market.get("series", []) if s.get("timeframe") == timeframe]
    if len(candidates) != 1:
        result["reason"] = "来源缺失或存在同周期重复来源。"
        return result, None
    source = candidates[0]
    status = source.get("status") or {}
    result.update(price_basis=source.get("price_basis"), source_state=status.get("state"))
    gap_only = (status.get("state") == "incomplete" and status.get("reasons")
                and all(r == HISTORY_GAPS for r in status["reasons"]))
    try:
        captured = parse_utc(status["captured_at"])
        valid_source = (status.get("state") == "available" or gap_only) and end <= captured <= cutoff
    except (KeyError, TypeError, ValueError, AttributeError):
        valid_source = False
    if not valid_source or source.get("price_basis") != "bid":
        result["reason"] = "来源/采集时点不合格或不是Bid，不推断价格关系。"
        return result, None
    slots = {}
    try:
        for bar in source.get("bars", []):
            opened, closed = parse_utc(bar["open_at"]), parse_utc(bar["close_at"])
            # Future and unfinished bars are not evidence; a duplicate within the
            # selected window remains a blocker rather than being silently removed.
            if opened < end and closed > start:
                slots.setdefault(opened, []).append(bar)
        selected = []
        for i in range(3):
            opened = start + timedelta(seconds=i * period)
            items = slots.get(opened, [])
            if len(items) != 1:
                raise ValueError("缺柱或重复柱")
            bar = items[0]
            values = {k: price(bar.get(k)) for k in ("open", "high", "low", "close")}
            if (bar.get("closed") is not True or parse_utc(bar["close_at"]) != opened + timedelta(seconds=period)
                    or any(v is None for v in values.values())
                    or not values["low"] <= min(values["open"], values["close"])
                    <= max(values["open"], values["close"]) <= values["high"]):
                raise ValueError("未闭柱、周期或OHLC不合格")
            selected.append(bar)
        if len(slots) != 3:
            raise ValueError("窗口存在未对齐柱")
    except (KeyError, TypeError, ValueError, AttributeError):
        result["reason"] = "最近三根完整窗口缺失、重复、未对齐或OHLC异常；未回退旧窗口。"
        return result, None
    result.update(state="available", sample_count=3,
                  change=str(Decimal(selected[-1]["close"]) - Decimal(selected[0]["open"])),
                  reason="近端完整；整段历史缺口仍保留。" if gap_only else None)
    for key, field in (("high", "highs"), ("low", "lows"), ("close", "closes")):
        result[field] = [b[key] for b in selected]
        result[key + "_relation"] = relation([Decimal(v) for v in result[field]])
    return result, {**source, "bars": deepcopy(selected)}


def build_decision_context(request, context):
    market = context["market"]
    cutoff = parse_utc(request["market_cutoff"])
    observations, trusted = [], []
    for timeframe in TIMEFRAMES:
        observation, source = window(market, timeframe, cutoff)
        observations.append(observation)
        if source:
            trusted.append(source)
    age = context["settings"]["values"]["quote_max_age_ms"]
    near = context.get("near_term") or build_near_term(market, request["market_cutoff"])
    quote = near.get("quote") or {}
    quote_available = quote.get("state") == "available"
    prior = context.get("previous_analysis") or {}
    valid_prior = False
    try:
        valid_prior = (bool(prior.get("version_id")) and parse_utc(prior["available_at"]) <= cutoff
                       and parse_utc(prior["market_cutoff"]) <= cutoff)
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    previous = {"state": "available" if valid_prior else "unavailable", "version_id": None,
                "market_cutoff": None, "available_at": None, "horizons": [], "checks": []}
    if valid_prior:
        previous.update({k: prior[k] for k in ("version_id", "market_cutoff", "available_at")})
        for horizon in prior.get("horizons", [])[:5]:
            previous["horizons"].append({"horizon_minutes": horizon["horizon_minutes"],
                "direction": horizon["direction"], "end_at": horizon["end_at"],
                "state": "ended" if parse_utc(horizon["end_at"]) <= cutoff else "unfinished"})
        safe_market = {**market, "series": trusted}
        if not quote_available:
            safe_market = {**safe_market, "quote": None}
        rules = []
        for plan in prior.get("plans", [])[:2]:
            rules.append((plan.get("trading_style"), plan.get("plan_key"), "background_invalidation",
                          plan.get("invalidation") or {"kind": "unsupported"}))
        for item in (prior.get("analysis_basis") or {}).get("reassessment_conditions", [])[:8]:
            rules.append((item["trading_style"], None, "reassessment_condition", item["condition"]))
        for style, key, role, raw_rule in rules:
            rule = {k: raw_rule[k] for k in ("kind", "timeframe", "quote_side", "operator", "price") if k in raw_rule}
            try:
                checked = rule_check(rule, safe_market, request["market_cutoff"], age, after=prior["available_at"])
                matched = checked["matched"]
                state = ("unknown" if matched is None or (matched is False and checked["reasons"])
                         else "met_now" if matched else "not_met_now")
                observed = checked.get("observed_at")
            except (KeyError, TypeError, ValueError, InvalidOperation):
                state, observed = "unknown", None
            previous["checks"].append({"trading_style": style, "plan_key": key, "role": role,
                "condition": rule, "state": state, "observed_at": observed})
    planned = context.get("timing_reference") or {}
    return {"version": VERSION, "snapshot_id": market["snapshot_id"], "market_cutoff": request["market_cutoff"],
            "source_kind": market["source_kind"], "observations": observations, "previous": previous,
            "generation_budget_seconds": planned.get("planning_delay_seconds"),
            "meaning": "截止前已发生的价格关系与原谓词当前核验，不是未来方向、反转确认、胜率或入场许可。",
            "limits": "三根闭柱的高低关系不是确认摆动点。当前条件不成立不撤销历史失效；未读取完整生命周期、连续失败次数或成交资格轨迹。"
                      "已结束周期不是旧预测正确或错误。不得把这些观察作为强制转多/转空或自动拒绝候选的门槛。"}


def project_decision_context(value):
    """Allowlist every nested key; diagnostics/credentials must never ride along."""
    def fields(v, names):
        return {k: deepcopy(v[k]) for k in names.split() if k in v}
    result = fields(value, "version snapshot_id market_cutoff source_kind generation_budget_seconds meaning limits")
    result["observations"] = [fields(row, "timeframe state price_basis window_start window_end sample_count source_state change highs lows closes high_relation low_relation close_relation reason") for row in value.get("observations", [])]
    prior = value.get("previous") or {}
    result["previous"] = fields(prior, "state version_id market_cutoff available_at")
    result["previous"]["horizons"] = [fields(h, "horizon_minutes direction end_at state") for h in prior.get("horizons", [])]
    result["previous"]["checks"] = []
    for check in prior.get("checks", []):
        row = fields(check, "trading_style plan_key role state observed_at")
        row["condition"] = fields(check.get("condition") or {}, "kind timeframe quote_side operator price")
        result["previous"]["checks"].append(row)
    return result
