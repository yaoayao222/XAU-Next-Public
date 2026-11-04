"""Pure time/provenance projection for the unified improved packet."""
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

INSTRUCTIONS = (
    "# 统一改进包：宏观资料\n"
    "macro_research保留金十原始评级、指标及事件，评级仅表示资料关注优先级，不决定黄金方向。"
    "全量是所选月份全部国家和评级的采集范围；模型只收到本窗口相关部分，excluded_event_count明确未发送数量。"
    "公开美元指数与2/10年收益率均是带观测时间的快照，real_time_verified=false时不得称已认证实时序列。"
    "stale只可作标明日期的背景，不能把日涨跌或单点数值写成最近30分钟走势。"
    "来源未取得、实际值缺失、时点未知各自说明，不能补造金十星级，不能因日历为空说没有事件。\n")


def invalid(message="宏观资料来源、时点或字段无效。"):
    raise DomainError("MACRO_RESEARCH_INVALID", message, 409)


def project_macro(value, *, cutoff, window_end):
    if not isinstance(value, dict) or value.get("version") not in {"macro-research/1","macro-research/2"} or value.get("data_kind") not in {"REAL","SYNTHETIC"}:
        invalid()
    multi=value["version"]=="macro-research/2"
    at = parse_utc(cutoff)
    if parse_utc(value["captured_at"]) > at:
        invalid("宏观资料取得晚于行情截止，请等待更新的行情后准备改进包。")
    sources = []
    known = set()
    for raw in value.get("sources", []):
        s = {k:deepcopy(raw.get(k)) for k in ("source_id","url","captured_at","status","content_sha256","message")}
        if s["source_id"] in known or parse_utc(s["captured_at"]) > at:
            invalid()
        known.add(s["source_id"])
        url = s["url"]
        allowed = {
            "DXY":"https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d",
            "US2Y":"https://www.cnbc.com/quotes/US2Y", "US10Y":"https://www.cnbc.com/quotes/US10Y",
            "calendar":"https://nfs.faireconomy.media/ff_calendar_thisweek.json", "umich":"https://www.sca.isr.umich.edu/"}
        if s["source_id"] in {"jin10-data","jin10-events"}:
            pattern = r"https://cdn-rili\.jin10\.com/web_data/20\d{2}/month/(?:[1-9]|1[0-2])/(?:economics|event)\.json"
            if not isinstance(url,str) or not re.fullmatch(pattern,url): invalid()
        elif allowed.get(s["source_id"]) != url:
            from xau_next.shared.research_sources import valid_source_url
            if not multi or not valid_source_url(s["source_id"],url):invalid()
        if s["status"] not in {"available","unavailable"} or (s["status"]=="available" and
                (not isinstance(s["content_sha256"],str) or not re.fullmatch(r"[0-9a-f]{64}",s["content_sha256"]))):
            invalid()
        sources.append(s)
    source_map = {s["source_id"]:s for s in sources}
    if len(source_map) > (24 if multi else 7):
        invalid()
    events = []
    seen = set()
    all_events = value.get("events", [])
    if not isinstance(all_events,list) or len(all_events) > 20000:
        invalid()
    for raw in all_events:
        row = {k:deepcopy(raw.get(k)) for k in (
            "event_id","title","country","currency","scheduled_at","reference_period","kind","importance","importance_scale",
            "previous","forecast","actual","revised","unit","actual_available_at","source_id","status")}
        source = source_map.get(row["source_id"])
        if not source or source["status"]!="available" or row["event_id"] in seen:
            invalid()
        seen.add(row["event_id"])
        if row["importance"] is not None and (type(row["importance"]) is not int or not 1<=row["importance"]<=5 or not row["source_id"].startswith("jin10")):
            invalid("非金十来源不能冒充金十星级。")
        if row["scheduled_at"] is None:
            continue
        scheduled = parse_utc(row["scheduled_at"])
        if row["actual"] is not None:
            if (row["kind"] != "data" or row["actual_available_at"] != source["captured_at"]
                    or not scheduled <= parse_utc(row["actual_available_at"]) <= at):
                invalid("未公布或晚取得的实际值不能进入本次资料。")
        if at-timedelta(hours=6) <= scheduled <= parse_utc(window_end):
            events.append(row)
    if len(events)>512:
        invalid("当前窗口宏观事件超过512项，请缩短窗口；不静默截断。")
    quotes = []
    for raw in value.get("quotes", []):
        q = {k:deepcopy(raw.get(k)) for k in ("symbol","value","unit","observed_at","captured_at","publisher","url",
                                             "state","provider_claims_realtime","real_time_verified","limitation")}
        source = source_map.get(q["symbol"])
        if not source or source["url"] != q["url"] or parse_utc(q["captured_at"])>at or q["real_time_verified"] is not False:
            invalid()
        if q["value"] is not None:
            if multi and (source["status"]!="available" or q["captured_at"]!=source["captured_at"] or
                          q["unit"]!=("指数点" if q["symbol"]=="DXY" else "%")):
                invalid()
            if not isinstance(q["value"],str) or not re.fullmatch(r"-?\d+(?:\.\d+)?",q["value"]) or not Decimal(q["value"]).is_finite():
                invalid()
            if q["observed_at"]:
                observed = parse_utc(q["observed_at"])
                if observed > parse_utc(q["captured_at"]): invalid()
                q["state"] = "fresh_snapshot" if (at-observed).total_seconds()<=300 else "stale"
            else: q["state"]="time_unknown"
        else: q["state"]="unavailable"
        quotes.append(q)
    if len({q["symbol"] for q in quotes})!=len(quotes): invalid()
    # Outbound validation can project an already bounded packet again. Keep the
    # original collection denominator instead of pretending excluded rows vanished.
    total = value.get("total_event_count", len(all_events))
    if type(total) is not int or not len(all_events) <= total <= 20000:
        invalid()
    result={"version":value["version"],"data_kind":value["data_kind"],"captured_at":value["captured_at"],
        "month":value["month"],"scope":value["scope"],"state":value["state"],"sources":sources,
        "events":events,"quotes":quotes,"total_event_count":total,"excluded_event_count":total-len(events),
        "limitations":["金十评级不是方向信号，未认证实时行情不用于宣称盘中同步驱动。",
            "只提供当前窗口相关事件；全部采集记录及原件仍另存，空列表不证明没有事件。"]}
    if multi:
        from .macro_evidence import project_evidence
        result=project_evidence(result,value,cutoff=cutoff,window_end=window_end)
    return result


def economic_for_bundle(value):
    """Prepare raw economic context. Actual cutoff/coverage checks run at build time."""
    if any(s["source_id"].startswith("jin10") and s["status"]=="available" for s in value["sources"]):
        start = datetime.fromisoformat(value["month"]+"-01T00:00:00+08:00")
        finish = (start.replace(day=28)+timedelta(days=4)).replace(day=1)
        z=lambda t:t.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
        sources=[{"source_id":s["source_id"],"publisher":"金十财经日历","url":s["url"],"captured_at":s["captured_at"],
                  "content_sha256":s["content_sha256"],"coverage_start":z(start),"coverage_end":z(finish)}
                 for s in value["sources"] if s["source_id"].startswith("jin10") and s["status"]=="available"]
        events=[]
        for e in value["events"]:
            if not e["source_id"].startswith("jin10") or e["scheduled_at"] is None:continue
            def unit(v):return v+"%" if v is not None and e["unit"]=="%" and not v.endswith("%") else v
            events.append({"event_id":e["event_id"],"title":e["country"]+" "+(e["reference_period"] or "")+" "+e["title"],
                "currency":e["currency"],"scheduled_at":e["scheduled_at"],"importance":str(e["importance"] or "未评级"),
                "importance_scale":e["importance_scale"],"source_id":e["source_id"],"forecast":unit(e["forecast"]),
                "previous":unit(e["previous"]),"actual":unit(e["actual"]),"actual_available_at":e["actual_available_at"]})
        # Raw monthly records stay in the bundle; the model's bounded time window
        # is selected later using the market cutoff, never by rating or country.
        return {"version":"economic-context/1","data_kind":value["data_kind"],"sources":sources,"events":events}
    if value.get("calendar_document"):
        result=deepcopy(value["calendar_document"])
    else:
        result={"version":"economic-context/1","data_kind":value["data_kind"],"sources":[],"events":[]}
    if value.get("umich_document"):
        result["actual_supplement"]=deepcopy(value["umich_document"])
    return result
