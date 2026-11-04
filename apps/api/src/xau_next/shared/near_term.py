from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from .closed_bars import HISTORY_GAPS
from .values import parse_utc


def _time(value):
    try:
        return parse_utc(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def _price(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _source(status):
    return {"source_state": status.get("state", "unavailable"),
            "source_reasons": list(status.get("reasons", [])),
            "captured_at": status.get("captured_at"), "observed_at": status.get("observed_at")}


def _quote(market, cutoff, errors):
    status = market.get("quote_status") or {}
    raw = market.get("quote") or {}
    result = {"state": "unknown", "reasons": list(errors), **_source(status),
              "price_basis": "bid_and_ask", "sample_count": 0, "bid": None, "ask": None,
              "spread": None, "event_time": raw.get("event_time"), "age_seconds": None}
    observed = _time(raw.get("event_time"))
    captured = _time(status.get("captured_at"))
    bid, ask = _price(raw.get("bid")), _price(raw.get("ask"))
    if status.get("state") != "available":
        result["reasons"].append("报价来源不可用或已过时。")
    if not observed or not cutoff or observed > cutoff:
        result["reasons"].append("报价时间缺失、无效或晚于截止点。")
    if not captured or (observed and captured < observed):
        result["reasons"].append("报价采集时间缺失或早于报价发生时间。")
    if bid is None or ask is None or ask < bid:
        result["reasons"].append("Bid/Ask缺失、无效或点差为负。")
    if not result["reasons"]:
        result.update(state="available", sample_count=1, bid=str(bid), ask=str(ask), spread=str(ask - bid),
                      age_seconds=(cutoff - observed).total_seconds())
    return result


def _select_series(market, timeframe):
    candidates = [series for series in market.get("series", []) if series.get("timeframe") == timeframe]
    if len(candidates) != 1:
        return {}, [timeframe + "来源缺失或存在多个同周期来源，不能静默混合。"]
    return candidates[0], []


def _aligned_end(cutoff, seconds):
    if cutoff is None:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = int((cutoff - epoch).total_seconds())
    return epoch + timedelta(seconds=elapsed - elapsed % seconds)


def _window(series, timeframe, seconds, count, cutoff, errors):
    end = _aligned_end(cutoff, seconds)
    start = end - timedelta(seconds=seconds * count) if end else None
    status = series.get("status") or {}
    result = {"state": "unknown", "reasons": list(errors), **_source(status), "timeframe": timeframe,
              "price_basis": series.get("price_basis", "unknown"), "sample_count": 0,
              "expected_sample_count": count, "window_start": _stamp(start), "window_end": _stamp(end),
              "continuity_complete": False}
    gap_only = (status.get("state") == "incomplete" and bool(status.get("reasons"))
                and all(reason == HISTORY_GAPS for reason in status["reasons"]))
    if status.get("state") != "available" and not gap_only:
        result["reasons"].append(timeframe + "来源状态不足以支持可信近端统计。")
    if series.get("price_basis") not in {"bid", "ask", "mid"}:
        result["reasons"].append(timeframe + "价格基准未知。")
    captured = _time(status.get("captured_at"))
    if not captured or (end and captured < end):
        result["reasons"].append(timeframe + "采集时间缺失或早于待统计闭柱。")
    if start is None or end is None:
        result["reasons"].append("冻结截止点无效。")
        return result, []
    check_start = start - timedelta(seconds=seconds) if gap_only and count == 1 else start
    slots = {}
    malformed = False
    for bar in series.get("bars", []):
        opened, closed = _time(bar.get("open_at")), _time(bar.get("close_at"))
        if opened is None or closed is None:
            malformed = True
            continue
        if opened >= end or closed <= check_start:
            continue
        slots.setdefault(opened, []).append(bar)
        if (closed - opened).total_seconds() != seconds or _aligned_end(opened, seconds) != opened:
            malformed = True
    valid = []
    missing = []
    duplicate = []
    invalid = []
    cursor = check_start
    while cursor < end:
        matching = slots.get(cursor, [])
        if not matching:
            missing.append(_stamp(cursor))
        elif len(matching) != 1:
            duplicate.append(_stamp(cursor))
        else:
            bar = matching[0]
            closed = _time(bar["close_at"])
            prices = {key: _price(bar.get(key)) for key in ("open", "high", "low", "close")}
            valid_prices = all(value is not None for value in prices.values())
            ordered = (valid_prices and prices["low"] <= min(prices["open"], prices["close"])
                       <= max(prices["open"], prices["close"]) <= prices["high"])
            if (bar.get("closed") is not True or closed != cursor + timedelta(seconds=seconds)
                    or closed > cutoff or not ordered):
                invalid.append(_stamp(cursor))
            elif cursor >= start:
                valid.append(bar)
        cursor += timedelta(seconds=seconds)
    result["sample_count"] = len(valid)
    if missing:
        result["reasons"].append("完整窗口缺柱：" + "、".join(missing))
    if duplicate:
        result["reasons"].append("完整窗口存在重复柱：" + "、".join(duplicate))
    if invalid:
        result["reasons"].append("完整窗口含未闭柱、未来柱、周期或OHLC异常：" + "、".join(invalid))
    if malformed:
        result["reasons"].append("柱时间缺失、周期长度异常或未按周期对齐。")
    if not result["reasons"] and len(valid) == count:
        result.update(state="available", continuity_complete=True)
        if gap_only:
            result["reasons"].append("近期窗口完整；来源整段历史缺口状态保留，未补造数据。")
    return result, valid


def _stats(metadata, bars):
    result = {**metadata, "change": None, "high": None, "low": None, "range": None,
              "first_open": None, "latest_close": None,
              "algorithm": "change=last.close-first.open; high=max(high); low=min(low); range=high-low"}
    if metadata["state"] == "available":
        high = max(Decimal(bar["high"]) for bar in bars)
        low = min(Decimal(bar["low"]) for bar in bars)
        result.update(change=str(Decimal(bars[-1]["close"]) - Decimal(bars[0]["open"])),
                      high=str(high), low=str(low), range=str(high - low),
                      first_open=bars[0]["open"], latest_close=bars[-1]["close"])
    return result


def _distance(price, basis, quote):
    result = {"state": "unknown", "reasons": [], "quote_basis": basis, "quote_price": None,
              "quote_event_time": quote["event_time"], "signed_distance": None, "absolute_distance": None,
              "spread_conversion": None, "algorithm": "signed_distance=structure_price-same_basis_quote"}
    if quote["state"] != "available":
        result["reasons"] = list(quote["reasons"])
        return result
    bid, ask = Decimal(quote["bid"]), Decimal(quote["ask"])
    current = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}[basis]
    delta = Decimal(price) - current
    result.update(state="available", quote_price=str(current), signed_distance=str(delta),
                  absolute_distance=str(abs(delta)))
    return result


def _extrema(metadata, bars, cutoff, quote):
    result = {**metadata, "high": None, "low": None, "age_basis": "bar_close_at",
              "algorithm": "window_high_low; ties=latest_bar; exact_tick_time_unknown"}
    if metadata["state"] != "available":
        return result
    for key in ("high", "low"):
        price = (max if key == "high" else min)(Decimal(bar[key]) for bar in bars)
        chosen = next(bar for bar in reversed(bars) if Decimal(bar[key]) == price)
        result[key] = {"price": str(price), "bar_open_at": chosen["open_at"],
                       "bar_close_at": chosen["close_at"],
                       "age_seconds": (cutoff - parse_utc(chosen["close_at"])).total_seconds(),
                       "quote_distance": _distance(str(price), metadata["price_basis"], quote)}
    return result


def _true_range(series, cutoff, errors):
    metadata, bars = _window(series, "M1", 60, 11, cutoff, errors)
    start = _time(metadata["window_start"])
    result = {**metadata, "window_start": _stamp(start + timedelta(minutes=1)) if start else None,
              "sample_count": 0, "expected_sample_count": 10, "input_bar_count": metadata["sample_count"],
              "expected_input_bar_count": 11, "previous_close_at": None, "value": None,
              "algorithm": "mean(max(high-low,abs(high-previous_close),abs(low-previous_close))); samples=10"}
    if metadata["state"] == "available":
        values = []
        for previous, bar in pairwise(bars):
            high, low, previous_close = Decimal(bar["high"]), Decimal(bar["low"]), Decimal(previous["close"])
            values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        result.update(sample_count=len(values), value=str(sum(values, Decimal(0)) / len(values)),
                      previous_close_at=bars[0]["close_at"])
    return result


def build_near_term(market, cutoff=None):
    frozen = market.get("market_cutoff") if cutoff is None else cutoff
    instant = _time(frozen)
    original_cutoff = _time(market.get("market_cutoff"))
    errors = []
    if instant is None or original_cutoff is None or instant > original_cutoff:
        errors.append("近端截止点无效或超出冻结快照截点，禁止使用未来事实。")
    if market.get("source_kind") not in {"mt5", "synthetic", "imported"}:
        errors.append("行情来源类型缺失或不可信。")
    quote = _quote(market, instant, errors)
    minute_series, minute_errors = _select_series(market, "M1")
    five_series, five_errors = _select_series(market, "M5")
    minute_windows = []
    for duration in (1, 3, 5, 10):
        metadata, bars = _window(minute_series, "M1", 60, duration, instant, errors + minute_errors)
        minute_windows.append({"window_minutes": duration, **_stats(metadata, bars)})
    extrema_metadata, extrema_bars = _window(minute_series, "M1", 60, 10, instant, errors + minute_errors)
    five_metadata, five_bars = _window(five_series, "M5", 300, 2, instant, errors + five_errors)
    return {"version": "near-term/1", "snapshot_id": market.get("snapshot_id"), "cutoff": frozen,
            "source_kind": market.get("source_kind"), "unit": "XAUUSD_price_dollars",
            "meaning": "已发生的局部价格描述统计；不是账户盈亏、MT5最小报价点或当前方案成功概率。",
            "quote": quote, "m1_windows": minute_windows,
            "local_extrema": _extrema(extrema_metadata, extrema_bars, instant, quote),
            "m5_structure": {**_stats(five_metadata, five_bars),
                             "local_extrema": _extrema(five_metadata, five_bars, instant, quote)},
            "m1_true_range": _true_range(minute_series, instant, errors + minute_errors)}


_SOURCE_FIELDS = ("state", "reasons", "source_state", "source_reasons", "captured_at", "observed_at")
_WINDOW_FIELDS = _SOURCE_FIELDS + ("timeframe", "price_basis", "sample_count", "expected_sample_count",
                                  "window_start", "window_end", "continuity_complete")
_STATS_FIELDS = _WINDOW_FIELDS + ("change", "high", "low", "range", "first_open", "latest_close", "algorithm")


def _project_fields(value, fields):
    if value is None:
        return None
    return {key: deepcopy(value[key]) for key in fields if key in value}


def _project_extrema(value):
    result = _project_fields(value, _WINDOW_FIELDS + ("age_basis", "algorithm"))
    if result is None:
        return None
    for key in ("high", "low"):
        if key not in value:
            continue
        point = value[key]
        result[key] = _project_fields(point, ("price", "bar_open_at", "bar_close_at", "age_seconds"))
        if point is not None and "quote_distance" in point:
            result[key]["quote_distance"] = _project_fields(
                point["quote_distance"], ("state", "reasons", "quote_basis", "quote_price", "quote_event_time",
                                          "signed_distance", "absolute_distance", "spread_conversion", "algorithm"))
    return result


def project_near_term(value):
    result = _project_fields(value, ("version", "snapshot_id", "cutoff", "source_kind", "unit", "meaning"))
    if result is None:
        return None
    if "quote" in value:
        result["quote"] = _project_fields(value["quote"], _SOURCE_FIELDS + (
            "price_basis", "sample_count", "bid", "ask", "spread", "event_time", "age_seconds"))
    if "m1_windows" in value:
        result["m1_windows"] = (None if value["m1_windows"] is None else
                                [_project_fields(window, _STATS_FIELDS + ("window_minutes",))
                                 for window in value["m1_windows"]])
    if "local_extrema" in value:
        result["local_extrema"] = _project_extrema(value["local_extrema"])
    if "m5_structure" in value:
        result["m5_structure"] = _project_fields(value["m5_structure"], _STATS_FIELDS)
        if value["m5_structure"] is not None and "local_extrema" in value["m5_structure"]:
            result["m5_structure"]["local_extrema"] = _project_extrema(value["m5_structure"]["local_extrema"])
    if "m1_true_range" in value:
        result["m1_true_range"] = _project_fields(value["m1_true_range"], _WINDOW_FIELDS + (
            "input_bar_count", "expected_input_bar_count", "previous_close_at", "value", "algorithm"))
    return result
