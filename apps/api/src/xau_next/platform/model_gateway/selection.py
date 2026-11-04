"""Durable at-most-once claims for isolated selection, independent of business tables."""

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from xau_next.shared.errors import DomainError


def reject(code, message):
    raise DomainError(code, message, 409)


class SelectionLedger:
    """A claimed slot is spent even if its sender crashes; there is no lease/retry."""

    def __init__(self, path, plan_hash):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY, hash TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS claims (slot TEXT PRIMARY KEY, stage INTEGER NOT NULL, "
                "provider TEXT NOT NULL, day TEXT NOT NULL, reserved TEXT, currency TEXT, "
                "state TEXT NOT NULL, at TEXT NOT NULL, result TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS halt (id INTEGER PRIMARY KEY, reason TEXT NOT NULL)")
            previous = db.execute("SELECT hash FROM identity WHERE id=1").fetchone()
            if previous and previous[0] != plan_hash:
                reject("SELECTION_PLAN_CHANGED", "实验账本已经绑定另一份计划，不可覆盖。")
            db.execute("INSERT OR IGNORE INTO identity VALUES (1,?)", (plan_hash,))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(
        self,
        slot,
        stage,
        provider,
        at,
        *,
        total_limit,
        stage_limit,
        daily_limit,
        reserved=None,
        currency=None,
        cost_limit=None,
    ):
        # No caller can raise the route ceiling by editing a plan or switching providers.
        if stage not in (1, 2) or not 0 < total_limit <= 48 or not 0 < stage_limit <= 24:
            reject("SELECTION_BUDGET_INVALID", "路线最多48次、每阶段最多24次。")
        if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or daily_limit < 1:
            reject("SELECTION_BUDGET_INVALID", "必须明确UTC日次数上限。")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM halt").fetchone():
                reject("SELECTION_HALTED", "实验已停止，不能自行继续。")
            if db.execute("SELECT 1 FROM claims WHERE slot=?", (slot,)).fetchone():
                reject("SELECTION_ALREADY_CLAIMED", "该槽位已经开始或预留，禁止重发。")
            if db.execute("SELECT count(*) FROM claims").fetchone()[0] >= total_limit:
                reject("SELECTION_CALL_LIMIT", "实验总次数已达，未发送。")
            if db.execute("SELECT count(*) FROM claims WHERE stage=?", (stage,)).fetchone()[0] >= stage_limit:
                reject("SELECTION_STAGE_LIMIT", "本阶段次数已达，未发送。")
            if (
                db.execute(
                    "SELECT count(*) FROM claims WHERE provider=? AND day=?", (provider, at[:10])
                ).fetchone()[0]
                >= daily_limit
            ):
                reject("SELECTION_DAY_LIMIT", "本渠道UTC日次数已达，未发送。")
            if cost_limit is not None:
                amounts = db.execute("SELECT reserved FROM claims WHERE currency=?", (currency,)).fetchall()
                if reserved is None or not currency or any(a[0] is None for a in amounts):
                    reject("SELECTION_PRICE_UNKNOWN", "费用未知，不能检查金额预留。")
                if sum((Decimal(a[0]) for a in amounts), Decimal(0)) + Decimal(reserved) > Decimal(
                    cost_limit
                ):
                    reject("SELECTION_COST_LIMIT", "超过该币种的实验估算金额上限。")
            db.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?, ?,NULL)",
                (slot, stage, provider, at[:10], reserved, currency, "outcome_unknown", at),
            )

    def finish(self, slot, result):
        with self.transaction() as db:
            row = db.execute("SELECT result FROM claims WHERE slot=?", (slot,)).fetchone()
            if row is None or row[0] is not None:
                reject("SELECTION_RESULT_EXISTS", "槽位缺失或已有结果，不可重写。")
            db.execute(
                "UPDATE claims SET state=?,result=? WHERE slot=?",
                (result["state"], json.dumps(result, ensure_ascii=False), slot),
            )
            if result["state"] != "succeeded":
                db.execute(
                    "INSERT OR IGNORE INTO halt VALUES (1,?)", (result.get("error_code") or result["state"],)
                )

    def halt(self, reason):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO halt VALUES (1,?)", (reason,))

    def status(self):
        with self.transaction() as db:
            rows = db.execute("SELECT slot,state FROM claims ORDER BY slot").fetchall()
            halt = db.execute("SELECT reason FROM halt WHERE id=1").fetchone()
        return {"attempts_reserved": len(rows), "slots": dict(rows), "halt": halt[0] if halt else None}
