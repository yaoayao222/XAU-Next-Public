"""One explicit public HTTP read; no API key, retries, scheduler, or model calls."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from xau_next.shared.errors import DomainError
from xau_next.shared.values import utc_now

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MAX_BYTES = 250000


def fetch_calendar(*, transport=None, clock=utc_now):
    chunks = []
    with httpx.Client(transport=transport, timeout=20, follow_redirects=False) as client:
        with client.stream("GET", URL, headers={"Accept": "application/json"}) as response:
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise DomainError("CALENDAR_TOO_LARGE", "日历资料超过大小上限，未接入。", 409)
                chunks.append(chunk)
    captured = clock()
    raw = b"".join(chunks)
    return calendar_document(raw, captured_at=captured), raw


def calendar_document(raw, *, captured_at, data_kind="REAL"):
    """This feed has forecasts/previous values, not an actual-release-value feed."""
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows or len(rows) > 512:
        raise DomainError("CALENDAR_FORMAT_INVALID", "日历格式无效。", 409)
    dates = [datetime.fromisoformat(r["date"]) for r in rows]
    if any(d.tzinfo is None for d in dates):
        raise DomainError("CALENDAR_TIME_INVALID", "日历缺少时区。", 409)
    first = min(dates)
    start = (first - timedelta(days=(first.weekday()+1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    finish = start+timedelta(days=7)
    z = lambda d: d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    events = []
    for row, at in zip(rows, dates):
        # Preserve provider's scale: High is not relabelled as a five-star rating.
        if row["country"] not in {"USD", "All"}:
            continue
        identity = json.dumps([row["title"], row["country"], z(at)], ensure_ascii=False).encode()
        events.append({"event_id": "ff-"+hashlib.sha256(identity).hexdigest()[:24], "title": row["title"],
            "currency": row["country"], "scheduled_at": z(at), "importance": row["impact"],
            "importance_scale": "Forex Factory: High/Medium/Low/Holiday", "source_id": "forexfactory-weekly",
            "forecast": row.get("forecast") or None, "previous": row.get("previous") or None,
            "actual": None, "actual_available_at": None})
    return {"version": "economic-context/1", "data_kind": data_kind,
        "sources": [{"source_id": "forexfactory-weekly", "publisher": "Forex Factory / Fair Economy",
            "url": URL, "captured_at": captured_at, "content_sha256": hashlib.sha256(raw).hexdigest(),
            "coverage_start": z(start), "coverage_end": z(finish)}], "events": events}
