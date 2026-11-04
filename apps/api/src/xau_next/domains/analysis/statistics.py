"""Small reproducible descriptions and frozen benchmarks, never invented probabilities."""

from datetime import timedelta
from decimal import Decimal, localcontext

from xau_next.shared.values import parse_utc

MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}


def decimal(value):
    return format(value.quantize(Decimal("0.00000001")), "f").rstrip("0").rstrip(".") or "0"


def closed_series(market):
    cutoff = parse_utc(market["market_cutoff"])
    result = {}
    for series in market.get("series", []):
        bars = sorted(
            (b for b in series["bars"] if b["closed"] and parse_utc(b["close_at"]) <= cutoff),
            key=lambda b: parse_utc(b["open_at"]),
        )
        result[series["timeframe"]] = (series, bars)
    return result


def contiguous(bars, minutes):
    duration = timedelta(minutes=minutes)
    return bool(bars) and all(
        parse_utc(bar["close_at"]) - parse_utc(bar["open_at"]) == duration
        and (i == 0 or bars[i - 1]["close_at"] == bar["open_at"])
        for i, bar in enumerate(bars)
    )


def build_statistics(market, horizons):
    with localcontext() as ctx:
        ctx.prec = 64
        return _build(market, horizons)


def _build(market, horizons):
    series = closed_series(market)
    structures = []
    for timeframe, (source, bars) in series.items():
        recent = bars[-20:]
        structures.append({
            "timeframe": timeframe, "closed_bar_count": len(bars),
            "source_status": source["status"]["state"], "price_basis": source["price_basis"],
            "start_at": recent[0]["open_at"] if recent else None,
            "end_at": recent[-1]["close_at"] if recent else None,
            "continuous": contiguous(recent, MINUTES[timeframe]),
            "recent_high": decimal(max(Decimal(b["high"]) for b in recent)) if recent else None,
            "recent_low": decimal(min(Decimal(b["low"]) for b in recent)) if recent else None,
            "last_three_closes": [b["close"] for b in recent[-3:]],
            "net_change": decimal(Decimal(recent[-1]["close"]) - Decimal(recent[0]["open"])) if recent else None,
        })
    distributions = []
    baselines = []
    for horizon in sorted(set(horizons)):
        choices = [(tf, s, bs) for tf, (s, bs) in series.items()
                   if horizon % MINUTES[tf] == 0 and s["price_basis"] == "bid"]
        # Choose the source with the largest frozen history coverage. Never merge sources.
        choices.sort(key=lambda x: (len(x[2]) * MINUTES[x[0]], -MINUTES[x[0]]), reverse=True)
        if not choices:
            distributions.append({"horizon_minutes": horizon, "state": "missing", "sample_count": 0})
            continue
        tf, source, bars = choices[0]
        size, samples, gaps = horizon // MINUTES[tf], [], 0
        for end in range(len(bars), size - 1, -size):
            chunk = bars[end - size:end]
            if not contiguous(chunk, MINUTES[tf]):
                gaps += 1
                continue
            samples.append(Decimal(chunk[-1]["close"]) - Decimal(chunk[0]["open"]))
        samples.sort()

        def quantile(ratio, sample_values=tuple(samples)):
            position = Decimal(len(sample_values) - 1) * ratio
            low = int(position)
            high = min(low + 1, len(sample_values) - 1)
            return decimal(sample_values[low] + (sample_values[high] - sample_values[low]) * (position - low))

        distributions.append({
            "horizon_minutes": horizon, "timeframe": tf, "price_basis": "bid",
            "state": "available" if len(samples) >= 20 else "insufficient_samples",
            "sample_count": len(samples), "skipped_incomplete_windows": gaps,
            "unused_oldest_bars": len(bars) % size,
            "start_at": bars[0]["open_at"] if bars else None,
            "end_at": bars[-1]["close_at"] if bars else None,
            "source_status": source["status"]["state"],
            "delta_quantiles": {"p10": quantile(Decimal(".1")), "p50": quantile(Decimal(".5")),
                                "p90": quantile(Decimal(".9"))} if len(samples) >= 20 else None,
        })
        lookback = bars[-min(len(bars), max(size, 2)):]
        if lookback and contiguous(lookback, MINUTES[tf]):
            last = Decimal(lookback[-1]["close"])
            change = last - Decimal(lookback[0]["open"])
            elapsed = len(lookback) * MINUTES[tf]
            baselines.append({"horizon_minutes": horizon, "price_basis": "bid", "anchor_price": decimal(last),
                              "anchor_at": lookback[-1]["close_at"], "no_change": decimal(last),
                              "fixed_drift": decimal(last + change * Decimal(horizon) / elapsed),
                              "lookback_minutes": elapsed})
    return {"method_version": "closed-history-description/1", "snapshot_id": market["snapshot_id"],
            "market_cutoff": market["market_cutoff"], "structure": structures,
            "non_overlapping_change_distributions": distributions, "frozen_baselines": baselines,
            "limitations": ["价格变化分布是描述统计，不是当前方案胜率。",
                            "仅用本包同源完整闭柱；历史覆盖不足不能靠模型补造。",
                            "未接入外部经济事件、美元或利率数据；不能据此推断没有事件。"]}
