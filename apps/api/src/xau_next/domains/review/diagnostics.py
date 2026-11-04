"""Read-only annotations: preserve original scores, identity, availability and revisions."""

from .alignment import aligned_direction
from .common import dec
from .sessions import session_diagnosis


def diagnose_review(review, version, basis, evidence, *, as_of):
    legacy = review["method_version"] in {"prediction_review/1", "prediction_review/2"}
    result = {
        "method_version": "review-diagnostics/1",
        "computed_at": as_of,
        "direction_basis": "legacy_complete_m1" if legacy else "frozen_bid",
        "alignment_metrics": [],
        "session": session_diagnosis(review, {}, {}),
        "reasons": ["原复盘与评分未修改；以下为当前读取已保存资料的独立核对，不是原available_at时已知结果。"],
    }
    if legacy:
        result["reasons"].append("原方向按完整M1裁剪起止价计算，不能直接当作冻结Bid至原端点的命中结论。")
    if not evidence:
        result["reasons"].append("没有该复盘引用的已保存历史附件，未重读终端或猜测。")
        return result
    market = evidence["window"]
    frozen = basis["market"]
    if any(market.get(k) != frozen.get(k) for k in ["account_scope_id", "symbol", "source_kind"]):
        result["reasons"].append("原请求与历史资料来源不匹配，未进行核对。")
        return result
    result["session"] = session_diagnosis(review, market, evidence["source"])
    horizon = next(
        (
            h
            for h in version["answer"]["horizons"]
            if h["horizon_minutes"] == review["inputs"]["horizon_minutes"]
        ),
        None,
    )
    if horizon:
        band = review["inputs"]["method_parameters"]["neutral_band"] or "0.00"
        result["alignment_metrics"] = aligned_direction(
            frozen,
            market,
            review["window_start"],
            review["window_end"],
            horizon["direction"],
            dec(band),
            basis["max_age_ms"],
            as_of=as_of,
        )
    return result
