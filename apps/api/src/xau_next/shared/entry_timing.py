"""Pure, versioned observation-window policy shared by import and live guidance."""
from datetime import timedelta
from math import ceil, floor

from .analysis_policy import analysis_preferences
from .values import parse_utc

POLICY_VERSION = "entry-observation/2"
PERIOD_SECONDS = {"M1": 60, "M5": 300, "M15": 900}


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def next_close(at, seconds):
    # A close exactly at effective_from is eligible, consistently with lifecycle.
    return at.fromtimestamp(ceil(at.timestamp() / seconds) * seconds, tz=at.tzinfo)


def analysis_latency(request, available_at):
    cutoff, created, available = map(parse_utc, (request["market_cutoff"], request.get("created_at", request["market_cutoff"]), available_at))
    return {"request_created_at": stamp(created),
            "cutoff_to_request_seconds": max(0, floor((created - cutoff).total_seconds())),
            "request_to_available_seconds": max(0, floor((available - created).total_seconds())),
            "import_delay_seconds": max(0, floor((available - cutoff).total_seconds()))}


def timing_reference(samples=None):
    samples = (samples or [])[:5]
    seconds = max(60, *(ceil((parse_utc(s["available_at"]) - parse_utc(s["created_at"])).total_seconds()) for s in samples)) if samples else 300
    return {"policy_version": POLICY_VERSION, "planning_delay_seconds": seconds,
            "sample_count": len(samples), "samples": samples,
            "basis": "max_recent_five_same_source_manual_answers" if samples else "initial_five_minute_assumption",
            "note": "只估计请求创建至回答导入的耗时；优先最近五份同来源新版人工流程，采用其中最长耗时预留。小样本不保证下一次耗时；资料创建前年龄另计，不延长分析时效。"}


def entry_timing(plan, request, available_at, *, enforced=True):
    cutoff = parse_utc(request["market_cutoff"])
    available = parse_utc(available_at)
    effective = max(available, parse_utc(plan["valid_from"]))
    preferences = {**analysis_preferences({}), **request.get("analysis_preferences", {})}
    minutes = preferences[(plan.get("trading_style") or "intraday") + "_refresh_minutes"]
    fresh_until = min(cutoff + timedelta(minutes=minutes), parse_utc(request["window_end"]))
    deadline = min(parse_utc(plan["entry_deadline"]), fresh_until)
    activation = plan["activation"]
    period = PERIOD_SECONDS.get(activation.get("timeframe")) if activation["kind"] == "bar_close" else None
    required = 2 if period else 0
    first = next_close(effective, period) if period else effective
    activation_ready = first + timedelta(seconds=period) if period else effective
    new_roles = request.get("answer_schema_version") == "1.6.0"
    check_times = [effective]
    for r in plan.get("entry_checks", []):
        role = r.get("observation", "new_since_available") if new_roles else "new_since_available"
        if role == "background":
            continue
        base = activation_ready if role == "new_since_activation" else effective
        if r["kind"] == "bar_close":
            period_check = PERIOD_SECONDS[r["timeframe"]]
            at = next_close(base, period_check)
            if role == "new_since_activation" and at <= base:
                at += timedelta(seconds=period_check)
            check_times.append(at)
        elif role == "new_since_activation":
            check_times.append(base + timedelta(seconds=1))
    checks_ready = max(check_times)
    reserve = max(60, period or 0) if plan["entry"]["mode"] == "limit_retest" else 60 if plan["entry"]["mode"] == "stop_breakout" else 10
    minimum = max(activation_ready, checks_ready) + timedelta(seconds=reserve)
    # Entry interval excludes its endpoint: a close exactly at the deadline does not count.
    closes = max(0, ceil((deadline - first).total_seconds() / period)) if period else None
    sufficient = deadline >= minimum
    reasons = []
    if not sufficient and enforced:
        reasons.append(f"导入后距有效入场截止仅{max(0, floor((deadline - effective).total_seconds()))}秒；"
                       f"需保留{required}次触发闭柱机会及{reserve}秒后续入场时间，实际有效截止早于最低观察所需截止。")
        reasons.append("保留原回答及期限；以最新资料取得新方案，不能自动延长期限。")
    return {"policy_version": "entry-observation/3" if new_roles else POLICY_VERSION, "enforced": enforced, "state": "SUFFICIENT" if sufficient else "INSUFFICIENT",
            "market_cutoff": request["market_cutoff"], "available_at": available_at,
            "effective_from": stamp(effective), "analysis_fresh_until": stamp(fresh_until),
            "effective_entry_deadline": stamp(deadline), "minimum_entry_deadline": stamp(minimum),
            **analysis_latency(request, available_at),
            "remaining_at_import_seconds": max(0, floor((deadline - available).total_seconds())),
            "activation_closes_at_import": closes, "required_activation_closes": required,
            "post_trigger_reserve_seconds": reserve, "reasons": reasons}


def planned_timing(plan, request, context):
    reference = context.get("timing_reference") or timing_reference()
    created = parse_utc(request.get("created_at", request["market_cutoff"]))
    # Frozen r6 requests retain their original estimate; new requests count both spans.
    if reference.get("policy_version") == "entry-observation/1":
        available = max(created, parse_utc(request["market_cutoff"]) + timedelta(seconds=reference["planning_delay_seconds"]))
    else:
        available = created + timedelta(seconds=reference["planning_delay_seconds"])
    return entry_timing(plan, request, stamp(available))
