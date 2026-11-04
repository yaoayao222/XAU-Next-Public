"""Deterministic frozen-Bid direction assessment; never substitutes future bars."""

from datetime import timedelta

from xau_next.shared.values import parse_utc
from .common import dec, metric


def endpoint_quote(market, at, max_age_ms, *, as_of=None):
    """Last unique, valid quote at/before the endpoint, in a covered tick window."""
    target = parse_utc(at)
    captured = market.get("status", {}).get("captured_at")
    if as_of and captured and parse_utc(captured) > parse_utc(as_of):
        return None, "逐笔资料在评价时点之后才可用，未使用未来资料。"
    if market.get("tick_status") != "available":
        return None, "逐笔报价未证明完整覆盖，不能用分钟柱替代精确端点。"
    if (
        not market.get("from")
        or not market.get("to")
        or parse_utc(market["from"]) > target - timedelta(milliseconds=max_age_ms)
        or parse_utc(market["to"]) < target
    ):
        return None, "逐笔资料请求范围未覆盖端点及回看区间。"
    previous, chosen, ambiguous = None, None, False
    for tick in market.get("ticks", []):
        if as_of and tick.get("available_at") and parse_utc(tick["available_at"]) > parse_utc(as_of):
            return None, "逐笔资料在评价时点之后才可用，未使用未来资料。"
        event = parse_utc(tick["event_time"])
        if previous is not None and event < previous:
            return None, "逐笔报价时间顺序异常，未重排或猜测。"
        previous = event
        if event > target:
            continue
        if not 0 <= (target - event).total_seconds() * 1000 <= max_age_ms:
            continue
        if dec(tick["bid"]) <= 0 or dec(tick["ask"]) < dec(tick["bid"]):
            return None, "端点回看内存在无效双侧报价。"
        if chosen and event == parse_utc(chosen["event_time"]):
            ambiguous |= (dec(tick["bid"]), dec(tick["ask"])) != (dec(chosen["bid"]), dec(chosen["ask"]))
        else:
            chosen, ambiguous = tick, False
    if ambiguous:
        return None, "端点最后时刻存在不同报价，未猜测同毫秒先后。"
    return (chosen, None) if chosen else (None, "缺少端点之前足够新鲜的报价。")


def aligned_direction(
    frozen_market,
    market,
    start,
    end,
    direction,
    band,
    max_age_ms=5000,
    *,
    index=None,
    prefix="",
    frozen_start=True,
    as_of=None,
):
    quote = frozen_market.get("quote") if frozen_start else None
    error = None
    if frozen_start:
        status = frozen_market.get("quote_status", {})
        if (
            not quote
            or status.get("state") != "available"
            or not 0
            <= (parse_utc(start) - parse_utc(quote["event_time"])).total_seconds() * 1000
            <= max_age_ms
            or dec(quote["bid"]) <= 0
            or dec(quote["ask"]) < dec(quote["bid"])
            or (status.get("captured_at") and parse_utc(status["captured_at"]) > parse_utc(start))
        ):
            quote, error = None, "冻结Bid缺失、过时或晚于截止点；未改用下一根M1开盘。"
    else:
        quote, error = endpoint_quote(market, start, max_age_ms, as_of=as_of)
    ending, end_error = endpoint_quote(market, end, max_age_ms, as_of=as_of)
    if (
        any(market.get(k) != frozen_market.get(k) for k in ["account_scope_id", "symbol", "source_kind"])
        or market.get("price_basis") != "bid"
    ):
        quote, ending = None, None
        error = end_error = "冻结事实与历史来源或报价侧不匹配，未评分。"
    reason = error or end_error
    values = [
        metric(
            prefix + "anchor_price",
            dec(quote["bid"]) if quote else None,
            unit="price",
            state="available" if quote else "unknown",
            reason=error,
            index=index,
        ),
        metric(
            prefix + "anchor_at",
            observed=quote["event_time"] if quote else None,
            unit="utc",
            state="available" if quote else "unknown",
            reason=error,
            index=index,
        ),
        metric(
            prefix + "endpoint_price",
            dec(ending["bid"]) if ending else None,
            unit="price",
            state="available" if ending else "unknown",
            reason=end_error,
            index=index,
        ),
        metric(
            prefix + "endpoint_at",
            observed=ending["event_time"] if ending else None,
            unit="utc",
            state="available" if ending else "unknown",
            reason=end_error,
            index=index,
        ),
    ]
    if not quote or not ending:
        values += [
            metric(prefix + name, state="unknown", reason=reason, index=index)
            for name in ("direction", "delta", "direction_correct")
        ]
        return values
    change = dec(ending["bid"]) - dec(quote["bid"])
    actual = "long" if change > band else "short" if change < -band else "neutral"
    values += [
        metric(prefix + "direction", observed=actual, unit="label", index=index),
        metric(prefix + "delta", change, unit="price", index=index),
        metric(
            prefix + "direction_correct",
            None if direction == "uncertain" else int(actual == direction),
            state="not_applicable" if direction == "uncertain" else "available",
            index=index,
            reason="模型方向为uncertain，不计方向正确或错误。" if direction == "uncertain" else None,
        ),
    ]
    return values
