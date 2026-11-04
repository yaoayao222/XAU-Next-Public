"""Explicit 12-hour, read-only history completeness and elapsed-time evidence."""

import argparse
import gzip
import json
import sys
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from xau_next.domains.market.public import MarketService
from xau_next.platform.db import Database
from xau_next.shared.values import parse_utc, utc_now


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--broker-timezone", required=True)
    args = parser.parse_args()
    root = Path(args.data_root).resolve() / ("history-12h-" + uuid4().hex)
    if root.is_relative_to(Path(__file__).resolve().parents[3]):
        raise ValueError("真实证据必须保存在仓库外")
    db = Database(root)
    db.initialize()
    market = MarketService(db, mode="mt5")
    settings = {"values": {"symbol": "XAUUSD", "connection": {"broker_timezone": args.broker_timezone}}}
    end = parse_utc(utc_now()).replace(second=0, microsecond=0)
    command = {
        "from": (end - timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
    }
    started = perf_counter()
    try:
        result = market.fetch_history(command, settings).to_dict()
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        (root / "history-evidence.json.gz").write_bytes(gzip.compress(raw, mtime=0))
        summary = {
            "kind": "real-read-only-history-12h",
            "captured_at": utc_now(),
            "data_root": str(root),
            "requested_window": command,
            "broker_timezone": args.broker_timezone,
            "elapsed_seconds": round(perf_counter() - started, 3),
            "bar_count": len(result["bars"]),
            "tick_count": len(result["ticks"]),
            "chunk_count": len(result["raw"].get("parts", [result["raw"]])),
            "tick_status": result["tick_status"],
            "bar_status": result["bar_status"],
            "coverage": result["coverage"],
            "synthetic_fallback": False,
            "trading_actions": 0,
            "nondecreasing_ticks": all(
                a["event_time"] <= b["event_time"] for a, b in zip(result["ticks"], result["ticks"][1:])
            ),
            "source_sequences_preserved_unknown": all(t["source_sequence"] is None for t in result["ticks"]),
        }
        (root / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        market.close()
        db.close()


if __name__ == "__main__":
    main()
