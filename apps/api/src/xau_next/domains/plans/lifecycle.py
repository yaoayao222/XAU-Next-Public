"""One-way lifecycle based on observable events, never intrabar guesses."""

from copy import deepcopy
from decimal import Decimal

from xau_next.shared.values import parse_utc
from xau_next.shared.closed_bars import PERIODS, HISTORY_GAPS

TERMINAL = {"INVALIDATED", "EXPIRED", "SUPERSEDED"}


def matches(rule, event):
    kind = rule["kind"]
    if kind == "immediate":
        return event["kind"] == "effective"
    if kind == "manual_observation":
        return event["kind"] == "manual" and event.get("condition_role") == event.get("role")
    if kind == "bar_close":
        if event["kind"] != "bar" or event["timeframe"] != rule["timeframe"]:
            return False
        price = Decimal(event["close"])
    elif kind == "quote_compare":
        if event["kind"] != "quote":
            return False
        price = Decimal(event[rule["quote_side"]])
    else:
        return False
    threshold = Decimal(rule["price"])
    return {
        "gt": price > threshold,
        "gte": price >= threshold,
        "lt": price < threshold,
        "lte": price <= threshold,
    }[rule["operator"]]


def advance(record, market, now):
    r = deepcopy(record)
    if r["lifecycle"] in TERMINAL:
        return r
    p = r["definition"]
    effective = max(parse_utc(r["available_at"]), parse_utc(p["valid_from"]))
    deadline, current = parse_utc(p["entry_deadline"]), parse_utc(now)
    events = [{"kind": "effective", "at": effective.isoformat().replace("+00:00", "Z")}]
    new_policy = (r.get("condition_policy_version") == "condition-observation/1" or
                  r.get("entry_timing", {}).get("policy_version") == "entry-observation/3")
    if market:
        cutoff = parse_utc(market["market_cutoff"]) if new_policy else current
        if (market["quote"] and market["quote_status"]["state"] == "available" and
                (not new_policy or parse_utc(market["quote"]["event_time"]) <= cutoff)):
            events.append({"kind": "quote", "at": market["quote"]["event_time"], **market["quote"]})
        for series in market["series"]:
            status = series.get("status", {})
            if new_policy and (status.get("state") != "available" and not
                    (status.get("state") == "incomplete" and status.get("reasons") == [HISTORY_GAPS])):
                continue
            if new_policy and (not status.get("captured_at") or parse_utc(status["captured_at"]) > current):
                continue
            if new_policy and (sum(s["timeframe"] == series["timeframe"] for s in market["series"]) != 1
                    or series.get("price_basis") not in {"bid", "ask", "mid"}):
                continue
            # An unavailable/partial series may still contain real observed closes;
            # no missing bar is synthesized and no high/low is used as event order.
            for bar in series["bars"]:
                eligible = bar["closed"]
                if new_policy:
                    end = parse_utc(bar["close_at"])
                    duration = (end - parse_utc(bar["open_at"])).total_seconds()
                    eligible = (eligible and end <= cutoff and duration == PERIODS.get(series["timeframe"])
                        and sum(other["close_at"] == bar["close_at"] for other in series["bars"]) == 1)
                if eligible:
                    events.append(
                        {
                            "kind": "bar",
                            "timeframe": series["timeframe"],
                            "at": bar["close_at"],
                            "close": bar["close"],
                        }
                    )
    for obs in r["manual_observations"]:
        events.append({"kind": "manual", "at": obs["recorded_at"], "condition_role": obs["condition_role"]})
    grouped = {}
    for event in events:
        t = parse_utc(event["at"])
        if effective <= t < deadline and t <= current:
            grouped.setdefault(t, []).append(event)
    for at in sorted(grouped):
        batch = grouped[at]
        activates = r["lifecycle"] == "WAITING_TRIGGER" and any(
            matches(p["activation"], {**e, "role": "activation"}) for e in batch)
        # Replayed candles before the saved activation may only break the background.
        post_enabled = activates or (r["lifecycle"] == "ACTIVE" and at >= parse_utc(r["lifecycle_at"]))
        background = any(matches(p["invalidation"], {**e, "role": "invalidation"}) for e in batch)
        post_rule = p.get("post_activation_invalidation")
        post = post_enabled and post_rule and any(matches(post_rule, e) for e in batch)
        if background or post:
            r.update(lifecycle="INVALIDATED", lifecycle_at=at.isoformat().replace("+00:00", "Z"))
            if "post_activation_invalidation" in p:
                r["invalidation_phase"] = "background" if background else "post_activation"
            return r
        if activates:
            r.update(lifecycle="ACTIVE", lifecycle_at=at.isoformat().replace("+00:00", "Z"))
            if p.get("trading_style"):
                r["activation_observed_at"] = now
    if current >= deadline:
        r.update(lifecycle="EXPIRED", lifecycle_at=p["entry_deadline"])
    return r
