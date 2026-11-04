"""Explicit bounded public research reads. No keys, models, bridge or retry loop."""
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import httpx

from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc, utc_now
from .public import URL as FF_URL, calendar_document
from .umich import URL as UMICH_URL, umich_document

MAX_BYTES = 4_000_000
JIN10_BASE = "https://cdn-rili.jin10.com/web_data/"
QUOTE_URLS = {"DXY": "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB?interval=1m&range=1d",
              "US2Y": "https://www.cnbc.com/quotes/US2Y", "US10Y": "https://www.cnbc.com/quotes/US10Y"}
NULLS = {"", "--", "待公布", "待发布", "未公布", "N/A", "null"}


def text(value):
    if value is None:
        return None
    result = str(value).strip()
    return None if result in NULLS else result[:1000]


def instant(value):
    if not isinstance(value, str) or len(value) <= 10:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            return None
        return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def jin10_events(raw, *, captured_at, source_id, kind):
    """All countries and ratings; unknown layout/time fails or stays explicit."""
    payload = json.loads(raw)
    rows = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 10000:
        raise DomainError("JIN10_FORMAT_INVALID", "金十资料结构未确认，未将空白当作没有事件。", 409)
    result = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            raise DomainError("JIN10_FORMAT_INVALID", "金十事件格式变化，未猜测指标。", 409)
        title = text(item.get("indicator_name") or item.get("name") or item.get("event_content"))
        if not title:
            raise DomainError("JIN10_FORMAT_INVALID", "金十事件名称缺失，未忽略该记录。", 409)
        at = instant(item.get("pub_time") or item.get("time"))
        star = item.get("star")
        star = int(star) if isinstance(star, (int, str)) and not isinstance(star, bool) and str(star) in {"1","2","3","4","5"} else None
        actual = text(item.get("actual")) if kind == "data" else None
        # Future or undated values cannot be certified as already released.
        if at is None or parse_utc(at) > parse_utc(captured_at):
            actual = None
        identity = str(item.get("data_id") or item.get("id") or "") + "|" + title + "|" + str(at)
        eid = "jin10-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if eid in seen:
            raise DomainError("JIN10_DUPLICATE_EVENT", "金十事件身份重复，未静默合并。", 409)
        seen.add(eid)
        country = text(item.get("country")) or "未注明"
        currency = text(item.get("currency")) or ({"美国":"USD", "中国":"CNY", "欧元区":"EUR", "英国":"GBP", "日本":"JPY"}.get(country) or "UNKNOWN")
        result.append({"event_id": eid, "title": title, "country": country, "currency": currency,
            "scheduled_at": at, "reference_period": text(item.get("time_period")), "kind": kind,
            "importance": star, "importance_scale": "金十原始1—5星；未获取不换算",
            "previous": text(item.get("previous")), "forecast": text(item.get("consensus")),
            "actual": actual, "revised": text(item.get("revised")), "unit": text(item.get("unit")),
            "actual_available_at": captured_at if actual is not None else None, "source_id": source_id,
            "status": "time_unknown" if at is None else "event" if kind == "event" else "released" if actual is not None else
                      "scheduled" if parse_utc(at) > parse_utc(captured_at) else "value_missing"})
    return result


def parse_quote(raw, *, symbol, captured_at, url):
    if symbol == "DXY":
        data = json.loads(raw)["chart"]["result"][0]["meta"]
        if data.get("symbol") != "DX-Y.NYB" or data.get("instrumentType") != "INDEX":
            raise ValueError("not the expected dollar index")
        value = str(data["regularMarketPrice"])
        observed = datetime.fromtimestamp(data["regularMarketTime"], timezone.utc).isoformat().replace("+00:00", "Z")
        publisher, unit, claimed = "Yahoo Finance / ICE美元指数页面", "指数点", False
    else:
        page = raw.decode("utf-8-sig")
        arrays = re.finditer(r'"quote"\s*:\s*\{\s*"data"\s*:\s*', page)
        matches = []
        for match in arrays:
            try:
                rows, _ = json.JSONDecoder().raw_decode(page[match.end():])
                matches.extend(r for r in rows if r.get("symbol") == symbol and "last_time" in r)
            except (ValueError, TypeError, AttributeError):
                continue
        if not matches:
            raise ValueError("timestamped yield quote missing")
        canonical = {(m.get("last"), m.get("last_time")) for m in matches}
        if len(canonical) != 1 or matches[0].get("type") != "BOND":
            raise ValueError("conflicting yield quotes")
        data = matches[0]
        value = str(data["last"])
        if not re.fullmatch(r"-?\d+(?:\.\d+)?%", value):
            raise ValueError("not a percent yield")
        value = value[:-1]
        observed = instant(data["last_time"])
        publisher, unit, claimed = "CNBC / Tradeweb收益率页面", "%", data.get("realTime") == "true"
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", value) or not Decimal(value).is_finite():
        raise ValueError("invalid quote value")
    if observed and parse_utc(observed) > parse_utc(captured_at):
        raise ValueError("future observation")
    age = (parse_utc(captured_at)-parse_utc(observed)).total_seconds() if observed else None
    return {"symbol": symbol, "value": value, "unit": unit, "observed_at": observed,
        "captured_at": captured_at, "publisher": publisher, "url": url,
        "state": "time_unknown" if age is None else "fresh_snapshot" if age <= 300 else "stale",
        "provider_claims_realtime": claimed, "real_time_verified": False,
        "limitation": "带时间的公开报价快照；未认证连续实时序列或延时。单点/日涨跌不能冒充最近30分钟变化。"}


def fetch_public_research(*, month, transport=None, clock=utc_now):
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", month):
        raise DomainError("RESEARCH_MONTH_INVALID", "请选择有效月份。", 409)
    year, number = month.split("-")
    urls = {"jin10-data": JIN10_BASE+year+"/month/"+str(int(number))+"/economics.json",
            "jin10-events": JIN10_BASE+year+"/month/"+str(int(number))+"/event.json",
            **QUOTE_URLS, "calendar": FF_URL, "umich": UMICH_URL}

    def fetch(item):
        name, url = item
        raw = None
        try:
            with httpx.Client(transport=transport, timeout=20, follow_redirects=False) as client:
                with client.stream("GET", url, headers={"User-Agent":"XAU-Next-Public-Research/1.0", "Accept":"*/*"}) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("response too large")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
            captured = clock()
            return name, {"source_id":name,"url":url,"captured_at":captured,"status":"available",
                          "content_sha256":hashlib.sha256(raw).hexdigest(),"message":"已取得公开资料"}, raw
        except Exception as error:
            return name, {"source_id":name,"url":url,"captured_at":clock(),"status":"unavailable",
                          "content_sha256":None,"message":"公开来源暂未取得；未自动重试。", "diagnostic":type(error).__name__}, raw
    with ThreadPoolExecutor(max_workers=4) as pool:
        captured = list(pool.map(fetch, urls.items()))
    sources, raws, events, quotes = [], {}, [], []
    calendar_doc = umich = None
    for name, source, raw in captured:
        raws[name] = raw
        if source["status"] == "available":
            try:
                if name.startswith("jin10"):
                    events.extend(jin10_events(raw, captured_at=source["captured_at"], source_id=name,
                                               kind="data" if name == "jin10-data" else "event"))
                elif name in QUOTE_URLS:
                    quotes.append(parse_quote(raw, symbol=name, captured_at=source["captured_at"], url=source["url"]))
                elif name == "calendar":
                    calendar_doc = calendar_document(raw, captured_at=source["captured_at"])
                elif name == "umich":
                    header = re.search(rb"(Final|Preliminary) Results for ([A-Za-z]+) (20\d{2})", raw)
                    if not header:
                        raise ValueError("official release header missing")
                    if header:
                        from .umich import MONTHS
                        period = header.group(3).decode()+"-"+f"{MONTHS[header.group(2).decode()]:02}"
                        umich = umich_document(raw, captured_at=source["captured_at"], reference_period=period,
                                               release_kind=header.group(1).decode().lower())
            except Exception as error:
                source.update(status="unavailable",message="来源格式或时间无法核验；缺口保留。",diagnostic=type(error).__name__)
        sources.append(source)
    for symbol in QUOTE_URLS:
        if not any(q["symbol"] == symbol for q in quotes):
            quotes.append({"symbol":symbol,"value":None,"unit":"指数点" if symbol=="DXY" else "%",
                "observed_at":None,"captured_at":clock(),"publisher":"公开报价来源", "url":QUOTE_URLS[symbol],
                "state":"unavailable","provider_claims_realtime":False,"real_time_verified":False,
                "limitation":"尚未取得可核验报价，不以其他品种或旧文章代替。"})
    # An alternate calendar remains explicitly labelled; it is never called Jin10.
    if calendar_doc:
        for e in calendar_doc["events"]:
            events.append({"event_id":e["event_id"],"title":e["title"],"country":"美国" if e["currency"]=="USD" else "全球",
                "currency":e["currency"],"scheduled_at":e["scheduled_at"],"reference_period":None,"kind":"data",
                "importance":None,"importance_scale":e["importance_scale"]+": "+e["importance"],
                "previous":e["previous"],"forecast":e["forecast"],"actual":None,"revised":None,"unit":None,
                "actual_available_at":None,"source_id":"calendar","status":"scheduled" if parse_utc(e["scheduled_at"])>parse_utc(clock()) else "value_missing"})
    at = clock()
    return {"version":"macro-research/1","data_kind":"REAL","captured_at":at,"month":month,
        "scope":"金十全部国家/全部评级/数据及事件；仅为所选月份，不声称覆盖全部历史指标",
        "state":"available" if all(s["status"]=="available" for s in sources) else "partial",
        "sources":sources,"events":events,"quotes":quotes,"calendar_document":calendar_doc,"umich_document":umich,
        "limitations":["金十评级保留原值，缺失不以Forex Factory高/中/低换算。",
            "公开报价保留观测和取得时间，休市旧值不是实时行情；连续实时性尚未认证。",
            "财经事件和讲话没有数值实际值，不强行补数；日历不包含所有突发新闻。"]}, raws
