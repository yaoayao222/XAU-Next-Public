"""Pure aggregation of broker-provided deal facts; no estimated fees or fills."""
from decimal import Decimal, localcontext

from xau_next.shared.values import parse_utc

FEE_FIELDS = ("realized_profit", "commission", "swap", "other_fee")
DEAL_FIELDS = ("deal_id", "event_time", "volume_lots", "price", *FEE_FIELDS)
TRADE_FIELDS = ("trade_id", "account_scope_id", "symbol", "side", "state", "opened_at", "closed_at", "plan_id",
                "known_realized_subtotal", "complete_realized_net", "fees_complete")


def aggregate(envelope, deals):
    with localcontext() as context:
        context.prec = 128
        return _aggregate(envelope, deals)


def _aggregate(envelope, deals):
    currency = envelope["currency"]
    subtotal = Decimal(0)
    entered = exited = Decimal(0)
    entry_weight = exit_weight = Decimal(0)
    missing, accounting_missing, ins, outs = [], [], [], []
    for deal in deals:
        for field in FEE_FIELDS:
            money = deal[field]
            if money is None:
                accounting_missing.append(f"{deal['deal_id']}.{field}")
            elif money["currency"] != currency:
                accounting_missing.append(f"{deal['deal_id']}.{field}.currency")
            else:
                subtotal += Decimal(money["amount"])
        volume, price = Decimal(deal["volume_lots"]), Decimal(deal["price"])
        if deal["entry_kind"] == "in" and volume > 0:
            entered += volume
            entry_weight += volume * price
            ins.append(deal)
        elif deal["entry_kind"] in ("out", "out_by") and volume > 0:
            exited += volume
            exit_weight += volume * price
            outs.append(deal)
        else:
            missing.append(f"{deal['deal_id']}.entry_kind_or_volume")
    if not ins:
        missing.append("entry_history_missing")
    if envelope.get("opening_complete") is False:
        missing.append("opening_history_incomplete")
    if envelope["side"] is None:
        missing.append("original_side_unknown")
    if exited > entered:
        missing.append("exit_volume_exceeds_known_entry")
    if ins and outs and min(parse_utc(d["event_time"]) for d in outs) < min(parse_utc(d["event_time"]) for d in ins):
        missing.append("exit_precedes_known_entry")
    lifecycle_complete = not missing
    state = "unknown"
    if lifecycle_complete:
        state = "closed" if entered == exited else ("partially_closed" if exited > 0 else "open")
    opened_at = (min(ins, key=lambda d: parse_utc(d["event_time"]))["event_time"]
                 if ins and envelope.get("opening_complete") is not False else envelope["opened_at"])
    closed_at = max(outs, key=lambda d: parse_utc(d["event_time"]))["event_time"] if state == "closed" else None
    money = {"amount": str(subtotal), "currency": currency}
    complete_net = money if not accounting_missing and lifecycle_complete else None
    trade = {"trade_id": envelope["trade_id"], "account_scope_id": envelope["account_scope_id"],
             "symbol": envelope["symbol"], "side": envelope["side"], "state": state,
             "opened_at": opened_at, "closed_at": closed_at, "plan_id": None,
             "known_realized_subtotal": money, "complete_realized_net": complete_net,
             "fees_complete": not accounting_missing}
    metrics = {"trade_id": envelope["trade_id"], "broker_position_id": envelope["broker_position_id"],
               "entry_volume_lots": str(entered) if ins else None, "closed_volume_lots": str(exited),
               "remaining_volume_lots": str(entered - exited) if lifecycle_complete else None,
               "weighted_entry_price": str(entry_weight / entered) if entered else None,
               "weighted_exit_price": str(exit_weight / exited) if exited else None,
               "lifecycle_complete": lifecycle_complete, "missing": missing + accounting_missing,
               "deal_count": len(deals), "accounting": trade}
    return trade, metrics
