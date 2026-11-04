"""One explicit official Search API request, no model generation, retries or arbitrary URL."""
import hashlib
import json
from datetime import datetime, timezone
import httpx

from xau_next.shared.errors import DomainError
from xau_next.shared.values import utc_now, parse_utc

URL="https://api.deepseek.com/v1/tools/search"
MAX_BYTES=1_000_000


def fetch_search(key, query, component, *, transport=None, clock=utc_now):
    if component not in {"news","usd","rates"} or not isinstance(query,str) or not 1 <= len(query) <= 128:
        raise DomainError("SHARED_SEARCH_QUERY_INVALID", "搜索问题无效，未发送。", 409)
    chunks=[]
    with httpx.Client(transport=transport,timeout=30,follow_redirects=False) as client:
        with client.stream("POST", URL,headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
                           json={"query":query,"count":3,"need_content":True}) as response:
            response.raise_for_status()
            size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>MAX_BYTES:
                    raise DomainError("SHARED_SEARCH_TOO_LARGE", "搜索响应超过上限，停止且不重试。", 409)
                chunks.append(chunk)
    raw=b"".join(chunks)
    if key.encode() in raw:
        raise DomainError("SHARED_SEARCH_SECRET_IN_RESPONSE", "响应含凭据，未保存。", 409)
    captured=clock(); payload=json.loads(raw);results=payload.get("results")
    if not isinstance(results,list) or not 1 <= len(results) <= 3:
        raise DomainError("SHARED_SEARCH_EMPTY", "搜索未取得来源，不补取、不启动模型组。", 409)
    rows=[]
    for i,r in enumerate(results):
        published=r.get("date")
        if published:
            try:
                instant=datetime.fromisoformat(published)
                if instant.tzinfo is None:
                    raise ValueError("unqualified publication timezone")
                published=instant.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
            except (ValueError,TypeError):
                published=None
        if published and parse_utc(published)>parse_utc(captured):
            raise DomainError("SHARED_SEARCH_FUTURE_SOURCE", "来源发布时间晚于获取时间，停止。", 409)
        rows.append({"source_id":component+"-"+str(i+1),"url":r.get("url"),"title":str(r.get("title") or "")[:300],
                     "published_at":published,"snippet":str(r.get("summary") or r.get("snippet") or "")[:1200],
                     "content":str(r.get("content") or "")[:2000]})
    return {"component":component,"query":query,"captured_at":captured,
            "content_sha256":hashlib.sha256(raw).hexdigest(),"results":rows},raw
