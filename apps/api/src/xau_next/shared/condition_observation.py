"""Shared current price-condition observation; no lifecycle or readiness decisions."""

from decimal import Decimal

from .closed_bars import latest_closed_observation
from .values import parse_utc


def rule_check(rule, market, now, max_age_ms, *, after=None, activation_at=None):
    """A tri-state result with concrete missing-evidence reasons."""
    def unknown(reason):
        return {"matched": None, "reasons": [reason]}

    if not market:
        return unknown("尚无当前行情，等待事实读取。")
    current, cutoff = parse_utc(now), parse_utc(market["market_cutoff"])
    if rule["kind"] == "quote_compare":
        quote = market.get("quote")
        if not quote or market["quote_status"]["state"] != "available":
            return unknown("当前报价不可用，等待事实恢复。")
        at = parse_utc(quote["event_time"])
        if at > cutoff or not 0 <= (current - at).total_seconds() * 1000 <= max_age_ms:
            return unknown("报价过时或时间异常，等待新的有效报价。")
        event = {"kind": "quote", **quote}
    elif rule["kind"] == "bar_close":
        candidates = [s for s in market.get("series", []) if s["timeframe"] == rule["timeframe"]]
        if len(candidates) != 1:
            return unknown(rule["timeframe"] + "：没有唯一的对应周期行情。")
        evidence = latest_closed_observation(candidates[0], market["market_cutoff"], now, max_age_ms)
        if evidence["state"] != "available":
            return {"matched": None, "reasons": evidence["reasons"]}
        bar = evidence["bar"]
        at = parse_utc(bar["close_at"])
        event = {"kind": "bar", "timeframe": rule["timeframe"], "close": bar["close"]}
    else:
        return unknown("该条件无法通过当前行情自动核验。")
    observation = rule.get("observation", "new_since_available")
    if observation == "new_since_activation":
        if not activation_at or at <= parse_utc(activation_at):
            return {"matched": False, "reasons": ["等待严格晚于激活事件的新观察；同事件不能作为后续确认。"]}
    if observation != "background" and after and at < parse_utc(after):
        return {"matched": False, "reasons": ["等待分析可用后的新观察，不补认之前的条件。"]}
    value = Decimal(event["close"] if rule["kind"] == "bar_close" else event[rule["quote_side"]])
    threshold = Decimal(rule["price"])
    matched = {"gt": value > threshold, "gte": value >= threshold,
               "lt": value < threshold, "lte": value <= threshold}[rule["operator"]]
    return {"matched": matched, "reasons": [], "observed_at": at.isoformat().replace("+00:00", "Z"), "condition": rule}
