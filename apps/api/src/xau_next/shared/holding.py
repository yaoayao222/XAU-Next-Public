"""Stable holding identities and detached facts; no broker or model operations."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from .values import parse_utc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def holding_key(order, opened_at):
    return digest([order["order"], order.get("plan_id"), opened_at])


def holding_fingerprint(order, opened_at):
    # Poll timestamps and quotes are deliberately excluded. Real order changes are not.
    values = {k: order.get(k) for k in (
        "order", "plan_id", "symbol", "side", "volume_lots", "entry_price", "stop_loss", "take_profit"
    )}
    for k in ("volume_lots", "entry_price", "stop_loss", "take_profit"):
        v = values[k]
        if isinstance(v, str) and "." in v:
            values[k] = v.rstrip("0").rstrip(".")
        if values[k] == "-0":
            values[k] = "0"
    return digest([values, opened_at])


def holding_deadline(plan, opened_at):
    if not plan:
        return None
    p = plan["definition"]
    values = [parse_utc(p["exit_deadline"])]
    if opened_at and p.get("max_holding_minutes"):
        values.append(parse_utc(opened_at) + timedelta(minutes=p["max_holding_minutes"]))
    return min(values).isoformat().replace("+00:00", "Z")


def rule_key(rule):
    return digest(rule)


def holding_review_context(order, plan, opened_at, previous=None):
    p = plan["definition"] if plan else {}
    return {
        "order": order["order"], "plan_id": order.get("plan_id"),
        "opened_at": opened_at, "deadline": holding_deadline(plan, opened_at),
        "fingerprint": holding_fingerprint(order, opened_at), "holding_key": holding_key(order, opened_at),
        "original_plan": ({k: p.get(k) for k in (
            "plan_key", "side", "stop_loss", "take_profit", "rationale", "invalidation",
            "post_activation_invalidation", "holding_exit", "exit_deadline", "max_holding_minutes"
        )} if plan else None),
        "original_version_id": plan.get("version_id") if plan else None,
        "previous_review": previous,
    }


def same_price(a, b):
    return (a is None and b is None) or (a is not None and b is not None and Decimal(a) == Decimal(b))
