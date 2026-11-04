"""Published schedule diagnosis, separate from prices, scores and historical reviews."""

from datetime import timedelta, timezone

from xau_next.shared.values import parse_utc

SCHEDULE_VERSION = "tmgm-published-xauusd/1"
SCHEDULE_URL = "https://www.tmgm.com/eng/trading/trading-hours"
SCHEDULE_CHECKED_AT = "2026-10-17T17:32:43.876Z"


def session_diagnosis(review, market, source):
    result = {
        "state": "unverified",
        "expected_minutes": 0,
        "observed_minutes": 0,
        "closed_missing_minutes": 0,
        "unexplained_missing_minutes": 0,
        "schedule_version": None,
        "schedule_url": None,
        "schedule_checked_at": None,
        "reasons": [],
    }
    if (
        market.get("source_kind") != "mt5"
        or market.get("symbol") != "XAUUSD"
        or source.get("server") != "TradeMaxGlobal-Live2"
        or source.get("company") != "TradeMax Global Limited"
        or source.get("broker_timezone") not in {"UTC+03:00", "UTC+02:00"}
    ):
        result["reasons"] = ["没有与当前来源匹配的交易时段证据；不从缺柱反推休市。"]
        return result
    result.update(
        schedule_version=SCHEDULE_VERSION, schedule_url=SCHEDULE_URL, schedule_checked_at=SCHEDULE_CHECKED_AT
    )
    zone = timezone(timedelta(hours=int(source["broker_timezone"][4:6])))
    start, end = map(parse_utc, (review["window_start"], review["window_end"]))
    left = start.replace(second=0, microsecond=0) + (
        timedelta(minutes=1) if start.second or start.microsecond else timedelta()
    )
    right = end.replace(second=0, microsecond=0)
    rows = [
        b
        for b in market.get("bars", [])
        if b.get("closed") and left <= parse_utc(b["open_at"]) and parse_utc(b["close_at"]) <= right
    ]
    observed = {parse_utc(b["open_at"]) for b in rows}
    if len(observed) != len(rows) or any(
        parse_utc(b["close_at"]) - parse_utc(b["open_at"]) != timedelta(minutes=1) for b in rows
    ):
        result.update(state="data_gap", reasons=["M1存在重复或周期异常，不能归为正常休市。"])
        return result

    def is_open(at):
        local = at.astimezone(zone)
        minute = local.hour * 60 + local.minute
        return local.weekday() < 5 and 61 <= minute < (1435 if local.weekday() == 4 else 1439)

    n = max(0, int((right - left).total_seconds() / 60))
    if n > 3 * 24 * 60:
        result["reasons"] = ["窗口超过当前休市诊断上限。"]
        return result
    missing = [left + timedelta(minutes=i) for i in range(n) if left + timedelta(minutes=i) not in observed]
    closed = sum(not is_open(t) for t in missing)
    unexplained = len(missing) - closed
    conflict = any(not is_open(t) for t in observed)
    result.update(
        expected_minutes=n,
        observed_minutes=len(observed),
        closed_missing_minutes=closed,
        unexplained_missing_minutes=unexplained,
    )
    result["state"] = (
        "schedule_conflict"
        if conflict
        else "mixed"
        if closed and unexplained
        else "market_closed"
        if closed
        else "data_gap"
        if unexplained
        else "continuous"
    )
    result["reasons"] = ["依据公布的常规交易时段；不替代账户当日临时公告，不补造K线、不改变原窗口或评分。"]
    if closed:
        result["reasons"].append(f"{closed}个缺失分钟符合公布休市时段。")
    if unexplained:
        result["reasons"].append(f"{unexplained}个缺失分钟位于公布交易时段内，仍待核对资料。")
    if conflict:
        result["reasons"].append("保存行情出现在公布休市时段，时段证据冲突，未按休市判定。")
    return result
