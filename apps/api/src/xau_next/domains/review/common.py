from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc


def data(value):
    return value.to_dict() if isinstance(value, Snapshot) else value


def dec(value) -> Decimal:
    if value is None or isinstance(value, (float, bool)):
        raise DomainError("REVIEW_INPUT_INVALID", "复盘数值必须为明确的十进制字符串。")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise DomainError("REVIEW_INPUT_INVALID", "复盘数值格式不正确。") from error
    if not result.is_finite():
        raise DomainError("REVIEW_INPUT_INVALID", "复盘数值不能为 NaN 或 Infinity。")
    return result


def number(value: Decimal | int) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def metric(name, value=None, *, unit="ratio", state="available", reason=None, index=None, observed=None):
    return {
        "name": name,
        "value": number(value) if value is not None else None,
        "unit": unit,
        "state": state,
        "reason": reason,
        "segment_index": index,
        "observed_label": observed,
    }


def parameters(**values):
    result = {
        "neutral_band": None,
        "price_basis": "not_applicable",
        "bar_timeframe": None,
        "turning_left_bars": None,
        "turning_right_bars": None,
        "execution_model_version": None,
        "simulation_delay_ms": None,
        "execution_quote_wait_ms": None,
    }
    result.update(values)
    return result


def inputs(
    *,
    version_id=None,
    plan_id=None,
    trade_id=None,
    horizon=None,
    scope,
    market_ids=(),
    settings_revision=None,
    method,
):
    return {
        "forecast_version_id": version_id,
        "plan_id": plan_id,
        "trade_id": trade_id,
        "horizon_minutes": horizon,
        "evaluation_scope": scope,
        "market_snapshot_ids": sorted(set(market_ids)),
        "cost_settings_revision": settings_revision,
        "method_parameters": method,
    }


def market_ids(market):
    return [market["snapshot_id"]] if market.get("snapshot_id") else list(market.get("snapshot_ids", []))


def review_base(kind, target, method, start, end, binding, refs=()):
    identity = {
        "kind": kind,
        "target": target,
        "method": method,
        "start": start,
        "end": end,
        "scope": binding["evaluation_scope"],
        "horizon": binding["horizon_minutes"],
        "version": binding["forecast_version_id"],
    }
    return {
        "review_id": "rv-" + digest(identity)[:32],
        "revision": 1,
        "target_kind": kind,
        "target_id": target,
        "method_version": method,
        "window_start": start,
        "window_end": end,
        "available_at": start,
        "coverage": "missing",
        "outcome": "DATA_INCOMPLETE",
        "net_pnl": None,
        "metrics": [],
        "reasons": [],
        "source_refs": sorted(set(refs)),
        "evaluated_start": None,
        "evaluated_end": None,
        "inputs": binding,
    }


def prepare(review, frozen_inputs, as_of, evidence_available_at=None):
    # Actual availability is stamped only when the API commits this detached result.
    sources = [frozen_inputs.get("market", {}), frozen_inputs.get("trade", {})]
    if any(source and source.get("source_kind") == "synthetic" for source in sources):
        review["reasons"].append("本次复盘使用合成样例数据，仅证明功能行为，不是实际市场或成交表现。")
    review["available_at"] = as_of
    source = frozen_inputs.get("market", {}).get("source_kind")
    if source is None:
        trade = frozen_inputs.get("trade", {})
        source = trade.get("source_kind") or trade.get("trade", {}).get("source_kind")
    review["source_kind"] = source if source in {"mt5", "synthetic", "imported"} else "unknown"
    fingerprint_review = {
        key: value for key, value in review.items() if key not in {"available_at", "revision"}
    }
    evidence_at = evidence_available_at or as_of
    if parse_utc(evidence_at) > parse_utc(as_of):
        raise DomainError("REVIEW_NOT_DUE", "复盘所需证据尚未实际可用。", 409)
    return Snapshot.of(
        {
            "review": review,
            "input_fingerprint": digest({"input": frozen_inputs, "result": fingerprint_review}),
            "evidence_available_at": evidence_at,
        }
    )


def get_series(market, timeframe="M1"):
    for series in market.get("series", []):
        if series["timeframe"] == timeframe:
            return series.get("bars", []), series.get("price_basis", "unknown")
    if timeframe == "M1":
        return market.get("bars", []), market.get("price_basis", "unknown")
    return [], "unknown"


def due(end, as_of):
    if parse_utc(end) > parse_utc(as_of):
        raise DomainError("REVIEW_NOT_DUE", "评价窗口尚未结束，稍后再进行到期复盘。", 409)
