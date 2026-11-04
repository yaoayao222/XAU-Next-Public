"""Evidence for a current close comparison, separate from whole-history coverage."""
from decimal import Decimal

from .values import parse_utc

PERIODS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}
# The existing bridge explicitly diagnoses only historical gaps with this reason.
# Any other incomplete-source reason remains a blocker; it is not inferred away.
HISTORY_GAPS = "区间存在缺柱或非交易时段，未补造 K 线"


def latest_closed_observation(series, cutoff, now, max_age_ms):
    timeframe = series["timeframe"]
    source = series.get("status", {})
    result = {"state": "unavailable", "source_state": source.get("state", "unavailable"),
              "timeframe": timeframe, "window_start": None, "window_end": None,
              "bars_checked": 0, "bar": None, "reasons": []}

    def missing(reason):
        result["reasons"] = [timeframe + "：" + reason]
        return result

    period = PERIODS.get(timeframe)
    if not period or series.get("price_basis") not in {"bid", "ask", "mid"}:
        return missing("周期或K线报价侧不明确，等待可信行情。")
    gap_only = (source.get("state") == "incomplete" and bool(source.get("reasons"))
                and all(reason == HISTORY_GAPS for reason in source["reasons"]))
    if source.get("state") != "available" and not gap_only:
        return missing("来源尚不可用（" + "；".join(source.get("reasons", [])) + "），等待事实恢复。")
    current, frozen = parse_utc(now), parse_utc(cutoff)
    captured = source.get("captured_at")
    if frozen > current or not captured or not 0 <= (current - parse_utc(captured)).total_seconds() * 1000 <= max_age_ms:
        return missing("本次K线读取时间缺失、过时或晚于当前时间，等待新的事实。")
    bars = sorted((bar for bar in series.get("bars", []) if bar["closed"] and parse_utc(bar["close_at"]) <= frozen),
                  key=lambda bar: parse_utc(bar["close_at"]))
    if not bars or (gap_only and len(bars) < 2):
        return missing("没有足够的已收盘K线核验最近窗口。")
    window = bars[-2:]
    for index, bar in enumerate(window):
        start, end = parse_utc(bar["open_at"]), parse_utc(bar["close_at"])
        if ((end - start).total_seconds() != period
                or sum(parse_utc(other["close_at"]) == end for other in bars) != 1
                or (index and parse_utc(window[index - 1]["close_at"]) != start)):
            return missing("最近闭柱窗口存在缺柱、重复或周期长度异常。")
        close = Decimal(bar["close"])
        if not close.is_finite() or close <= 0:
            return missing("最近闭柱价格无效。")
    latest = window[-1]
    if not 0 <= (current - parse_utc(latest["close_at"])).total_seconds() <= period + max_age_ms / 1000:
        return missing("最近闭柱已过时，等待新的完整闭柱。")
    result.update(state="available", window_start=window[0]["open_at"], window_end=latest["close_at"],
                  bars_checked=len(window), bar=latest,
                  reasons=["仅核验最近闭柱窗口；整段历史的缺口和原始来源状态仍保留。"] if gap_only else [])
    return result
