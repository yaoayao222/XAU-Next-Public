"""Measure durable growth through real services using an isolated synthetic source.

Run with the repository Python and --output PATH. No real terminal is opened.
The 8-hour numbers are explicitly projections from the measured 100-cycle workload.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sqlite3
import sys
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from xau_next.domains.market.public import MarketService
from xau_next.domains.plans.public import PlansService
from xau_next.domains.portfolio.public import PortfolioService
from xau_next.domains.settings.public import SettingsService
from xau_next.platform.db import Database
from xau_next.shared.values import parse_utc, utc_now

START = "2026-10-12T02:00:00Z"
SERIES_COUNTS = {"H1": 240, "M15": 400, "M5": 400, "M1": 1440}
COMMAND = {"include_history": False, "history_from": None, "history_to": None}


class Clock:
    elapsed = 0

    def __call__(self):
        return (parse_utc(START) + timedelta(seconds=self.elapsed)).isoformat().replace("+00:00", "Z")


def inventory():
    prefixes = [
        "apps/api/src/xau_next/domains/market",
        "apps/api/src/xau_next/domains/plans",
        "apps/api/src/xau_next/domains/portfolio",
        "apps/api/src/xau_next/domains/settings",
        "apps/api/src/xau_next/platform",
        "apps/api/src/xau_next/shared",
        "apps/api/migrations",
        "apps/mt5_bridge/src",
    ]
    paths = [path for prefix in prefixes for path in (ROOT / prefix).rglob("*.py")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def sizes(root, database):
    files = list((root / "attachments").rglob("*"))
    attachments = sum(path.stat().st_size for path in files if path.is_file())
    with sqlite3.connect(database) as connection:
        # page_count sees committed pages in the WAL as well as the main file.
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
    parts = {}
    for name, suffix in (("database_file", ""), ("wal", "-wal"), ("shm", "-shm")):
        path = Path(str(database) + suffix)
        parts[name + "_bytes"] = path.stat().st_size if path.exists() else 0
    return {
        **parts,
        "database_committed_pages_bytes": pages * page_size,
        "attachment_bytes": attachments,
        "attachment_files": sum(path.is_file() for path in files),
        "durable_database_and_attachment_bytes": pages * page_size + attachments,
        "current_database_files_and_attachment_bytes": sum(parts.values()) + attachments,
    }


def checkpoint(database):
    with sqlite3.connect(database) as connection:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result[0] != 0:
        raise AssertionError(f"Isolated benchmark checkpoint was busy: {result}")


def stats(values):
    values = sorted(values)
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
        "p50": values[math.ceil(len(values) * 0.50) - 1],
        "p95": values[math.ceil(len(values) * 0.95) - 1],
    }


def contains_repeated_market(value):
    if isinstance(value, dict):
        return bool({"series", "bars", "market_snapshot"} & value.keys()) or any(
            contains_repeated_market(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(contains_repeated_market(item) for item in value)
    return False


def run(data_root, cycles):
    data_root.mkdir(parents=True, exist_ok=False)
    before_code = inventory()
    clock = Clock()
    db = Database(data_root, clock=clock)
    db.initialize()
    # Specify the synthetic bridge executable explicitly; environment overrides cannot select MT5.
    bridge = [sys.executable, "-m", "xau_mt5_bridge", "--mode", "synthetic"]
    import os

    old_pythonpath = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(ROOT / "apps/mt5_bridge/src")
    market = MarketService(db, bridge_command=bridge, mode="synthetic", clock=clock)
    if old_pythonpath is None:
        os.environ.pop("PYTHONPATH", None)
    else:
        os.environ["PYTHONPATH"] = old_pythonpath
    plans, portfolio = PlansService(db, clock=clock), PortfolioService(db, clock=clock)
    samples, captured, evaluation_ids = [], {}, []
    try:
        settings_service = SettingsService(db, clock=clock)
        settings_service.initialize()
        settings = settings_service.get_snapshot().to_dict()
        settings["values"].update(
            default_volume_lots="0.01",
            per_trade_loss_cap={"amount": "2", "currency": "USD"},
            cost_assumption={
                "commission_roundtrip_per_lot": "20",
                "slippage_reserve_per_lot": "20",
                "other_cost_per_lot": "0",
                "currency": "USD",
                "valid_from": START,
                "valid_until": None,
                "account_scope_id": "synthetic-account",
                "symbol": "XAUUSD",
                "source_note": "合成存储增长基准费用，不代表真实账户或市场。",
            },
        )
        settings = settings_service.update_settings(
            {"expected_revision": settings["revision"], "values": settings["values"]}
        ).to_dict()
        definition = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text("utf-8"))["plans"][0]
        definition.update(
            valid_from=START,
            entry_deadline="2026-10-12T12:00:00Z",
            exit_deadline="2026-10-12T13:00:00Z",
            rationale="合成存储增长基准方案，不代表真实交易建议。",
        )
        definition["activation"]["price"] = "99.00"
        drafts = [
            {
                "plan_id": f"synthetic-storage-plan-{index}",
                "version_id": "synthetic-storage-version",
                "available_at": START,
                "definition": copy.deepcopy(definition),
            }
            for index in (1, 2)
        ]
        with db.write() as tx:
            plans.register(
                drafts, uow=tx, market_identity={"account_scope_id": "synthetic-account", "symbol": "XAUUSD"}
            )
        checkpoint(db.path)
        baseline = sizes(data_root, db.path)
        previous = baseline
        started = time.perf_counter()
        for index in range(cycles):
            clock.elapsed = index * 5
            cycle_started = time.perf_counter()
            prepared = market.capture(COMMAND, settings)
            value = prepared.market.to_dict()
            counts = {item["timeframe"]: len(item["bars"]) for item in value["series"]}
            assert counts == SERIES_COUNTS, counts
            assert value["source_kind"] == "synthetic"
            captured[value["snapshot_id"]] = hashlib.sha256(
                json.dumps(value, sort_keys=True).encode("utf-8")
            ).hexdigest()
            with db.write() as tx:
                current = market.commit_capture(prepared, tx)
                account = portfolio.sync_facts(prepared.portfolio, trades=prepared.deals, uow=tx)
                for draft in drafts:
                    result = plans.evaluate_plan(
                        draft["plan_id"],
                        {
                            "market_snapshot_id": value["snapshot_id"],
                            "settings_revision": settings["revision"],
                            "entry_price_override": None,
                            "volume_lots_override": None,
                            "sizing_mode": "fixed_volume",
                        },
                        market=current,
                        portfolio=account,
                        settings=settings,
                        uow=tx,
                    ).to_dict()
                    assert result["planned_loss"] is not None and result["net_rr"] is not None
                    evaluation_ids.append(result["evaluation_id"])
            elapsed = time.perf_counter() - cycle_started
            storage = sizes(data_root, db.path)
            samples.append(
                {
                    "cycle": index + 1,
                    "synthetic_captured_at": clock(),
                    "service_elapsed_seconds": elapsed,
                    "storage": storage,
                    "durable_growth_bytes": storage["durable_database_and_attachment_bytes"]
                    - previous["durable_database_and_attachment_bytes"],
                }
            )
            previous = storage
        measured_seconds = time.perf_counter() - started
        payload_sizes = {"market": [], "evaluation": []}
        with db.read() as scope:
            for snapshot_id, digest in captured.items():
                reopened = market.get_snapshot(snapshot_id, scope).to_dict()
                assert (
                    hashlib.sha256(json.dumps(reopened, sort_keys=True).encode("utf-8")).hexdigest() == digest
                )
                record = db.documents("market").get(scope, "snapshot", snapshot_id)
                assert record["storage_format"] in {"market-snapshot/2", "market-snapshot/3"}
                assert not contains_repeated_market(record)
                payload_sizes["market"].append(len(json.dumps(record, ensure_ascii=False).encode("utf-8")))
            for evaluation_id in evaluation_ids:
                record = plans.store.get(scope, "evaluation", evaluation_id)
                assert record["storage_format"] == "plan-evaluation/2"
                assert not contains_repeated_market(record)
                frozen = plans.get_evaluation_inputs(evaluation_id, scope=scope).to_dict()
                reference = frozen["market_snapshot_ref"]
                assert reference["snapshot_id"] in captured
                assert reference["source_kind"] == "synthetic" and reference["revision"] == 1
                assert frozen["market_snapshot_header"]["snapshot_id"] == reference["snapshot_id"]
                payload_sizes["evaluation"].append(
                    len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
                )
        integrity = db.check_integrity()
        with sqlite3.connect(db.path) as connection:
            rows = {
                table: {
                    f"{owner}.{kind}": count
                    for owner, kind, count in connection.execute(
                        f"SELECT owner,kind,COUNT(*) FROM {table} GROUP BY owner,kind"
                    )
                }
                for table in ("documents", "document_revisions")
            }
            rows["attachments"] = connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
            rows["payload_blobs"] = connection.execute("SELECT COUNT(*) FROM payload_blobs").fetchone()[0]
        assert rows["documents"]["market.snapshot"] == cycles
        assert rows["documents"]["plans.evaluation"] == cycles * 2
        assert rows["documents"]["portfolio.snapshot"] == cycles
        market.close()
        db.close()
        checkpoint(db.path)
        final = sizes(data_root, db.path)
        assert final["database_file_bytes"] == final["database_committed_pages_bytes"]
        after_code = inventory()
        assert before_code == after_code, "Source files changed during measurement"
        growth = (
            final["durable_database_and_attachment_bytes"] - baseline["durable_database_and_attachment_bytes"]
        )
        projected_cycles = 8 * 60 * 60 / 5
        return {
            "kind": "synthetic-storage-growth/1",
            "measured_at": utc_now(),
            "data_root": str(data_root),
            "source_kind": "synthetic",
            "capture_count": cycles,
            "evaluations_per_capture": 2,
            "evaluation_count": len(evaluation_ids),
            "bars_per_capture": SERIES_COUNTS,
            "bars_total_per_capture": sum(SERIES_COUNTS.values()),
            "measured_wall_seconds": measured_seconds,
            "simulated_capture_interval_seconds": 5,
            "baseline": baseline,
            "final_checkpointed": final,
            "measured_durable_growth_bytes": growth,
            "measured_mean_bytes_per_cycle": growth / cycles,
            "per_cycle_durable_growth_bytes": stats([item["durable_growth_bytes"] for item in samples]),
            "decoded_record_json_bytes": {key: stats(value) for key, value in payload_sizes.items()},
            "final_rows": rows,
            "checks": {
                "all_market_snapshots_reopened_with_equal_content": len(captured),
                "all_evaluation_references_resolve": len(evaluation_ids),
                "new_market_records_without_inline_series": len(captured),
                "new_evaluation_records_without_inline_market_or_series": len(evaluation_ids),
                "database_integrity": integrity,
                "source_files_unchanged_during_measurement": True,
            },
            "projected_8_hours": {
                "kind": "projected_not_measured",
                "assumed_cycle_interval_seconds": 5,
                "assumed_cycles": projected_cycles,
                "database_and_attachment_growth_bytes": growth / cycles * projected_cycles,
                "database_and_attachment_growth_mib": growth / cycles * projected_cycles / 1024**2,
            },
            "limitations": [
                "Synthetic fixed candles and empty orders/deals; compression differs with real data entropy.",
                "Measured service capture, portfolio persistence and two plan evaluations; excludes HTTP/jobs/events, analysis imports and review workloads.",
                "Eight-hour estimate assumes one completed cycle every 5 seconds; actual scheduling waits after completion.",
                "Durable size uses committed SQLite pages plus physical attachments; WAL/SHM are reported separately and checkpointed only at baseline/end.",
                "No eight-hour soak, live MT5 read, trading action or production data mutation was performed.",
            ],
            "source_sha256": before_code,
            "samples": samples,
        }
    finally:
        market.close()
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--cycles", type=int, default=100)
    args = parser.parse_args()
    if args.cycles < 1 or args.cycles > 500:
        parser.error("--cycles must be between 1 and 500")
    if args.output.exists():
        parser.error("Output exists; choose a new path to preserve the previous measurement")
    data_root = args.data_root or args.output.parent / (args.output.stem + "-synthetic-" + uuid4().hex[:8])
    result = run(data_root.resolve(), args.cycles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"source_sha256", "samples"}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
