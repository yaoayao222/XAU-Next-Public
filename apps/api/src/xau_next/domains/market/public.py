from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import zlib
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

from xau_next.platform.db import Database, ReadScope, UnitOfWork
from xau_next.platform.files import AttachmentStore, PreparedAttachment
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import new_id, parse_utc, utc_now

from . import archive
from .bridge import BridgeClient, BridgeError


def _dict(value):
    return value.to_dict() if isinstance(value, Snapshot) else value


@dataclass(frozen=True)
class PreparedCapture:
    market: Snapshot
    portfolio: Snapshot
    deals: tuple[Snapshot, ...]
    raw_attachment: PreparedAttachment
    identity: Snapshot
    history: Snapshot | None = None
    market_attachment: PreparedAttachment | None = None
    review_evidence: dict | None = None
    archive: archive.PreparedArchive | None = None


class MarketService:
    def __init__(
        self,
        db: Database,
        files: AttachmentStore | None = None,
        bridge_command: list[str] | None = None,
        mode="mt5",
        clock=utc_now,
        storage_format=None,
    ):
        if mode not in {"synthetic", "mt5"}:
            raise ValueError("行情来源模式无效")
        self._db, self._files, self._mode, self._clock = db, files or AttachmentStore(db), mode, clock
        self._docs = db.documents("market")
        self._storage_format = storage_format or os.environ.get("XAU_NEXT_MARKET_STORAGE", "chunked")
        if self._storage_format not in {"chunked", "legacy"}:
            raise ValueError("行情存储模式无效")
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        if bridge_command is None:
            root = Path(__file__).resolve().parents[6]
            source = Path(os.environ.get("XAU_NEXT_BRIDGE_SOURCE", str(root / "apps" / "mt5_bridge" / "src")))
            python = Path(
                os.environ.get(
                    "XAU_NEXT_BRIDGE_PYTHON",
                    str(root / "apps" / "mt5_bridge" / ".venv" / "Scripts" / "python.exe"),
                )
            )
            executable = (
                str(python) if os.environ.get("XAU_NEXT_BRIDGE_PYTHON") or python.exists() else sys.executable
            )
            bridge_command = [executable, "-m", "xau_mt5_bridge", "--mode", mode]
            if source.is_dir():
                environment["PYTHONPATH"] = str(source)
        self._bridge = BridgeClient(
            bridge_command, environment=environment, error_log=db.data_root / "logs" / "bridge.log"
        )

    @property
    def source_kind(self):
        return self._mode

    def capture(self, command, settings) -> PreparedCapture:
        command, settings = _dict(command), _dict(settings)
        values = settings.get("values", settings)
        params = self._params(values)
        if bool(command.get("history_from")) != bool(command.get("history_to")):
            raise DomainError("HISTORY_RANGE_INVALID", "历史起止时间必须同时提供")
        if command.get("include_history") and not command.get("history_from"):
            raise DomainError("HISTORY_RANGE_INVALID", "读取历史必须明确起止时间")
        params.update(command)
        snapshot_params = {**params, "include_history": False, "history_from": None, "history_to": None}
        response = self._bridge.request("snapshot", snapshot_params)
        identity, result = response["source_identity"], response["result"]
        self._check_identity(identity, values)
        market, portfolio = result["market"], result["portfolio"]
        if market["source_kind"] != self._mode or market["account_scope_id"] != identity["account_scope_id"]:
            raise BridgeError("SOURCE_IDENTITY_MISMATCH", "行情来源与桥接身份不一致", retryable=False)
        if portfolio["account_scope_id"] != identity["account_scope_id"]:
            raise BridgeError("SOURCE_IDENTITY_MISMATCH", "账户与行情来源不一致", retryable=False)
        if command.get("include_history"):
            history = self._history_chunks(
                {**params, "from": command["history_from"], "to": command["history_to"]},
                values,
                expected_scope=identity["account_scope_id"],
            )
            result["history"] = history
            # The ordinary account-deal window is independent from this (often short)
            # market replay window. Both same-source batches are merged by portfolio.
            result["trades"] = result.get("trades", []) + history["trades"]
            result["raw"]["capture_request"] = command
        chunked = None
        if not command.get("include_history") and self._storage_format == "chunked":
            chunked = archive.prepare(response, self._files)
            prepared = chunked.manifest
        else:
            raw = gzip.compress(archive.wire(response), mtime=0)
            prepared = self._files.prepare(raw, media_type="application/gzip")
        # Large replay evidence can contain hours of ticks. Keep a compact market
        # projection for ordinary snapshot reads without decoding that whole replay.
        market_attachment = None
        if command.get("include_history"):
            market_attachment = self._files.prepare(
                gzip.compress(
                    json.dumps(market, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0
                ),
                media_type="application/gzip",
            )
        return PreparedCapture(
            Snapshot.of(market),
            Snapshot.of(portfolio),
            tuple(Snapshot.of(value) for value in result.get("trades", [])),
            prepared,
            Snapshot.of(identity),
            Snapshot.of(result["history"]) if result.get("history") else None,
            market_attachment,
            self._review_evidence(market, result["history"]) if result.get("history") else None,
            chunked,
        )

    @staticmethod
    def _history_key(account_scope_id, symbol, start, end):
        return hashlib.sha256(json.dumps([account_scope_id, symbol,
            parse_utc(start).isoformat(), parse_utc(end).isoformat()]).encode()).hexdigest()

    @classmethod
    def _review_evidence(cls, market, history):
        # Hash detached evaluation facts, never observation IDs/times or unrelated account facts.
        def facts(value):
            if isinstance(value, dict):
                return {k: facts(v) for k, v in value.items()
                        if k not in {"snapshot_id", "snapshot_ids", "captured_at", "observed_at"}}
            return [facts(v) for v in value] if isinstance(value, list) else value
        window = cls._window(market, history, history["from"], history["to"]).to_dict()
        encoded = json.dumps(facts({**window, "contract": market["contract"]}),
                             sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return {"key": cls._history_key(market["account_scope_id"], market["symbol"],
                                        history["from"], history["to"]),
                "fingerprint": hashlib.sha256(encoded).hexdigest()}

    def review_evidence_token(self, snapshot_id, start, end, *, scope):
        record = self._docs.get(scope, "snapshot", snapshot_id)
        if not record:
            return None
        header = record.get("header", record)
        key = self._history_key(header["account_scope_id"], header["symbol"], start, end)
        evidence = self._docs.get(scope, "history_content", key)
        return f"{evidence['revision']}:{evidence['fingerprint']}" if evidence else None

    def commit_capture(self, prepared: PreparedCapture, uow: UnitOfWork, *, settings_revision=None) -> Snapshot:
        market = prepared.market.to_dict()
        if prepared.archive:
            for part in prepared.archive.parts:
                self._files.register(uow, part)
        ref = self._files.register(uow, prepared.raw_attachment)
        projection = (
            self._files.register(uow, prepared.market_attachment) if prepared.market_attachment else None
        )
        self._docs.insert(
            uow,
            "snapshot",
            market["snapshot_id"],
            {
                "storage_format": "market-snapshot/3" if prepared.archive else "market-snapshot/2",
                "header": {k: v for k, v in market.items() if k != "series"},
                "attachment_id": projection.id if projection else ref.id,
                "attachment_format": "market" if projection else archive.FORMAT if prepared.archive else "bridge-response",
            },
            parent_id=market["account_scope_id"],
            available_at=market["captured_at"],
        )
        self._docs.insert(
            uow,
            "evidence",
            market["snapshot_id"],
            {"attachment_id": ref.id, "sha256": ref.sha256, "identity": prepared.identity.to_dict(),
             "portfolio_snapshot_id": prepared.portfolio.to_dict()["snapshot_id"],
             "settings_revision": settings_revision,
             **({"storage_format": archive.FORMAT, "logical_sha256": prepared.archive.logical_sha256,
                 "logical_size": prepared.archive.logical_size} if prepared.archive else {})},
        )
        if prepared.history:
            self._docs.insert(
                uow,
                "history",
                market["snapshot_id"],
                {
                    "attachment_id": ref.id,
                    "from": prepared.history.to_dict()["from"],
                    "to": prepared.history.to_dict()["to"],
                    "account_scope_id": market["account_scope_id"],
                },
                parent_id=market["account_scope_id"],
                available_at=prepared.history.to_dict()["status"]["captured_at"],
            )
        if prepared.review_evidence:
            evidence = prepared.review_evidence
            old = self._docs.get(uow, "history_content", evidence["key"])
            if old is None:
                self._docs.insert(uow, "history_content", evidence["key"], evidence)
            elif old["fingerprint"] != evidence["fingerprint"]:
                self._docs.update(uow, "history_content", evidence["key"], evidence, old["revision"])
        return Snapshot.of(market)

    def capture_binding(self, snapshot_id, *, scope):
        """The immutable batch association; never substitute the latest account projection."""
        evidence = self._docs.get(scope, "evidence", snapshot_id)
        if not evidence:
            return None
        portfolio_id = evidence.get("portfolio_snapshot_id")
        if not portfolio_id:
            raw = self._response(scope, evidence["attachment_id"])
            portfolio_id = raw.get("result", {}).get("portfolio", {}).get("snapshot_id")
        return {"portfolio_snapshot_id": portfolio_id,
                "capture_settings_revision": evidence.get("settings_revision")}

    def get_snapshot(self, id: str, scope: ReadScope | None = None) -> Snapshot | None:
        if scope is None:
            with self._db.read() as read:
                return self.get_snapshot(id, read)
        record = self._docs.get(scope, "snapshot", id)
        if record is None:
            return None
        if record.get("storage_format") in {"market-snapshot/2", "market-snapshot/3"}:
            raw = self._response(scope, record["attachment_id"], expected=record["attachment_format"])
            market = raw if record["attachment_format"] == "market" else raw["result"]["market"]
            if record["header"] != {k: v for k, v in market.items() if k != "series"}:
                raise DomainError("SNAPSHOT_EVIDENCE_MISMATCH", "行情快照与原始证据不一致，不能使用。", 500)
            return Snapshot.of(market)
        if record.get("storage_format"):
            raise DomainError("SNAPSHOT_EVIDENCE_MISMATCH", "无法识别行情存储格式，不能使用。", 500)
        return Snapshot.of({key: value for key, value in record.items() if key not in {"id", "recorded_at"}})

    def _response(self, scope, attachment_id, *, expected=None):
        try:
            ref = self._files.get(scope, attachment_id)
            if ref is None:
                raise KeyError("附件未登记")
            chunked = ref.media_type == archive.MEDIA
            if expected == archive.FORMAT and not chunked:
                raise ValueError("行情清单编码不一致")
            if expected in {"market", "bridge-response"} and chunked:
                raise ValueError("行情附件编码不一致")
            content = self._files.read(scope, attachment_id)
            if chunked:
                return archive.decode(content, lambda ids: self._files.read_many(scope, ids))
            # Preserve the existing history reader's size semantics. The new
            # codec's ordinary-capture bound must not restrict legacy evidence.
            return json.loads(gzip.decompress(content))
        except (ValueError, KeyError, TypeError, OSError, EOFError, AttributeError, IndexError, zlib.error) as exc:
            raise DomainError("SNAPSHOT_EVIDENCE_MISMATCH", "行情证据缺失或校验失败，不能使用。", 500) from exc

    def read_evidence(self, snapshot_id, *, scope=None):
        """Reconstruct original bridge evidence without a new capture or model call."""
        if scope is None:
            with self._db.read() as reader:
                return self.read_evidence(snapshot_id, scope=reader)
        evidence = self._docs.get(scope, "evidence", snapshot_id)
        return Snapshot.of(self._response(scope, evidence["attachment_id"])) if evidence else None

    def latest(self, scope: ReadScope | None = None) -> Snapshot | None:
        if scope is None:
            with self._db.read() as read:
                return self.latest(read)
        records = self._docs.list(scope, "snapshot", limit=1)
        return self.get_snapshot(records[0]["id"], scope) if records else None

    def current(self, settings, scope: ReadScope | None = None) -> Snapshot | None:
        """Display freshness is evaluated now without rewriting the captured snapshot."""
        value = self.latest(scope=scope)
        if value is None:
            return None
        market = value.to_dict()
        cfg = _dict(settings).get("values", _dict(settings))
        if market.get("quote") and market["quote_status"]["state"] == "available":
            age = (parse_utc(self._clock()) - parse_utc(market["quote"]["event_time"])).total_seconds() * 1000
            if age < 0:
                market["quote_status"] = {
                    **market["quote_status"],
                    "state": "incomplete",
                    "reasons": ["报价时间晚于当前 UTC 时间，时间来源需要核对。"],
                }
            elif age > cfg["quote_max_age_ms"]:
                market["quote_status"] = {
                    **market["quote_status"],
                    "state": "stale",
                    "reasons": ["报价已超过设置的新鲜度，请刷新事实。"],
                }
        return Snapshot.of(market)

    def read_window(self, snapshot_id: str, start: str, end: str, scope: ReadScope | None = None) -> Snapshot:
        if parse_utc(start) >= parse_utc(end):
            raise DomainError("WINDOW_INVALID", "行情窗口结束时间必须晚于开始时间")
        if scope is None:
            with self._db.read() as read:
                return self.read_window(snapshot_id, start, end, read)
        snapshot = self.get_snapshot(snapshot_id, scope)
        if snapshot is None:
            raise DomainError("SNAPSHOT_NOT_FOUND", "指定行情快照不存在", 404)
        market = snapshot.to_dict()
        history = self._docs.get(scope, "history", snapshot_id)
        if history and "attachment_id" in history:
            raw_response = self._response(scope, history["attachment_id"])
            history = raw_response["result"]["history"]
        return self._window(market, history, start, end)

    def window_from_capture(self, prepared: PreparedCapture, start: str, end: str) -> Snapshot:
        """Pure projection of prepared facts before the final atomic result transaction."""
        return self._window(
            prepared.market.to_dict(), prepared.history.to_dict() if prepared.history else None, start, end
        )

    def saved_review_evidence(self, snapshot_id, *, endpoint_at=None, max_age_ms=5000, scope=None):
        """Read existing history only. No bridge call, capture, settings read or persistence."""
        if scope is None:
            with self._db.read() as reader:
                return self.saved_review_evidence(snapshot_id,endpoint_at=endpoint_at,max_age_ms=max_age_ms,scope=reader)
        record=self._docs.get(scope,'history',snapshot_id)
        if not record or not record.get('attachment_id'):
            return None
        result=self._response(scope,record['attachment_id'])['result']
        market=result['market']
        history=result['history']
        target=parse_utc(endpoint_at) if endpoint_at else None
        ticks=[t for t in history.get('ticks',[]) if target is None or
               0<=(target-parse_utc(t['event_time'])).total_seconds()*1000<=max_age_ms]
        window={k:market[k] for k in ['source_kind','account_scope_id','symbol']}
        window.update({'from':history['from'],'to':history['to'],'bars':history['bars'],
            'price_basis':history['price_basis'],'tick_status':history.get('tick_status','unavailable'),
            'ticks':ticks,'snapshot_id':snapshot_id,'status':history.get('status',{})})
        raw=result.get('raw',{})
        account=raw.get('account',{})
        source={'server':account.get('server'),'company':account.get('company'),
                'broker_timezone':raw.get('time_normalization',{}).get('broker_timezone')}
        return Snapshot.of({'window':window,'source':source})

    @staticmethod
    def _window(market: dict, history: dict | None, start: str, end: str) -> Snapshot:
        if parse_utc(start) >= parse_utc(end):
            raise DomainError("WINDOW_INVALID", "行情窗口结束时间必须晚于开始时间")
        snapshot_id = market["snapshot_id"]
        series = next((row for row in market["series"] if row["timeframe"] == "M1"), None)
        bars = [
            row
            for row in (history["bars"] if history else series["bars"] if series else [])
            if row["closed"]
            and parse_utc(row["open_at"]) >= parse_utc(start)
            and parse_utc(row["close_at"]) <= parse_utc(end)
        ]
        ticks = [
            row
            for row in (history.get("ticks", []) if history else [])
            if parse_utc(start) <= parse_utc(row["event_time"]) <= parse_utc(end)
        ]
        tick_status = history.get("tick_status", "incomplete") if history else "unavailable"
        if history and (
            parse_utc(history["from"]) > parse_utc(start) or parse_utc(history["to"]) < parse_utc(end)
        ):
            tick_status = "incomplete"
        complete = bool(
            bars
            and parse_utc(bars[0]["open_at"]) <= parse_utc(start)
            and parse_utc(bars[-1]["close_at"]) >= parse_utc(end)
        )
        complete = complete and all(
            parse_utc(a["close_at"]) == parse_utc(b["open_at"]) for a, b in pairwise(bars)
        )
        at = history["status"]["captured_at"] if history else market["captured_at"]
        state = "available" if complete else "incomplete"
        reasons = [] if complete else ["请求区间缺少完整连续的 M1 闭柱"]
        if series and any("TIME_BASIS_UNKNOWN" in reason for reason in series["status"]["reasons"]):
            state, complete = "incomplete", False
            reasons.extend(series["status"]["reasons"])
        price_basis = history["price_basis"] if history else series["price_basis"] if series else "unknown"
        bar_status = {
            "state": state,
            "captured_at": at,
            "observed_at": bars[-1]["close_at"] if bars else None,
            "reasons": reasons,
        }
        projected_series = [
            {"timeframe": "M1", "price_basis": price_basis, "bars": bars, "status": bar_status}
        ]
        for native in market["series"]:
            if native["timeframe"] == "M1":
                continue
            # Conditions observe a candle at its close. A candle closing inside
            # the requested window is relevant even if its open precedes it.
            minutes = {"M5": 5, "M15": 15, "H1": 60}[native["timeframe"]]
            interval = timedelta(minutes=minutes)
            first_close = parse_utc(start).replace(second=0, microsecond=0)
            first_close -= timedelta(minutes=first_close.minute % minutes)
            if first_close < parse_utc(start):
                first_close += interval
            expected_closes = []
            close = first_close
            while close <= parse_utc(end):
                expected_closes.append(close)
                close += interval
            native_bars = [
                bar
                for bar in native["bars"]
                if bar["closed"]
                and parse_utc(bar["close_at"]) >= parse_utc(start)
                and parse_utc(bar["close_at"]) <= parse_utc(end)
            ]
            native_complete = [parse_utc(bar["close_at"]) for bar in native_bars] == expected_closes and all(
                parse_utc(bar["close_at"]) - parse_utc(bar["open_at"]) == interval for bar in native_bars
            )
            native_complete = native_complete and not any(
                "TIME_BASIS_UNKNOWN" in reason for reason in native["status"]["reasons"]
            )
            projected_series.append(
                {
                    "timeframe": native["timeframe"],
                    "price_basis": native["price_basis"],
                    "bars": native_bars,
                    "status": {
                        "state": "available"
                        if native_complete
                        else "incomplete"
                        if native_bars
                        else "unavailable",
                        "captured_at": at,
                        "observed_at": native_bars[-1]["close_at"] if native_bars else None,
                        "reasons": []
                        if native_complete
                        else ["本次窗口需要的原生周期闭柱缺失或时间基准未确认"],
                    },
                }
            )
        return Snapshot.of(
            {
                "source_kind": market["source_kind"],
                "account_scope_id": market["account_scope_id"],
                "symbol": market["symbol"],
                "from": start,
                "to": end,
                "bars": bars,
                "ticks": ticks,
                "tick_status": tick_status,
                "price_basis": price_basis,
                "status": {
                    "state": state,
                    "captured_at": at,
                    "observed_at": bars[-1]["close_at"] if bars else None,
                    "reasons": reasons,
                },
                "bar_status": {
                    "state": state,
                    "captured_at": at,
                    "observed_at": bars[-1]["close_at"] if bars else None,
                    "reasons": reasons,
                },
                "snapshot_id": snapshot_id,
                "series": projected_series,
                "coverage": {
                    "from": start,
                    "to": end,
                    "actual_from": bars[0]["open_at"] if bars else None,
                    "actual_to": bars[-1]["close_at"] if bars else None,
                    "method": "stored-native-m1/1",
                    "complete": complete,
                },
            }
        )

    def fetch_history(self, command, settings) -> Snapshot:
        values = _dict(settings).get("values", _dict(settings))
        params = {**self._params(values), **_dict(command)}
        return Snapshot.of(self._history_chunks(params, values))

    def _history_chunks(self, params: dict, settings: dict, *, expected_scope: str | None = None) -> dict:
        start, end = parse_utc(params["from"]), parse_utc(params["to"])
        if start >= end or end - start > timedelta(days=3):
            raise DomainError("HISTORY_RANGE_INVALID", "单批历史区间必须在 0 到 3 天内，请分批读取")
        parts, cursor, span = [], start, timedelta(minutes=30)
        while cursor < end:
            boundary = min(end, (cursor + span).replace(second=0, microsecond=0))
            try:
                response = self._bridge.request(
                    "history",
                    {
                        **params,
                        "from": cursor.isoformat().replace("+00:00", "Z"),
                        "to": boundary.isoformat().replace("+00:00", "Z"),
                    },
                )
            except BridgeError as exc:
                if exc.code not in {"HISTORY_TOO_LARGE", "BRIDGE_RESPONSE_TOO_LARGE"} or span <= timedelta(
                    minutes=1
                ):
                    raise
                span = timedelta(minutes=max(1, int(span.total_seconds() / 60) // 2))
                continue
            self._check_identity(response["source_identity"], settings)
            actual_scope = response["source_identity"]["account_scope_id"]
            expected_scope = expected_scope or actual_scope
            if actual_scope != expected_scope:
                raise BridgeError(
                    "ACCOUNT_CHANGED", "分段历史读取期间账户变化，整批结果未接纳", retryable=False
                )
            parts.append(response["result"])
            cursor = boundary
            span = timedelta(minutes=30)
        if len(parts) == 1:
            return parts[0]
        bars, ticks, trades = [], [], {}
        for index, part in enumerate(parts):
            bars.extend(part["bars"])
            ticks.extend(
                tick
                for tick in part["ticks"]
                if index == 0 or parse_utc(tick["event_time"]) > parse_utc(part["from"])
            )
            for trade in part.get("trades", []):
                existing = trades.get(trade["trade_id"])
                if existing is None:
                    trades[trade["trade_id"]] = trade
                else:
                    deal_map = {d["deal_id"]: d for d in existing["deals"]}
                    deal_map.update({d["deal_id"]: d for d in trade["deals"]})
                    existing["deals"] = sorted(
                        deal_map.values(), key=lambda d: (d["event_time"], d["deal_id"])
                    )
                    if trade.get("opening_complete"):
                        existing.update({k: trade[k] for k in ("side", "opened_at", "opening_complete")})
                    existing["captured_at"] = trade.get("captured_at", parts[-1]["status"]["captured_at"])
        reasons = list(dict.fromkeys(reason for part in parts for reason in part["status"]["reasons"]))
        bar_reasons = list(
            dict.fromkeys(reason for part in parts for reason in part["bar_status"]["reasons"])
        )
        tick_complete = all(part.get("tick_status") == "available" for part in parts)
        captured_at = parts[-1]["status"]["captured_at"]
        return {
            "snapshot_id": new_id(),
            "source_kind": self._mode,
            "account_scope_id": expected_scope,
            "symbol": params["symbol"],
            "from": params["from"],
            "to": params["to"],
            "bars": bars,
            "ticks": ticks,
            "tick_status": "available" if tick_complete else "incomplete" if ticks else "unavailable",
            "price_basis": "bid" if all(p["price_basis"] == "bid" for p in parts) else "unknown",
            "bar_status": {
                "state": "available" if not bar_reasons else "incomplete",
                "reasons": bar_reasons,
                "captured_at": captured_at,
                "observed_at": bars[-1]["close_at"] if bars else None,
            },
            "status": {
                "state": "available" if not reasons else "incomplete",
                "reasons": reasons,
                "captured_at": captured_at,
                "observed_at": params["to"],
            },
            "coverage": {
                "from": params["from"],
                "to": params["to"],
                "actual_from": ticks[0]["event_time"] if ticks else None,
                "actual_to": ticks[-1]["event_time"] if ticks else None,
                "method": "source-bound-30-minute-chunks/1",
                "complete": tick_complete,
            },
            "trades": list(trades.values()),
            "raw": {
                "parts": [
                    {
                        "snapshot_id": part["snapshot_id"],
                        "from": part["from"],
                        "to": part["to"],
                        "raw": part["raw"],
                        "coverage": part["coverage"],
                    }
                    for part in parts
                ]
            },
        }

    def calculation(self, kind: str, command, settings) -> Snapshot:
        if kind not in {"profit", "margin"}:
            raise DomainError("OPERATION_NOT_ALLOWED", "仅提供利润或保证金只读试算")
        values = _dict(settings).get("values", _dict(settings))
        response = self._bridge.request(kind, {**self._params(values), **_dict(command)})
        self._check_identity(response["source_identity"], values)
        return Snapshot.of(response["result"])

    def identity(self, settings) -> Snapshot:
        values = _dict(settings).get("values", _dict(settings))
        response = self._bridge.request("identity", self._params(values))
        self._check_identity(response["source_identity"], values)
        return Snapshot.of(response["result"])

    def _params(self, values: dict) -> dict:
        return {
            "symbol": values.get("symbol", "XAUUSD"),
            "quote_max_age_ms": values.get("quote_max_age_ms", 30000),
            "terminal_path": values.get("connection", {}).get("terminal_path"),
            "broker_timezone": values.get("connection", {}).get("broker_timezone"),
            "history_days": values.get("connection", {}).get("history_days", 30),
            "captured_at": self._clock() if self._mode == "synthetic" else None,
        }

    def _check_identity(self, identity, values):
        if not identity or identity.get("source_kind") != self._mode:
            raise BridgeError("SOURCE_IDENTITY_MISMATCH", "来源模式不匹配，未回退为合成数据", retryable=False)
        expected = values.get("connection", {}).get("account_scope_id")
        if expected and identity.get("account_scope_id") != expected:
            raise BridgeError(
                "ACCOUNT_CHANGED", "当前终端账户已变化，请在设置中确认账户来源", retryable=False
            )

    def close(self):
        self._bridge.close()
