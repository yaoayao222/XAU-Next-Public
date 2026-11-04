"""Explicit read-only environment probe. Raw evidence stays in an external DATA_ROOT."""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/mt5_bridge/src"))

from xau_mt5_bridge.windows import running_terminals
from xau_next.domains.market.public import MarketService
from xau_next.domains.portfolio.public import PortfolioService
from xau_next.platform.db import Database
from xau_next.shared.values import parse_utc, utc_now


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--broker-timezone")
    args = parser.parse_args()
    root = Path(args.data_root).resolve() / ("run-" + uuid4().hex)
    if root.is_relative_to(ROOT):
        raise ValueError("真实原始证据必须位于仓库外 DATA_ROOT")
    db = Database(root)
    db.initialize()
    before = running_terminals()
    if len(before) != 1:
        raise RuntimeError("只读验收需要唯一已运行 terminal64.exe，未启动其它终端")
    settings = {
        "values": {
            "symbol": "XAUUSD",
            "quote_max_age_ms": 30000,
            "connection": {
                "terminal_path": before[0]["path"],
                "account_scope_id": None,
                "broker_timezone": args.broker_timezone,
            },
        }
    }
    market = MarketService(db, mode="mt5")
    report = {
        "kind": "real-read-only-smoke",
        "captured_at": utc_now(),
        "data_root": str(root),
        "synthetic_fallback": False,
        "trading_actions": 0,
    }
    report["broker_timezone"] = args.broker_timezone
    try:
        identity = market.identity(settings).to_dict()
        settings["values"]["connection"]["account_scope_id"] = identity["account_scope_id"]
        capture = market.capture(
            {"include_history": False, "history_from": None, "history_to": None}, settings
        )
        portfolio = PortfolioService(db)
        with db.write() as tx:
            stored = market.commit_capture(capture, tx)
            portfolio.sync_facts(capture.portfolio, trades=capture.deals, uow=tx)
        snapshot = stored.to_dict()
        report.update(
            {
                "identity_bound": snapshot["account_scope_id"] == identity["account_scope_id"],
                "sdk_version": identity["sdk_version"],
                "child_processes_blocked": identity["child_processes_blocked"],
                "snapshot_id": snapshot["snapshot_id"],
                "quote_state": snapshot["quote_status"]["state"],
                "quote_age_ms": (
                    parse_utc(snapshot["captured_at"]) - parse_utc(snapshot["quote"]["event_time"])
                ).total_seconds()
                * 1000
                if snapshot["quote"]
                else None,
                "contract_state": snapshot["contract_status"]["state"],
                "calculation_mode": (snapshot["contract"] or {}).get("calculation_mode"),
                "series": [
                    {
                        "timeframe": s["timeframe"],
                        "count": len(s["bars"]),
                        "price_basis": s["price_basis"],
                        "state": s["status"]["state"],
                        "closed_count": sum(b["closed"] for b in s["bars"]),
                        "last_closed_at": next(
                            (b["close_at"] for b in reversed(s["bars"]) if b["closed"]), None
                        ),
                    }
                    for s in snapshot["series"]
                ],
                "account_state": capture.portfolio.to_dict()["account_status"]["state"],
                "orders_state": capture.portfolio.to_dict()["orders_status"]["state"],
            }
        )
        end = parse_utc(utc_now()).replace(second=0, microsecond=0)
        history = market.fetch_history(
            {
                "from": (end - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                "to": end.isoformat().replace("+00:00", "Z"),
            },
            settings,
        ).to_dict()
        (root / "history-evidence.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
        report["history"] = {
            "bar_count": len(history["bars"]),
            "tick_count": len(history["ticks"]),
            "status": history["status"],
            "tick_status": history.get("tick_status"),
            "bar_status": history.get("bar_status"),
            "coverage": history.get("coverage"),
        }
        if snapshot["quote"] and snapshot["contract"]:
            quote, contract = snapshot["quote"], snapshot["contract"]
            from decimal import Decimal

            command = {
                "symbol": "XAUUSD",
                "side": "long",
                "volume_lots": contract["volume_min"],
                "entry_price": quote["ask"],
                "exit_price": str(Decimal(quote["ask"]) + Decimal(contract["tick_size"])),
            }
            for operation in ("profit", "margin"):
                result = market.calculation(operation, command, settings).to_dict()
                (root / (operation + "-evidence.json")).write_text(json.dumps(result), encoding="utf-8")
                report[operation + "_read"] = "available"
        market.close()
        db.close()
        reopened = Database(root)
        reopened.initialize()
        recovered = MarketService(reopened, mode="mt5")
        report["restart_snapshot_equal"] = recovered.latest().to_dict() == snapshot
        recovered.close()
        reopened.close()
        report["status"] = (
            "passed" if report["identity_bound"] and report["restart_snapshot_equal"] else "failed"
        )
    except Exception as exc:  # noqa: BLE001 -- save exact failure locally, print only sanitized status.
        (root / "failure.txt").write_text(repr(exc), encoding="utf-8")
        report["status"] = "blocked"
        report["error_code"] = getattr(exc, "code", type(exc).__name__)
        report["error_detail"] = str(exc)
    finally:
        market.close()
        db.close()
        after = running_terminals()
        report["terminal_processes_unchanged"] = before == after
        (root / "smoke-summary.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
