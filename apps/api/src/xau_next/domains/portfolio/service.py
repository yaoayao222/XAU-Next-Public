from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from decimal import Decimal

from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import new_id, parse_utc, utc_now
from xau_next.shared.holding import holding_key, digest

from .model import DEAL_FIELDS, FEE_FIELDS, aggregate
from .usage import ExecutionUsage

PORTFOLIO_FIELDS = (
    "snapshot_id",
    "account_scope_id",
    "account_status",
    "account",
    "orders_status",
    "orders",
    "revision",
)


def data(value):
    return value.to_dict() if isinstance(value, Snapshot) else json.loads(json.dumps(value))


def identity(order):
    return hashlib.sha256(
        json.dumps(
            [order[key] for key in ("account_scope_id", "kind", "broker_id")], separators=(",", ":")
        ).encode()
    ).hexdigest()


def fail(code, path, message, status=422):
    return DomainError(code, message, status, [{"path": path, "code": code, "message": message}])


def decimal_value(value, path, *, positive=False):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", value)
        or len(value) > 48
    ):
        raise fail("DECIMAL_INVALID", path, "金额、价格和数量须使用有效的十进制字符串。")
    result = Decimal(value)
    if positive and result <= 0:
        raise fail("DECIMAL_INVALID", path, "价格和数量必须大于零。")
    return result


class PortfolioService:
    def __init__(self, db, clock=utc_now):
        self._db, self._clock, self._docs = db, clock, db.documents("portfolio")
        self._usage = ExecutionUsage(self._docs, identity, clock)
        with db.write() as tx:
            self._usage.backfill(tx)

    def entry_usage(self, plan_id, *, available_at, account_scope_id, symbol, scope):
        return Snapshot.of(self._usage.status(scope, plan_id, available_at=available_at,
                                             account_scope_id=account_scope_id, symbol=symbol))

    def holding_events(self, order, opened_at, *, scope):
        if not opened_at:
            return []
        return [{k: row[k] for k in ("rule_key", "kind", "rule", "observed_at")}
                for row in self._docs.list(scope, "holding_event", parent_id=holding_key(order, opened_at), limit=30)]

    def remember_holding_events(self, order, opened_at, events, *, uow):
        if not opened_at:
            return
        parent = holding_key(order, opened_at)
        for event in events:
            id = digest([parent, event["rule_key"]])
            if not self._docs.get(uow, "holding_event", id):
                self._docs.insert(uow, "holding_event", id, event, parent_id=parent,
                                  available_at=event["observed_at"])

    def sync_facts(self, portfolio_snapshot, *, trades=(), uow=None):
        incoming = data(portfolio_snapshot)
        envelopes = [data(trade) for trade in trades]
        order_ids = [identity(item["order"]) for item in incoming["orders"]]
        if len(order_ids) != len(set(order_ids)):
            raise fail("ORDER_IDENTITY_INVALID", "/orders", "读取到重复的账户订单身份。")
        for item in incoming["orders"]:
            if item["order"]["account_scope_id"] != incoming["account_scope_id"]:
                raise fail("ACCOUNT_MISMATCH", "/orders", "订单身份与读取账户不一致。")
        with nullcontext(uow) if uow else self._db.write() as tx:
            raw = self._docs.get(tx, "snapshot", incoming["snapshot_id"])
            if raw and raw["wire"] != incoming:
                raise DomainError("SNAPSHOT_CONFLICT", "相同快照身份出现不同内容，已保留原事实。", 409)
            if raw is None:
                self._docs.insert(
                    tx,
                    "snapshot",
                    incoming["snapshot_id"],
                    {"wire": incoming},
                    parent_id=incoming["account_scope_id"],
                )
            if incoming["orders_status"]["state"] != "unavailable":
                for order in incoming["orders"]:
                    self._usage.position(tx, order, incoming["snapshot_id"])
            for envelope in envelopes:
                if envelope["account_scope_id"] != incoming["account_scope_id"]:
                    raise fail("ACCOUNT_MISMATCH", "/trades", "成交事实必须与当前读取账户一致。")
                envelope["portfolio_snapshot_id"] = incoming["snapshot_id"]
                self._merge_trade(tx, envelope)
            current = self._docs.get(tx, "current", "portfolio")
            if current and current["snapshot_id"] == incoming["snapshot_id"]:
                return self._portfolio_snapshot(tx, current)
            merged = data(incoming)
            if current and current["account_scope_id"] == incoming["account_scope_id"]:
                # A late capture must not replace a newer snapshot. Raw history is retained above.
                old_time = max(
                    parse_utc(current[key]["captured_at"]) for key in ("account_status", "orders_status")
                )
                new_time = max(
                    parse_utc(incoming[key]["captured_at"]) for key in ("account_status", "orders_status")
                )
                if new_time < old_time:
                    return self._portfolio_snapshot(tx, current)
                if incoming["orders_status"]["state"] == "unavailable":
                    merged["orders"] = current["orders"]
                    merged["orders_status"] = {
                        **merged["orders_status"],
                        "observed_at": current["orders_status"]["observed_at"],
                    }
                elif incoming["orders_status"]["state"] == "incomplete":
                    known = {identity(order["order"]): order for order in current["orders"]}
                    known.update({identity(order["order"]): order for order in incoming["orders"]})
                    merged["orders"] = list(known.values())
                if (
                    incoming["account_status"]["state"] in ("unavailable", "incomplete")
                    and incoming["account"] is None
                ):
                    merged["account"] = current["account"]
                    merged["account_status"] = {
                        **merged["account_status"],
                        "observed_at": current["account_status"]["observed_at"],
                    }
            # Broker reads do not get to assert an application plan association.
            merged["orders"] = [{**item, "plan_id": None} for item in merged["orders"]]
            merged.pop("revision", None)
            if current:
                current = self._docs.update(tx, "current", "portfolio", merged, current["revision"])
            else:
                current = self._docs.insert(tx, "current", "portfolio", merged)
            return self._portfolio_snapshot(tx, current)

    def get_portfolio(self, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            current = self._docs.get(tx, "current", "portfolio")
            if current:
                return self._portfolio_snapshot(tx, current)
            status = {
                "state": "unavailable",
                "observed_at": None,
                "captured_at": self._clock(),
                "reasons": ["尚未读取终端事实。"],
            }
            return Snapshot.of(
                {
                    "snapshot_id": "unavailable",
                    "account_scope_id": "unconfigured",
                    "account_status": status,
                    "account": None,
                    "orders_status": status,
                    "orders": [],
                    "revision": 0,
                }
            )

    def get_snapshot(self, snapshot_id, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            row = self._docs.get(tx, "snapshot", snapshot_id)
            if row is None:
                raise DomainError("PORTFOLIO_NOT_FOUND", "没有找到指定账户事实快照。", 404)
            return Snapshot.of(row["wire"])

    def create_association(self, command, *, plan_snapshot=None, trade_snapshot=None, uow=None):
        command = data(command)
        with nullcontext(uow) if uow else self._db.write() as tx:
            current = self._current(tx)
            key = identity(command["order"])
            linked = self._docs.get(tx, "link", key)
            if command.get("expected_order_token") is not None:
                # A disappeared position must not silently become a historical-trade edit.
                order = next((item for item in current["orders"] if identity(item["order"]) == key), None)
                if order is None or command["expected_order_token"] != self._association_token(current, order, linked):
                    raise DomainError("VERSION_CONFLICT", "这笔持仓或挂单的关键资料、读取状态或方案关联已变化。已保留填写内容，请核对最新资料后再保存。", 409)
            elif command["expected_portfolio_revision"] != current["revision"]:
                raise DomainError("VERSION_CONFLICT", "订单或关联已变化，请刷新后重试。", 409)
            self._known_order(tx, command["order"], current)
            if command["action"] == "link":
                if not command["plan_id"] or plan_snapshot is None:
                    raise fail("PLAN_REQUIRED", "/plan_id", "关联必须明确选择一个方案。")
                context = data(plan_snapshot)
                plan = context.get("plan", context)
                if plan["plan_id"] != command["plan_id"]:
                    raise fail("PLAN_MISMATCH", "/plan_id", "选择的方案与快照不一致。")
                if context.get("account_scope_id") != command["order"]["account_scope_id"]:
                    raise fail("ACCOUNT_MISMATCH", "/order", "不能关联到其他账户来源的方案。")
                fact = self._known_order(tx, command["order"], current)
                if context.get("symbol") != fact["symbol"]:
                    raise fail("SYMBOL_MISMATCH", "/order", "不能关联到其他品种的方案。")
                plan_id = command["plan_id"]
            elif command["action"] == "unlink":
                if linked is None or linked["plan_id"] is None:
                    raise DomainError("ASSOCIATION_NOT_FOUND", "该订单尚未关联方案。", 409)
                if command["plan_id"] not in (None, linked["plan_id"]):
                    raise DomainError("VERSION_CONFLICT", "撤销目标与当前关联不一致。", 409)
                plan_id = None
            else:
                raise fail("ASSOCIATION_ACTION_INVALID", "/action", "关联动作必须为 link 或 unlink。")
            if not command["reason"].strip():
                raise fail("REASON_REQUIRED", "/reason", "请记录关联或撤销的依据。")
            now, association_id = self._clock(), new_id()
            output = {"association_id": association_id, "created_at": now, "input": command}
            self._docs.insert(
                tx,
                "association",
                association_id,
                {**output, "sequence": linked["revision"] + 1 if linked else 1},
                parent_id=key,
                available_at=now,
            )
            pointer = {
                "order": command["order"],
                "plan_id": plan_id,
                "association_id": association_id,
                "available_at": now,
                "source": "manual",
            }
            if linked:
                self._docs.update(tx, "link", key, pointer, linked["revision"])
            else:
                self._docs.insert(tx, "link", key, pointer, available_at=now)
            self._usage.association(tx, pointer)
            # Revision covers association changes while underlying broker fields remain identical.
            self._docs.update(tx, "current", "portfolio", self._current_payload(current), current["revision"])
            return Snapshot.of(output)

    def record_decision(self, command, *, uow=None):
        command = data(command)
        with nullcontext(uow) if uow else self._db.write() as tx:
            current = self._current(tx)
            order = self._known_order(tx, command["order"], current)
            action, kind = command["action"], command["order"]["kind"]
            allowed = {"hold", "close", "reduce", "modify_sl_tp", "cancel_pending", "observe"}
            if (
                action not in allowed
                or (kind == "position" and action == "cancel_pending")
                or (kind == "pending_order" and action in ("hold", "close", "reduce"))
            ):
                raise fail("DECISION_ACTION_INVALID", "/action", "该人工决定不适用于当前订单种类。")
            volume = command["volume_lots"]
            if volume is not None:
                value = decimal_value(volume, "/volume_lots", positive=True)
                known_volume = order.get("volume_lots")
                if known_volume is not None and value > Decimal(known_volume):
                    raise fail(
                        "DECISION_VOLUME_INVALID", "/volume_lots", "人工操作数量不能超过已知持仓数量。"
                    )
                if action == "reduce" and known_volume is not None and value >= Decimal(known_volume):
                    raise fail("DECISION_VOLUME_INVALID", "/volume_lots", "减仓数量须小于已知持仓数量。")
            elif action == "reduce":
                raise fail("DECISION_VOLUME_REQUIRED", "/volume_lots", "减仓意图必须填写明确数量。")
            for field in ("stop_loss", "take_profit"):
                if command[field] is not None:
                    decimal_value(command[field], "/" + field, positive=True)
            if action == "modify_sl_tp" and command["stop_loss"] is None and command["take_profit"] is None:
                raise fail("DECISION_PRICE_REQUIRED", "/stop_loss", "修改止损止盈至少填写一个价格。")
            if not command["note"].strip():
                raise fail("NOTE_REQUIRED", "/note", "请填写操作意图说明。")
            output = {
                "decision_id": new_id(),
                "recorded_at": self._clock(),
                "state": "recorded_pending_broker_fact",
                "input": command,
            }
            self._docs.insert(
                tx,
                "decision",
                output["decision_id"],
                output,
                parent_id=identity(command["order"]),
                available_at=output["recorded_at"],
            )
            return Snapshot.of(output)

    def list_trades(self, *, limit=50, cursor=None, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            rows = self._docs.list(tx, "trade", limit=limit + 1, cursor=cursor)
            return Snapshot.of(
                {
                    "items": [self._trade_summary(tx, row) for row in rows[:limit]],
                    "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
                }
            )

    def get_trade(self, trade_id, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            row = self._required_trade(tx, trade_id)
            if len(row["deal_ids"]) > 10000:
                raise DomainError("TRADE_TOO_LARGE", "成交生命周期超过详情容量，需缩小导出范围。", 413)
            deals = [self._docs.get(tx, "deal", id)["fact"] for id in row["deal_ids"]]
            deals.sort(key=lambda item: (parse_utc(item["event_time"]), item["deal_id"]))
            return Snapshot.of(
                {
                    "trade": self._trade_summary(tx, row),
                    "deals": [{key: deal[key] for key in DEAL_FIELDS} for deal in deals],
                }
            )

    def get_trade_metrics(self, trade_id, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            row = self._required_trade(tx, trade_id)
            order_ref = {
                "account_scope_id": row["trade"]["account_scope_id"],
                "kind": "position",
                "broker_id": row["envelope"]["broker_position_id"],
            }
            linked = self._docs.get(tx, "link", identity(order_ref))
            return Snapshot.of(
                {
                    **row["metrics"],
                    "available_at": row["available_at"],
                    "revision": row["revision"],
                    "capture_evidence": row["envelope"].get("capture_evidence"),
                    "portfolio_snapshot_id": row["envelope"].get("portfolio_snapshot_id"),
                    "source_kind": row.get("source_kind", "unknown"),
                    "market_snapshot_id": row["envelope"].get("market_snapshot_id"),
                    "order_ref": order_ref,
                    "association_id": linked["association_id"] if linked else None,
                    "association_available_at": linked["available_at"] if linked else None,
                    "association_revision": linked["revision"] if linked else 0,
                    "accounting": self._trade_summary(tx, row),
                }
            )

    def trade_history(self, trade_id, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            return tuple(Snapshot.of(row) for row in self._docs.history(tx, "trade", trade_id))

    def list_associations(self, order, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            return tuple(
                Snapshot.of({key: row[key] for key in ("association_id", "created_at", "input")})
                for row in self._docs.list(tx, "association", parent_id=identity(data(order)), limit=1000)
            )

    def association_at(self, order, as_of, *, scope=None):
        with nullcontext(scope) if scope else self._db.read() as tx:
            records = self._docs.list(
                tx, "association", parent_id=identity(data(order)), available_before=as_of, limit=1000
            )
            if not records:
                return None
            latest = max(records, key=lambda row: (parse_utc(row["created_at"]), row["sequence"]))
            return Snapshot.of(
                {
                    "association_id": latest["association_id"],
                    "available_at": latest["created_at"],
                    "plan_id": latest["input"]["plan_id"] if latest["input"]["action"] == "link" else None,
                    "source": "manual",
                }
            )

    def position_opened_at(self, order, *, scope):
        """Only an exact broker identity with complete entry history can start the timer."""
        link = self._docs.get(scope, "order_trade", identity(order["order"]))
        if link:
            facts = self.get_trade_metrics(link["trade_id"], scope=scope).to_dict()
            if facts["lifecycle_complete"] and facts["accounting"]["symbol"] == order["symbol"]:
                return facts["accounting"]["opened_at"]
        return None

    def _merge_trade(self, tx, envelope):
        required = (
            "trade_id",
            "account_scope_id",
            "broker_position_id",
            "symbol",
            "side",
            "opened_at",
            "currency",
            "deals",
        )
        for field in required:
            if field not in envelope:
                raise fail("TRADE_FACT_MISSING", "/trades/" + field, "成交资料缺少真实身份或方向字段。")
        if envelope["side"] not in ("long", "short", None):
            raise fail("TRADE_SIDE_INVALID", "/trades/side", "成交方向缺失，不能自动推断。")
        if envelope["opened_at"] is not None:
            parse_utc(envelope["opened_at"])
        existing = self._docs.get(tx, "trade", envelope["trade_id"])
        captured_at = envelope.get("captured_at") or self._clock()
        parse_utc(captured_at)
        source_kind = envelope.get("source_kind", "unknown")
        if source_kind not in ("mt5", "synthetic", "imported", "unknown"):
            raise fail("SOURCE_INVALID", "/trades/source_kind", "成交来源必须明确标识或保留 unknown。")
        if (
            existing
            and existing.get("source_kind", "unknown") not in ("unknown", source_kind)
            and source_kind != "unknown"
        ):
            raise DomainError("SOURCE_MISMATCH", "不能把不同事实来源的成交合并为同一生命周期。", 409)
        if existing and existing.get("source_kind", "unknown") == "unknown" and source_kind != "unknown":
            for saved_id in existing["deal_ids"]:
                known_source = self._docs.get(tx, "deal", saved_id).get("source_kind", "unknown")
                if known_source not in ("unknown", source_kind):
                    raise DomainError(
                        "SOURCE_MISMATCH", "该生命周期已含其他来源的成交，不能混入新来源。", 409
                    )
        stable_fields = ("account_scope_id", "broker_position_id", "symbol", "currency")
        if existing and any(existing["envelope"][field] != envelope[field] for field in stable_fields):
            raise DomainError(
                "TRADE_IDENTITY_CONFLICT", "相同成交生命周期身份对应不同账户、品种或券商对象。", 409
            )
        if existing:
            for field in ("side", "opened_at"):
                if envelope[field] is None:
                    envelope[field] = existing["envelope"][field]
            if existing["envelope"].get("opening_complete") is True:
                envelope["opening_complete"] = True
        ids = set(existing["deal_ids"]) if existing else set()
        changed = existing is None or any(
            envelope.get(field) != existing["envelope"].get(field)
            for field in ("side", "opened_at", "opening_complete")
        )
        batch = {}
        for fact in envelope["deals"]:
            if "entry_kind" not in fact:
                raise fail(
                    "TRADE_FACT_MISSING",
                    "/trades/deals/entry_kind",
                    "缺少真实入场/出场标记，不能由盈亏猜测。",
                )
            for field in DEAL_FIELDS:
                if field not in fact:
                    raise fail(
                        "TRADE_FACT_MISSING",
                        "/trades/deals/" + field,
                        "成交事实字段缺失，应明确为 null 而不是省略。",
                    )
            parse_utc(fact["event_time"])
            if decimal_value(fact["volume_lots"], "/trades/deals/volume_lots") < 0:
                raise fail("DECIMAL_INVALID", "/trades/deals/volume_lots", "券商成交数量不能为负。")
            decimal_value(fact["price"], "/trades/deals/price", positive=True)
            for field in FEE_FIELDS:
                if fact[field] is not None:
                    decimal_value(fact[field]["amount"], "/trades/deals/" + field)
            deal_id = hashlib.sha256(
                (envelope["account_scope_id"] + "\0" + fact["deal_id"]).encode()
            ).hexdigest()
            if deal_id in batch and batch[deal_id] != fact:
                raise DomainError(
                    "DEAL_IDENTITY_CONFLICT", "同一批次的相同成交编号包含冲突事实，无法确定顺序。", 409
                )
            batch[deal_id] = fact
            previous = self._docs.get(tx, "deal", deal_id)
            if previous and previous["trade_id"] != envelope["trade_id"]:
                raise DomainError("DEAL_IDENTITY_CONFLICT", "相同账户成交编号被重复归入不同生命周期。", 409)
            if (
                previous
                and previous.get("source_kind", "unknown") not in ("unknown", source_kind)
                and source_kind != "unknown"
            ):
                raise DomainError("SOURCE_MISMATCH", "相同成交编号对应不同事实来源，不能合并。", 409)
            row = {
                "trade_id": envelope["trade_id"],
                "fact": fact,
                "captured_at": captured_at,
                "source_kind": source_kind,
                "market_snapshot_id": envelope.get("market_snapshot_id"),
                "portfolio_snapshot_id": envelope.get("portfolio_snapshot_id"),
            }
            if previous is None:
                self._docs.insert(tx, "deal", deal_id, row, parent_id=envelope["trade_id"])
                changed = True
            elif previous["fact"] != fact and parse_utc(captured_at) >= parse_utc(previous["captured_at"]):
                self._docs.update(tx, "deal", deal_id, row, previous["revision"])
                changed = True
            elif previous["fact"] == fact and previous.get("source_kind", "unknown") != source_kind:
                if source_kind != "unknown" and parse_utc(captured_at) >= parse_utc(previous["captured_at"]):
                    self._docs.update(tx, "deal", deal_id, row, previous["revision"])
                    changed = True
            elif previous["fact"] == fact and parse_utc(captured_at) > parse_utc(previous["captured_at"]):
                self._docs.update(tx, "deal", deal_id, row, previous["revision"])
            ids.add(deal_id)
        if not changed:
            return
        deal_rows = [self._docs.get(tx, "deal", id) for id in ids]
        deals = [row["fact"] for row in deal_rows]
        sources = {row.get("source_kind", "unknown") for row in deal_rows}
        resolved_source = next(iter(sources)) if len(sources) == 1 else "unknown"
        trade, metrics = aggregate(envelope, deals)
        trade["source_kind"] = resolved_source
        payload = {
            "trade": trade,
            "metrics": metrics,
            "envelope": {key: value for key, value in envelope.items() if key != "deals"},
            "deal_ids": sorted(ids),
            "available_at": self._clock(),
            "source_kind": resolved_source,
        }
        if existing:
            self._docs.update(
                tx, "trade", envelope["trade_id"], payload, existing["revision"], state=trade["state"]
            )
        else:
            self._docs.insert(
                tx,
                "trade",
                envelope["trade_id"],
                payload,
                parent_id=envelope["account_scope_id"],
                state=trade["state"],
                available_at=payload["available_at"],
            )
        lookup = identity(
            {
                "account_scope_id": envelope["account_scope_id"],
                "kind": "position",
                "broker_id": envelope["broker_position_id"],
            }
        )
        prior_lookup = self._docs.get(tx, "order_trade", lookup)
        if prior_lookup and prior_lookup["trade_id"] != envelope["trade_id"]:
            raise DomainError("TRADE_IDENTITY_CONFLICT", "券商持仓身份已关联到另一生命周期。", 409)
        if prior_lookup is None:
            self._docs.insert(tx, "order_trade", lookup, {"trade_id": envelope["trade_id"]})
        self._usage.trade(tx, payload)

    def _portfolio_snapshot(self, tx, row):
        output = {key: row[key] for key in PORTFOLIO_FIELDS}
        output["trades_status"] = row.get("trades_status")
        output["orders"] = []
        for order in row["orders"]:
            linked = self._docs.get(tx, "link", identity(order["order"]))
            output["orders"].append({**order, "plan_id": linked["plan_id"] if linked else None,
                                     "association_token": self._association_token(row, order, linked)})
        return Snapshot.of(output)

    @staticmethod
    def _association_token(current, order, linked):
        # Never include quote/profit/account totals or observation timestamps.
        # The link revision prevents changing away and back from reusing an old token.
        payload = {
            "format": "order-association/1",
            "account_scope_id": current["account_scope_id"],
            "orders_state": current["orders_status"]["state"],
            "fact": {k: order.get(k) for k in (
                "order", "symbol", "side", "volume_lots", "entry_price",
                "stop_loss", "take_profit", "event_time",
            )},
            "association_revision": linked["revision"] if linked else 0,
            "association_id": linked["association_id"] if linked else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _trade_summary(self, tx, row):
        trade = data(row["trade"])
        linked = self._docs.get(
            tx,
            "link",
            identity(
                {
                    "account_scope_id": trade["account_scope_id"],
                    "kind": "position",
                    "broker_id": row["envelope"]["broker_position_id"],
                }
            ),
        )
        trade["plan_id"] = linked["plan_id"] if linked else None
        trade["source_kind"] = row.get("source_kind", "unknown")
        return trade

    def _known_order(self, tx, reference, current):
        if reference["account_scope_id"] != current["account_scope_id"]:
            raise fail("ACCOUNT_MISMATCH", "/order", "订单不属于当前选择的账户。")
        key = identity(reference)
        order = next((item for item in current["orders"] if identity(item["order"]) == key), None)
        if order:
            return order
        lookup = self._docs.get(tx, "order_trade", key)
        if lookup:
            row = self._required_trade(tx, lookup["trade_id"])
            return {**row["trade"], "volume_lots": row["metrics"]["remaining_volume_lots"]}
        raise DomainError("ORDER_NOT_FOUND", "没有找到该账户的当前订单或已保存成交事实。", 404)

    def _current(self, tx):
        row = self._docs.get(tx, "current", "portfolio")
        if row is None:
            raise DomainError("PORTFOLIO_UNAVAILABLE", "尚未读取账户和订单事实。", 409)
        return row

    def _required_trade(self, tx, id):
        row = self._docs.get(tx, "trade", id)
        if row is None:
            raise DomainError("TRADE_NOT_FOUND", "没有找到该成交生命周期。", 404)
        return row

    @staticmethod
    def _current_payload(row):
        return {
            **{key: row[key] for key in PORTFOLIO_FIELDS if key != "revision"},
            "trades_status": row.get("trades_status"),
        }
