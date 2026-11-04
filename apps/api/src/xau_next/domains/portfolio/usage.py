"""Durable execution evidence and one-use plan consumption, owned by portfolio.

Only exact user associations consume a plan. Unattributed executions block reuse
without inventing an association. No notification or manual intent is a fill.
"""

from decimal import Decimal

from xau_next.shared.values import parse_utc


class ExecutionUsage:
    def __init__(self, store, identity, clock):
        self.store, self.identity, self.clock = store, identity, clock

    def rows(self, scope, kind, **filters):
        cursor = None
        while True:
            page = self.store.list(scope, kind, limit=1000, cursor=cursor, **filters)
            yield from page
            if len(page) < 1000:
                return
            cursor = page[-1]["id"]

    def position(self, tx, order, snapshot_id):
        if order["order"]["kind"] == "position" and Decimal(order["volume_lots"]) > 0:
            self.execution(tx, order["order"], order["symbol"], order["event_time"],
                           {"kind": "position", "portfolio_snapshot_id": snapshot_id})

    def trade(self, tx, row):
        deals = [self.store.get(tx, "deal", id)["fact"] for id in row["deal_ids"]]
        positive = [d for d in deals if Decimal(d["volume_lots"]) > 0
                    and d["entry_kind"] in {"in", "out", "out_by"}]
        if not positive:
            return
        first = min(positive, key=lambda d: parse_utc(d["event_time"]))
        order = {"account_scope_id": row["trade"]["account_scope_id"], "kind": "position",
                 "broker_id": row["envelope"]["broker_position_id"]}
        self.execution(tx, order, row["trade"]["symbol"], first["event_time"],
                       {"kind": "deal", "trade_id": row["trade"]["trade_id"], "deal_id": first["deal_id"]})

    def execution(self, tx, order, symbol, occurred_at, proof):
        key = self.identity(order)
        row = self.store.get(tx, "execution", key)
        if row is None:
            row = self.store.insert(tx, "execution", key,
                {"order": order, "symbol": symbol, "occurred_at": occurred_at,
                 "observed_at": self.clock(), "proof": proof}, parent_id=order["account_scope_id"])
        linked = self.store.get(tx, "link", key)
        if linked and linked["plan_id"]:
            self.association(tx, linked)
        # An earlier explicit association remains evidence even after unlinking.
        # This path also handles delayed fills/history arriving after cancellation.
        for old in self.rows(tx, "association", parent_id=key):
            if old["input"]["action"] == "link":
                self.association(tx, {"order": old["input"]["order"], "plan_id": old["input"]["plan_id"],
                                      "association_id": old["association_id"]})

    def association(self, tx, link, *, legacy=False):
        plan_id, order = link["plan_id"], link["order"]
        if not plan_id or order["kind"] != "position":
            return  # Pending, cancelled and intended orders are not fills.
        previous = self.store.get(tx, "plan_usage", plan_id)
        if previous and previous["state"] == "USED":
            return
        execution = self.store.get(tx, "execution", self.identity(order))
        if not execution and not legacy:
            return
        value = {"plan_id": plan_id, "state": "USED" if execution else "UNKNOWN",
                 "association_id": link["association_id"], "order": order,
                 "execution_id": execution["id"] if execution else None,
                 "recorded_at": self.clock()}
        if previous:
            if value["state"] == previous["state"]:
                return
            self.store.update(tx, "plan_usage", plan_id, value, previous["revision"], state=value["state"])
        else:
            self.store.insert(tx, "plan_usage", plan_id, value, state=value["state"])

    def backfill(self, tx):
        """Append projections once; never rewrite old broker facts or associations."""
        if self.store.get(tx, "usage_index", "1"):
            return
        for row in self.rows(tx, "trade"):
            self.trade(tx, row)
        current = self.store.get(tx, "current", "portfolio")
        if current and current["orders_status"]["state"] != "unavailable":
            for order in current["orders"]:
                self.position(tx, order, current["snapshot_id"])
        for row in self.rows(tx, "association"):
            if row["input"]["action"] == "link":
                self.association(tx, {"order": row["input"]["order"], "plan_id": row["input"]["plan_id"],
                                      "association_id": row["association_id"]}, legacy=True)
        self.store.insert(tx, "usage_index", "1", {"completed_at": self.clock()})

    def status(self, scope, plan_id, *, available_at, account_scope_id, symbol):
        used = self.store.get(scope, "plan_usage", plan_id)
        if used:
            return {"state": used["state"], "evidence": used}
        for execution in self.rows(scope, "execution", parent_id=account_scope_id):
            if execution["symbol"] != symbol or parse_utc(execution["occurred_at"]) < parse_utc(available_at):
                continue
            linked = self.store.get(scope, "link", execution["id"])
            if not linked or not linked["plan_id"]:
                return {"state": "UNKNOWN", "evidence": execution}
        return {"state": "UNUSED", "evidence": None}
