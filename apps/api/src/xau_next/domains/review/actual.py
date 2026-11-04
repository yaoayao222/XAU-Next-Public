from __future__ import annotations

from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

from .common import data, dec, digest, due, inputs, metric, parameters, prepare, review_base


def compute_actual(trade, *, as_of):
    trade = data(trade)
    summary = trade.get("trade", trade)
    deals = trade.get("deals", [])
    known = {}
    for deal in deals:
        if parse_utc(deal["event_time"]) > parse_utc(as_of):
            continue
        if deal["deal_id"] in known and known[deal["deal_id"]] != deal:
            raise DomainError("TRADE_DEAL_CONFLICT", "相同成交 ID 有冲突材料，不能在复盘中猜测取舍。", 409)
        known[deal["deal_id"]] = deal
    deals = sorted(known.values(), key=lambda item: (parse_utc(item["event_time"]), item["deal_id"]))
    start = summary.get("opened_at") or (deals[0]["event_time"] if deals else as_of)
    end = summary.get("closed_at") or (deals[-1]["event_time"] if deals else start)
    due(start, as_of)
    due(end, as_of)
    binding = inputs(
        plan_id=summary.get("plan_id"),
        trade_id=summary["trade_id"],
        scope="actual_trade",
        method=parameters(price_basis="actual_fill"),
    )
    # Lifecycle windows may become known later, but the logical actual-trade review
    # keeps one identity; corrections append revisions under this identity.
    result = review_base(
        "actual_trade",
        summary["trade_id"],
        "actual_trade/1",
        start,
        end,
        binding,
        [summary["trade_id"]] + [deal["deal_id"] for deal in deals],
    )
    result["review_id"] = (
        "rv-"
        + digest({"kind": "actual_trade", "trade": summary["trade_id"], "method": "actual_trade/1"})[:32]
    )
    if not summary.get("opened_at"):
        result["reasons"].append(
            "真实开仓时间未知；评价窗口起点仅为最早已知成交时间（无成交时为本次缺口记录时点），未回填原交易字段。"
        )
    result.update(
        evaluated_start=deals[0]["event_time"] if deals else None,
        evaluated_end=deals[-1]["event_time"] if deals else None,
    )
    fields = ("realized_profit", "commission", "swap", "other_fee")
    currencies = {
        item[field]["currency"] for item in deals for field in fields if item.get(field) is not None
    }
    fallback_currency = summary.get("known_realized_subtotal", {}).get("currency")
    currency = next(iter(currencies)) if len(currencies) == 1 else fallback_currency
    complete = (
        bool(deals)
        and len(currencies) == 1
        and all(item.get(field) is not None for item in deals for field in fields)
    )
    complete = complete and summary.get("fees_complete", False)
    if len(currencies) > 1:
        result["reasons"].append("成交与费用币种不一致，缺少换算依据，不合成单一净值。")
    if deals and len(currencies) == 1 and currency:
        subtotal = sum(
            (dec(item[field]["amount"]) for item in deals for field in fields if item.get(field) is not None),
            Decimal(0),
        )
        result["metrics"].append(
            metric(
                "known_realized_subtotal",
                subtotal,
                unit=currency,
                reason="仅包含已有券商金额字段，保留其原始符号。",
            )
        )
        for field in fields:
            amounts = [item[field] for item in deals if item.get(field) is not None]
            total = sum((dec(amount["amount"]) for amount in amounts), Decimal(0))
            result["metrics"].append(
                metric(
                    field,
                    total if amounts else None,
                    unit=currency,
                    state="available" if amounts else "unknown",
                    reason=None
                    if len(amounts) == len(deals)
                    else "部分成交缺少该字段，已知小计不等于完整金额。",
                )
            )
        if complete:
            result["metrics"].append(metric("complete_realized_net", subtotal, unit=currency))
            if summary.get("state") == "closed":
                result["net_pnl"] = {"amount": str(subtotal), "currency": currency}
    else:
        result["metrics"].append(
            metric(
                "known_realized_subtotal", state="unknown", unit="money", reason="没有可确认的统一币种金额。"
            )
        )
    state = summary.get("state")
    result["outcome"] = (
        "ACTUAL_CLOSED"
        if state == "closed"
        else "ACTUAL_OPEN"
        if state in {"open", "partially_closed"}
        else "DATA_INCOMPLETE"
    )
    result["coverage"] = (
        "complete"
        if complete and summary.get("opened_at") and state != "unknown"
        else "partial"
        if deals
        else "missing"
    )
    if not complete:
        result["reasons"].append(
            "实测收费或成交生命周期不完整，费用预留不会代替真实收费，完整净结果保留未知。"
        )
    if state in {"open", "partially_closed"}:
        result["reasons"].append("交易尚未全平，已实现小计与完整生命周期净结果分开。")
    if not summary.get("plan_id"):
        result["reasons"].append("当前未可靠关联方案，实际结果不归因于任何参考方案。")
    return prepare(result, {"trade": trade}, as_of)
