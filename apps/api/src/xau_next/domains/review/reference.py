from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from itertools import groupby, permutations

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

from .common import data, dec, get_series, inputs, iso, market_ids, metric, parameters, prepare, review_base


def matches(rule, event):
    if rule["kind"] == "immediate":
        return True
    if rule["kind"] == "manual_observation":
        return event["kind"] == "observation" and event["role"] == rule.get("_role")
    if rule["kind"] == "bar_close":
        if event["kind"] != "bar" or event["timeframe"] != rule["timeframe"]:
            return False
        price = dec(event["bar"]["close"])
    else:
        if event["kind"] != "quote" or event["quote"].get(rule["quote_side"]) is None:
            return False
        price = dec(event["quote"][rule["quote_side"]])
    threshold = dec(rule["price"])
    return {
        "gt": price > threshold,
        "gte": price >= threshold,
        "lt": price < threshold,
        "lte": price <= threshold,
    }[rule["operator"]]


def aggregate_m1(bars, minutes):
    """Derive one declared source only; never patch holes in a native series."""
    groups = {}
    for bar in bars:
        at = parse_utc(bar["open_at"])
        bucket = at.replace(minute=(at.minute // minutes) * minutes, second=0, microsecond=0)
        groups.setdefault(bucket, []).append(bar)
    result = []
    for start, values in sorted(groups.items()):
        values.sort(key=lambda item: parse_utc(item["open_at"]))
        if len(values) != minutes:
            continue
        if any(
            parse_utc(row["open_at"]) != start + timedelta(minutes=index)
            or parse_utc(row["close_at"]) != start + timedelta(minutes=index + 1)
            or not row.get("closed")
            for index, row in enumerate(values)
        ):
            continue
        result.append(
            {
                "open_at": iso(start),
                "close_at": iso(start + timedelta(minutes=minutes)),
                "open": values[0]["open"],
                "close": values[-1]["close"],
                "high": str(max(dec(row["high"]) for row in values)),
                "low": str(min(dec(row["low"]) for row in values)),
                "tick_volume": sum(row.get("tick_volume", 0) for row in values),
                "closed": True,
                "available_at": max(
                    (row.get("available_at", row["close_at"]) for row in values), key=parse_utc
                ),
            }
        )
    return result


def price_condition(definition, quote):
    side = "ask" if definition["side"] == "long" else "bid"
    if quote.get(side) is None or definition["entry"]["price"] is None:
        return False
    price, target = dec(quote[side]), dec(definition["entry"]["price"])
    if definition["entry"]["mode"] == "limit_retest":
        return price <= target if definition["side"] == "long" else price >= target
    return price >= target if definition["side"] == "long" else price <= target


def advance(state, event, definition, effective, entry_deadline, delay):
    if state["outcome"]:
        return state
    at, kind = event["at"], event["kind"]
    long = definition["side"] == "long"
    entry_side, exit_side = ("ask", "bid") if long else ("bid", "ask")
    if state["entered_at"] is None:
        if kind == "end_entry" or at >= entry_deadline:
            state["outcome"] = "EXPIRED_WITHOUT_ENTRY" if state["trigger_at"] else "NOT_TRIGGERED"
            state["completed_at"] = min(at, entry_deadline)
            return state
        if invalidates(state, event, definition):
            state["outcome"] = "EXPIRED_WITHOUT_ENTRY" if state["trigger_at"] else "NOT_TRIGGERED"
            state["reason"] = event.get("reason", "候选方案在参考入场前已失效。")
            state["completed_at"] = at
            return state
        if state["trigger_at"] is None and (
            kind == "immediate" or matches({**definition["activation"], "_role": "activation"}, event)
        ):
            state["trigger_at"], state["trigger_kind"] = at, kind
            state["breakout_armed"] = not state["last_beyond"]
        if kind != "quote":
            return state
        quote = event["quote"]
        crossing = price_condition(definition, quote)
        if state["trigger_at"] is not None and not crossing:
            state["breakout_armed"] = True
        state["last_beyond"] = crossing
        if state["trigger_at"] is None or quote.get(entry_side) is None:
            return state
        # A closed-bar trigger cannot consume an earlier or same-time quote from that candle.
        if state["trigger_kind"] == "bar" and parse_utc(quote["event_time"]) <= state["trigger_at"]:
            return state
        if parse_utc(quote["event_time"]) < effective:
            return state
        mode = definition["entry"]["mode"]
        if mode == "market":
            eligible = at >= state["trigger_at"] + timedelta(milliseconds=delay)
        else:
            eligible = crossing and (mode != "stop_breakout" or state["breakout_armed"])
        if not eligible:
            if crossing and mode == "stop_breakout" and not state["breakout_armed"]:
                state["reason"] = "突破价在生效/触发前已经越过，等待重新回到阈值另一侧后再观察。"
            return state
        state["entered_at"] = at
        state["entry_price"] = (
            dec(definition["entry"]["price"]) if mode == "limit_retest" else dec(quote[entry_side])
        )
        exit_at = parse_utc(definition["exit_deadline"])
        holding = definition.get("max_holding_minutes")
        state["exit_deadline"] = min(exit_at, at + timedelta(minutes=holding)) if holding else exit_at
    if kind != "quote" or event["quote"].get(exit_side) is None:
        return state
    quote, deadline = event["quote"], state["exit_deadline"]
    price = dec(quote[exit_side])
    stop, target = dec(definition["stop_loss"]), dec(definition["take_profit"])
    stop_hit, target_hit = (price <= stop, price >= target) if long else (price >= stop, price <= target)
    if at >= deadline:
        if parse_utc(quote["event_time"]) < deadline:
            return state
        if at > deadline + timedelta(seconds=5):
            state.update(
                outcome="DATA_INCOMPLETE",
                reason="退出截止后 5 秒内没有取得执行侧报价。",
                completed_at=deadline + timedelta(seconds=5),
            )
            return state
        if at == deadline and (stop_hit or target_hit):
            state.update(
                outcome="AMBIGUOUS",
                reason="同一时点到期退出与止损/目标同时成立，缺少可确定的先后。",
                completed_at=at,
            )
            return state
        state.update(outcome="TIME_EXIT", exit_price=price, exited_at=at, completed_at=at)
    elif stop_hit:
        state.update(outcome="SL", exit_price=price, exited_at=at, completed_at=at)
    elif target_hit:
        state.update(outcome="TP", exit_price=target, exited_at=at, completed_at=at)
    return state


def invalidates(state, event, definition):
    if event["kind"] == "terminated" or matches({**definition["invalidation"], "_role": "invalidation"}, event):
        return True
    post = definition.get("post_activation_invalidation")
    enabled = state["trigger_at"] is not None or event.get("activation_batch") or matches(definition["activation"], event)
    return bool(post and enabled and matches(post, event))


def state_key(state):
    return tuple((key, str(value)) for key, value in sorted(state.items()))


def coverage_ok(market, start, end):
    status = market.get("tick_status", "unavailable")
    if isinstance(status, dict):
        status = status.get("state")
    if status != "available" or not market.get("from") or not market.get("to"):
        return False
    if market.get("coverage", {}).get("complete") is False:
        return False
    if parse_utc(market["from"]) > start or parse_utc(market["to"]) < end:
        return False
    for gap in market.get("gaps", []):
        if parse_utc(gap["from"]) < end and parse_utc(gap["to"]) > start:
            return False
    return True


def cost_for_entry(settings, market, definition, at):
    if settings is None:
        return None, "费用配置未提供。"
    values = settings.get("values", settings)
    cost = values.get("cost_assumption")
    contract = market.get("contract")
    if not cost or not contract:
        return None, "费用或合约输入缺失。"
    if cost["account_scope_id"] != market.get("account_scope_id") or cost["symbol"] != market.get("symbol"):
        return None, "费用适用账户或品种与市场来源不一致。"
    if parse_utc(cost["valid_from"]) > at or (
        cost.get("valid_until") and parse_utc(cost["valid_until"]) <= at
    ):
        return None, "参考入场时没有有效费用假设。"
    currency = contract.get("profit_currency")
    if currency != "USD" or cost["currency"] != currency or contract.get("calculation_mode") != "linear_usd":
        return None, "缺少受支持的利润换算或合约计算依据。"
    fields = ["commission_roundtrip_per_lot", "slippage_reserve_per_lot", "other_cost_per_lot"]
    if any(cost.get(field) is None for field in fields):
        return None, "来回费用存在缺项，完整净结果未知。"
    if not cost.get("source_note", "").strip():
        return None, "费用没有来源说明。"
    return sum((dec(cost[field]) for field in fields), Decimal(0)), None


def compute_reference(plan, market, *, settings=None, as_of, simulation_delay_ms=0):
    plan, market, settings = data(plan), data(market), data(settings)
    if not isinstance(simulation_delay_ms, int) or simulation_delay_ms < 0:
        raise DomainError("REVIEW_INPUT_INVALID", "模拟延迟必须为非负整数毫秒。")
    definition = plan["definition"]
    method = "bidask_reference/2" if "post_activation_invalidation" in definition else "bidask_reference/1"
    frozen_scalp = definition.get("trading_style") == "scalp" and "scalp_preferences" in plan
    if frozen_scalp:
        method = "bidask_reference/3"
    effective = max(parse_utc(plan["available_at"]), parse_utc(definition["valid_from"]))
    entry_deadline, end, as_at = (
        parse_utc(definition["entry_deadline"]),
        parse_utc(definition["exit_deadline"]),
        parse_utc(as_of),
    )
    refs = market_ids(market)
    binding = inputs(
        version_id=plan["version_id"],
        plan_id=plan["plan_id"],
        scope="executable_plan",
        market_ids=refs,
        settings_revision=settings.get("revision") if settings else None,
        method=parameters(
            price_basis="bidask",
            execution_model_version=method,
            simulation_delay_ms=simulation_delay_ms,
            execution_quote_wait_ms=5000,
        ),
    )
    result = review_base(
        "reference_plan",
        plan["plan_id"],
        method,
        iso(effective),
        iso(end),
        binding,
        refs + [plan["version_id"], plan["plan_id"]],
    )
    frozen = {"plan": plan, "market": market, "settings": settings, "delay_ms": simulation_delay_ms}
    if effective > as_at:
        raise DomainError("REVIEW_NOT_DUE", "方案尚未实际可用，不能提前进行参考回放。", 409)
    if frozen_scalp:
        result["reasons"] = [
            "新超短线偏好方案采用bidask_reference/3；当前参考输入缺少逐时点完整入场资格证据，结果保持未知。",
            "报价路径完整也不能证明当时的新闭柱观察、执行侧有效区间、尺度距离、全部入场检查、费用预算及保证金同时通过。",
            "冻结导入诊断或当前适配结果不能代替历史时点资格；本版本不套用旧精确限价成交假设，不生成参考成交或净盈亏。",
        ]
        if not refs:
            result["reasons"].append("同时缺少已持久化的行情快照引用。")
        return prepare(result, frozen, as_of)
    if not refs:
        result["reasons"] = ["参考回放缺少已持久化的行情快照引用。"]
        return prepare(result, frozen, as_of)
    if plan.get("entry_timing", {}).get("enforced") and plan["entry_timing"]["state"] == "INSUFFICIENT":
        result["reasons"] = ["实际导入后观察预算不足，方案不具备入场资格；不生成参考成交。"]
        return prepare(result, frozen, as_of)
    if effective >= entry_deadline or (
        plan.get("lifecycle") in {"INVALIDATED", "SUPERSEDED"}
        and parse_utc(plan["lifecycle_at"]) <= effective
    ):
        result.update(
            outcome="EXPIRED_WITHOUT_ENTRY",
            coverage="complete",
            evaluated_start=iso(effective),
            evaluated_end=iso(effective),
        )
        result["reasons"] = ["方案实际可用时已不再具有新入场区间，未回填此前机会。"]
        return prepare(result, frozen, as_of)
    if definition["entry"]["mode"] != "market" and definition["entry"]["price"] is None:
        result["reasons"] = ["缺少精确委托价格，区间中点不会用于参考成交。"]
        return prepare(result, frozen, as_of)
    ticks = [
        item
        for item in market.get("ticks", [])
        if parse_utc(item.get("available_at", item["event_time"])) <= as_at
    ]
    if not ticks:
        bars, _ = get_series(market)
        double = any(
            bar.get("closed")
            and parse_utc(bar.get("available_at", bar["close_at"])) <= as_at
            and parse_utc(bar["close_at"]) > effective
            and parse_utc(bar["open_at"]) < end
            and dec(bar["low"]) <= min(dec(definition["stop_loss"]), dec(definition["take_profit"]))
            and dec(bar["high"]) >= max(dec(definition["stop_loss"]), dec(definition["take_profit"]))
            for bar in bars
        )
        result["reasons"] = ["只有 OHLC 或缺少双侧报价，不能确定 bidask_reference/1 的参考成交。"]
        if double:
            result["method_version"] = "ohlc_bounds/1"
            result["inputs"]["method_parameters"]["execution_model_version"] = "ohlc_bounds/1"
            result["review_id"] = review_base(
                "reference_plan", plan["plan_id"], "ohlc_bounds/1", iso(effective), iso(end), binding
            )["review_id"]
            result["outcome"] = "AMBIGUOUS"
            result["reasons"].append("同一根 K 线同时触及止损和目标，未假定是否入场或先后顺序。")
        return prepare(result, frozen, as_of)
    events = []
    if definition["activation"]["kind"] == "immediate":
        events.append({"kind": "immediate", "at": effective})
    needed_frames = {
        rule["timeframe"]
        for rule in (definition["activation"], definition["invalidation"], definition.get("post_activation_invalidation"))
        if rule and rule["kind"] == "bar_close"
    }
    missing_frame = False
    condition_bars = {}
    for timeframe in needed_frames:
        bars, basis = get_series(market, timeframe)
        native_exists = any(row["timeframe"] == timeframe for row in market.get("series", []))
        series_status = next(
            (row.get("status", {}) for row in market.get("series", []) if row["timeframe"] == timeframe),
            market.get("bar_status", {}),
        )
        if series_status.get("state", "available") != "available":
            missing_frame = True
            bars = []
        if not native_exists and not bars and timeframe in {"M5", "M15"}:
            m1, m1_basis = get_series(market)
            if m1_basis == "bid":
                bars, basis = aggregate_m1(m1, int(timeframe[1:])), "bid"
                result["metrics"].append(
                    metric(
                        "condition_series_source",
                        unit="label",
                        observed=f"{timeframe} from aligned complete M1",
                    )
                )
        if basis != "bid" or not bars:
            missing_frame = True
        else:
            condition_bars[timeframe] = bars
            events.extend(
                {
                    "kind": "bar",
                    "at": max(
                        parse_utc(bar["close_at"]), parse_utc(bar.get("available_at", bar["close_at"]))
                    ),
                    "timeframe": timeframe,
                    "bar": bar,
                }
                for bar in bars
                if bar.get("closed")
            )
    for observation in plan.get("manual_observations", []):
        events.append(
            {
                "kind": "observation",
                "role": observation["condition_role"],
                "at": parse_utc(observation["recorded_at"]),
            }
        )
    if plan.get("lifecycle") in {"INVALIDATED", "SUPERSEDED"}:
        events.append(
            {
                "kind": "terminated",
                "at": parse_utc(plan["lifecycle_at"]),
                "reason": "方案在参考入场前已失效或被替代。",
            }
        )
    events.extend(
        {
            "kind": "quote",
            "at": max(
                parse_utc(quote["event_time"]), parse_utc(quote.get("available_at", quote["event_time"]))
            ),
            "quote": quote,
        }
        for quote in ticks
    )
    events.append({"kind": "end_entry", "at": entry_deadline})
    previous = [quote for quote in ticks if parse_utc(quote["event_time"]) < effective]
    previous.sort(key=lambda row: (parse_utc(row["event_time"]), row.get("source_sequence") or 0))
    last_beyond = price_condition(definition, previous[-1]) if previous else False
    state = {
        "trigger_at": None,
        "trigger_kind": None,
        "entered_at": None,
        "entry_price": None,
        "exit_deadline": None,
        "exit_price": None,
        "exited_at": None,
        "outcome": None,
        "reason": None,
        "completed_at": None,
        "last_beyond": last_beyond,
        "breakout_armed": not last_beyond,
    }
    events = sorted(
        (event for event in events if effective <= event["at"] <= as_at), key=lambda event: event["at"]
    )
    for at, grouped in groupby(events, key=lambda event: event["at"]):
        group = list(grouped)
        if "post_activation_invalidation" in definition and any(matches(definition["activation"], e) for e in group):
            group = [{**e, "activation_batch": True} for e in group]
        before_group = deepcopy(state)
        facts = [event for event in group if event["kind"] != "quote"]
        quotes = [event for event in group if event["kind"] == "quote"]
        # The spec defines invalidation priority for the same closed candle.
        facts.sort(
            key=lambda event: (
                0
                if event["kind"] in {"terminated", "end_entry"}
                or invalidates(state, event, definition)
                else 1
            )
        )
        for event in facts:
            state = advance(state, event, definition, effective, entry_deadline, simulation_delay_ms)
        if (
            before_group["entered_at"] is None
            and quotes
            and any(
                event["kind"] == "terminated"
                or invalidates(before_group, event, definition)
                for event in facts
            )
        ):
            for quote_event in quotes:
                alternate = advance(
                    deepcopy(before_group),
                    quote_event,
                    definition,
                    effective,
                    entry_deadline,
                    simulation_delay_ms,
                )
                for fact in facts:
                    alternate = advance(
                        alternate, fact, definition, effective, entry_deadline, simulation_delay_ms
                    )
                if alternate["entered_at"] != state["entered_at"] or alternate["outcome"] != state["outcome"]:
                    state.update(
                        outcome="AMBIGUOUS",
                        reason="闭柱/人工失效与入场报价同一时点，缺少可证明的先后。",
                        completed_at=at,
                    )
                    break
        if state["outcome"]:
            break
        sequences = [event["quote"].get("source_sequence") for event in quotes]
        if (
            len(quotes) <= 1
            or all(sequence is not None for sequence in sequences)
            and len(set(sequences)) == len(sequences)
        ):
            for event in sorted(quotes, key=lambda event: event["quote"].get("source_sequence") or 0):
                state = advance(state, event, definition, effective, entry_deadline, simulation_delay_ms)
        else:
            # Examine all distinct paths in a small ambiguous timestamp. Large mixed
            # groups conservatively remain unknown rather than inventing an order.
            distinct = {
                str((event["quote"].get("bid"), event["quote"].get("ask"))): event for event in quotes
            }
            quotes = list(distinct.values())
            if len(quotes) > 6:
                state.update(
                    outcome="AMBIGUOUS",
                    reason="同一时间的多条报价没有源序列，无法证明成交或触发先后。",
                    completed_at=at,
                )
            else:
                variants = {}
                for ordering in permutations(quotes):
                    candidate = deepcopy(state)
                    for event in ordering:
                        candidate = advance(
                            candidate, event, definition, effective, entry_deadline, simulation_delay_ms
                        )
                    variants[state_key(candidate)] = candidate
                    if len(variants) > 1:
                        break
                if len(variants) > 1:
                    state.update(
                        outcome="AMBIGUOUS",
                        reason="同时间报价先后会改变入场、退出或条件，源序列未知。",
                        completed_at=at,
                    )
                elif variants:
                    state = next(iter(variants.values()))
        if state["outcome"]:
            break
    if not state["outcome"]:
        deadline = state["exit_deadline"] or entry_deadline
        required = deadline + (timedelta(seconds=5) if state["entered_at"] else timedelta())
        if as_at < required:
            raise DomainError("REVIEW_NOT_DUE", "参考方案尚未结束观察或退出报价等待区间。", 409)
        state.update(
            outcome="DATA_INCOMPLETE", completed_at=required, reason="退出时没有取得等待上限内的执行侧报价。"
        )
    needed_until = state["completed_at"] or min(as_at, end)
    # Missing condition candles can hide a trigger or invalidation, even when
    # every supplied candle itself looks valid.
    condition_end = state["entered_at"] or min(needed_until, entry_deadline)
    for timeframe, bars in condition_bars.items():
        minutes = int(timeframe[1:])
        floor = effective.replace(minute=effective.minute // minutes * minutes, second=0, microsecond=0)
        first_close = floor if floor == effective else floor + timedelta(minutes=minutes)
        expected = []
        current = first_close
        while current <= condition_end and current < entry_deadline:
            expected.append(current)
            current += timedelta(minutes=minutes)
        present = [
            parse_utc(bar["close_at"])
            for bar in bars
            if bar.get("closed")
            and first_close <= parse_utc(bar["close_at"]) <= condition_end
            and parse_utc(bar["close_at"]) < entry_deadline
        ]
        if sorted(present) != expected or any(
            parse_utc(bar["close_at"]) - parse_utc(bar["open_at"]) != timedelta(minutes=minutes)
            for bar in bars
        ):
            missing_frame = True
    complete = coverage_ok(market, effective, needed_until) and not missing_frame
    missing_sides = any(
        item.get("bid") is None or item.get("ask") is None
        for item in ticks
        if effective <= parse_utc(item["event_time"]) <= needed_until
    )
    if not complete or missing_sides:
        if state["outcome"] != "AMBIGUOUS":
            state["outcome"] = "DATA_INCOMPLETE"
        result["reasons"].append("双侧报价区间、条件闭柱或报价侧的完整性不足。")
    result.update(
        outcome=state["outcome"],
        coverage="complete" if complete and not missing_sides else "partial",
        evaluated_start=iso(effective),
        evaluated_end=iso(needed_until),
    )
    if state["reason"]:
        result["reasons"].append(state["reason"])
    result["metrics"].extend(
        [
            metric(
                "reference_entry_at",
                unit="utc",
                observed=iso(state["entered_at"]) if state["entered_at"] else None,
                state="available" if state["entered_at"] else "not_applicable",
            ),
            metric(
                "reference_entry_price",
                state["entry_price"],
                unit="price",
                state="available" if state["entry_price"] is not None else "not_applicable",
            ),
            metric(
                "reference_exit_price",
                state["exit_price"],
                unit="price",
                state="available" if state["exit_price"] is not None else "unknown",
            ),
        ]
    )
    if state["exit_price"] is not None and state["outcome"] in {"TP", "SL", "TIME_EXIT"}:
        volume = definition.get("volume_lots") or (settings or {}).get("values", settings or {}).get(
            "default_volume_lots"
        )
        contract = market.get("contract")
        fee, reason = cost_for_entry(settings, market, definition, state["entered_at"])
        if (
            volume is not None
            and contract
            and contract.get("calculation_mode") == "linear_usd"
            and contract.get("profit_currency") == "USD"
        ):
            gross = (
                (Decimal(1) if definition["side"] == "long" else Decimal(-1))
                * (state["exit_price"] - state["entry_price"])
                * dec(contract["contract_size"])
                * dec(volume)
            )
            result["metrics"].append(metric("reference_gross_pnl", gross, unit="USD"))
            if fee is not None:
                result["net_pnl"] = {"amount": str(gross - fee * dec(volume)), "currency": "USD"}
            else:
                result["reasons"].append(reason)
        else:
            result["reasons"].append("参考手数或线性 USD 合约计算依据不足。")
    return prepare(result, frozen, as_of)
