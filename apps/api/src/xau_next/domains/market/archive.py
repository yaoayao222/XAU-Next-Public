"""Lossless ordinary-capture storage. Every manifest is independently readable.

Only explicitly selected arrays are externalized. No historical row, source
timestamp or business value is normalized by this codec.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from xau_next.platform.files import PreparedAttachment

FORMAT = "market-capture/3"
MEDIA = "application/vnd.xau.market-capture-v3+gzip"
PART_MEDIA = "application/vnd.xau.market-part-v1+gzip"
MAX_BYTES = 64 * 1024 * 1024
MAX_PARTS = 4096
BUCKET_WIDTH = 256
PERIODS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def unpack(content):
    with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("行情内容超过解码上限")
    return json.loads(raw)


def descriptor(part):
    return {"id": part.id, "sha256": part.sha256, "size": part.size}


@dataclass(frozen=True)
class PreparedArchive:
    manifest: PreparedAttachment
    parts: tuple[PreparedAttachment, ...]
    logical_sha256: str
    logical_size: int


def _selected(response):
    result = response.get("result", {})
    for index, series in enumerate(result.get("market", {}).get("series", [])):
        if isinstance(series, dict) and isinstance(series.get("bars"), list):
            yield ["result", "market", "series", index, "bars"], series["bars"], series.get("timeframe"), False
    rates = result.get("raw", {}).get("rates", {})
    if isinstance(rates, dict):
        for timeframe, rows in rates.items():
            if isinstance(rows, list):
                yield ["result", "raw", "rates", timeframe], rows, timeframe, True


def _parent(value, path):
    for key in path[:-1]:
        value = value[key]
    return value, path[-1]


def _bucket(row, period, raw):
    if raw:
        instant = row["time"]
        if type(instant) is not int:
            raise ValueError("原始时间不是整数")
    else:
        instant = datetime.fromisoformat(row["open_at"])
        if instant.tzinfo is None:
            raise ValueError("缺失时区")
        instant = int(instant.timestamp())
    return instant // (period * BUCKET_WIDTH), instant


def _chunks(rows, timeframe, raw):
    if not rows:
        return []
    try:
        keys = [_bucket(row, PERIODS[timeframe], raw) for row in rows]
        if any(b[1] <= a[1] for a, b in pairwise(keys)):
            raise ValueError("原始顺序异常")
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return [rows]  # Unknown time/order: retain the entire array verbatim.
    result, group, previous = [], [], None
    for row, key in zip(rows, keys):
        current = key[0]
        if group and current != previous:
            result.append(group)
            group = []
        group.append(row)
        previous = current
    if group:
        result.append(group)
    return result


def prepare(response, files):
    original = wire(response)
    if len(original) > MAX_BYTES:
        raise ValueError("普通行情采集超过存储上限")
    skeleton = copy.deepcopy(response)
    market = response["result"]["market"]
    raw = response["result"].get("raw", {})
    # Scope is stable across captures, but changes with source/time semantics.
    context = hashlib.sha256(wire({
        "identity": response.get("source_identity"), "symbol": market.get("symbol"),
        "source_kind": market.get("source_kind"), "account_scope_id": market.get("account_scope_id"),
        "time_normalization": raw.get("time_normalization"),
    })).hexdigest()
    parts, arrays = {}, []

    def save(value):
        part = files.prepare_shared(gzip.compress(wire(value), mtime=0), media_type=PART_MEDIA)
        parts[part.id] = part
        return descriptor(part)

    for path, rows, timeframe, is_raw in _selected(response):
        references = []
        basis = None if is_raw else response["result"]["market"]["series"][path[3]].get("price_basis")
        # Group all changing tails with this observation's other facts, rather
        # than create one tiny file per timeframe every five seconds.
        keep_tail = bool(rows) and (is_raw or isinstance(rows[-1], dict) and rows[-1].get("closed") is False)
        prefix, tail = (rows[:-1], rows[-1:]) if keep_tail else (rows, [])
        for chunk in _chunks(prefix, timeframe, is_raw):
            references.append(save({"context": context, "timeframe": timeframe, "raw": is_raw,
                                    "price_basis": basis, "rows": chunk}))
        parent, key = _parent(skeleton, path)
        parent[key] = copy.deepcopy(tail)
        arrays.append({"path": path, "count": len(rows), "tail_count": len(tail), "chunks": references})
    if len(parts) > MAX_PARTS:
        raise ValueError("行情内容块超过存储上限")
    digest = hashlib.sha256(original).hexdigest()
    # The per-observation skeleton and references share one compressed file.
    # Only reusable bar prefixes need independent attachment registrations.
    manifest = {"format": FORMAT, "codec": "json-gzip/3", "skeleton": skeleton,
                "arrays": arrays, "logical_sha256": digest, "logical_size": len(original)}
    attachment = files.prepare_shared(gzip.compress(wire(manifest), mtime=0), media_type=MEDIA)
    return PreparedArchive(attachment, tuple(parts.values()), digest, len(original))


def decode(content, read_many):
    manifest = unpack(content)
    if manifest.get("format") != FORMAT or manifest.get("codec") != "json-gzip/3":
        raise ValueError("不支持的行情存储格式")
    arrays = manifest["arrays"]
    refs = [ref for array in arrays for ref in array["chunks"]]
    if len(refs) > MAX_PARTS or not 0 <= manifest["logical_size"] <= MAX_BYTES:
        raise ValueError("行情清单超过解码上限")
    contents = read_many([ref["id"] for ref in refs])
    decoded, size = {}, 0
    for ref in refs:
        data = contents[ref["id"]]
        if len(data) != ref["size"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ValueError("行情块与清单不一致")
        if ref["id"] not in decoded:
            decoded[ref["id"]] = unpack(data)
        size += len(wire(decoded[ref["id"]]))
        if size > MAX_BYTES * 2:
            raise ValueError("行情块解码总量超过上限")
    response = copy.deepcopy(manifest["skeleton"])
    allowed = {tuple(path) for path, *_ in _selected(response)}
    seen = set()
    for array in arrays:
        path = tuple(array["path"])
        if path not in allowed or path in seen:
            raise ValueError("行情数组路径不合法或重复")
        seen.add(path)
        rows = []
        for ref in array["chunks"]:
            rows.extend(decoded[ref["id"]]["rows"])
        parent, key = _parent(response, path)
        if len(parent[key]) != array["tail_count"]:
            raise ValueError("行情尾部与清单不一致")
        rows.extend(parent[key])
        if len(rows) != array["count"]:
            raise ValueError("行情数组长度或骨架不一致")
        parent[key] = rows
    if seen != allowed:
        raise ValueError("行情数组清单不完整")
    original = wire(response)
    if len(original) != manifest["logical_size"] or hashlib.sha256(original).hexdigest() != manifest["logical_sha256"]:
        raise ValueError("行情响应重建校验失败")
    return response
