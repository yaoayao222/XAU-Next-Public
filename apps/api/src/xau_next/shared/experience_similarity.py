"""Frozen closed-bar retrieval descriptors; never a forecast or an execution rule."""
import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext

from .near_term import build_near_term


def market_signature(market):
    identity = [market.get(key) for key in ("account_scope_id", "symbol", "source_kind")]
    scope = (hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
             if all(isinstance(value, str) and value for value in identity) else None)
    result = {"version": "closed-m1-retrieval/1", "scope_key": scope, "state": "unknown",
              "cutoff": market.get("market_cutoff"), "window_minutes": 10,
              "price_basis": "bid", "shape": None, "direction": None,
              "range_bps": None, "efficiency": None}
    try:
        near = build_near_term(market)
        window = next(w for w in near["m1_windows"] if w["window_minutes"] == 10)
        if window["state"] != "available" or window["price_basis"] != "bid" or not window["continuity_complete"]:
            return result
        with localcontext() as context:
            context.prec = 64
            change, span, close = (Decimal(window[k]) for k in ("change", "range", "latest_close"))
            if not all(v.is_finite() for v in (change, span, close)) or close <= 0 or span < abs(change):
                return result
            efficiency = abs(change) / span if span else Decimal(0)
            result.update(state="available", direction="up" if change > 0 else "down" if change < 0 else "flat",
                shape="directional" if efficiency >= Decimal("0.6") else "two_way" if efficiency <= Decimal("0.3") else "mixed",
                range_bps=format((span / close * 10000).quantize(Decimal("0.000001")), "f"),
                efficiency=format(efficiency.quantize(Decimal("0.000001")), "f"))
    except (ValueError, TypeError, KeyError, InvalidOperation, StopIteration):
        return result
    return result
