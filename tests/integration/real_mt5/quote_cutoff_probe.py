"""Read-only, identity-bound quote-to-packet probe in an isolated DATA_ROOT."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps/mt5_bridge/src"))

from xau_mt5_bridge.windows import running_terminals
from xau_next.domains.analysis.public import AnalysisService
from xau_next.domains.market.public import MarketService
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore
from xau_next.shared.values import parse_utc, utc_now


def read_settings(data_root):
    path = (data_root / "db/xau-next.sqlite3").as_posix()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        source.execute("PRAGMA query_only=ON")
        revision, payload = source.execute(
            "SELECT revision,payload FROM documents WHERE owner='settings' AND kind='settings' AND id='current'"
        ).fetchone()
        if payload.startswith("@xau-payload-v1:"):
            digest = payload.split(":", 1)[1]
            compressed, = source.execute("SELECT compressed FROM payload_blobs WHERE sha256=?", (digest,)).fetchone()
            payload = zlib.decompress(compressed)
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("Source settings payload hash mismatch")
        value = json.loads(payload)
        return {"revision": revision, **value}


def run(output, settings_root, *, bridge_python, bridge_source, samples=30, interval=0.5):
    output, settings_root = Path(output).resolve(), Path(settings_root).resolve()
    if output.is_relative_to(ROOT) or output.is_relative_to(settings_root) or output.exists():
        raise ValueError("Probe requires a new external DATA_ROOT separate from the active application")
    if not 1 <= samples <= 100 or not 0 <= interval <= 10:
        raise ValueError("Probe must use 1 to 100 samples and a bounded interval")
    output.mkdir(parents=True)
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    settings = read_settings(settings_root)
    if not settings["values"]["connection"]["account_scope_id"]:
        raise ValueError("Source identity must be explicitly bound")
    os.environ["XAU_NEXT_BRIDGE_PYTHON"] = str(Path(bridge_python).resolve())
    os.environ["XAU_NEXT_BRIDGE_SOURCE"] = str(Path(bridge_source).resolve())
    (output / "settings-source.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    before = running_terminals()
    db = Database(output)
    db.initialize()
    files = AttachmentStore(db)
    market = MarketService(db, files, mode="mt5")
    schema = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
    analysis = AnalysisService(db, files, schema)
    report = {"kind": "isolated-real-quote-cutoff-probe", "started_at": utc_now(),
              "settings_revision": settings["revision"], "trading_actions": 0,
              "active_data_root_written": False, "synthetic_fallback": False, "samples": []}
    try:
        for index in range(samples):
            started = time.monotonic()
            capture = market.capture({"include_history": False}, settings)
            snapshot = capture.market.to_dict()
            with db.write() as tx:
                market.commit_capture(capture, tx)
            evidence = market.read_evidence(snapshot["snapshot_id"]).to_dict()
            raw = evidence["result"]["raw"]
            request = analysis.create_request(
                {"kind": "new", "market_snapshot_id": snapshot["snapshot_id"], "forecast_id": None,
                 "base_version_id": None, "horizon_minutes": settings["values"]["daily_horizons_minutes"]},
                market=capture.market, portfolio=capture.portfolio, settings=settings,
            ).to_dict()
            packet = json.loads(request["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)[1].split("\n# 全部字段说明", 1)[0])
            quote = snapshot["quote"]
            available = snapshot["quote_status"]["state"] == "available"
            kept = packet["market"]["quote"] == quote
            at_or_before = quote is not None and parse_utc(quote["event_time"]) <= parse_utc(snapshot["market_cutoff"])
            if available and not (at_or_before and kept):
                raise AssertionError("Available quote was future-dated or dropped from the packet")
            selection = raw.get("snapshot_quote_selection")
            report["samples"].append({
                "index": index, "snapshot_id": snapshot["snapshot_id"], "request_id": request["request_id"],
                "market_cutoff": snapshot["market_cutoff"], "quote": quote,
                "quote_status": snapshot["quote_status"], "packet_kept_quote": kept,
                "selected_from_pre_cutoff_history": bool(selection and selection["selected_tick"]),
                "selection": selection, "original_latest_tick_time_msc": (raw.get("tick") or {}).get("time_msc"),
                "raw_attachment": capture.raw_attachment.relative_path,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            })
            if index + 1 < samples:
                time.sleep(interval)
        report["available_quotes"] = sum(s["quote_status"]["state"] == "available" for s in report["samples"])
        report["resolved_future_ticks"] = sum(s["selected_from_pre_cutoff_history"] for s in report["samples"])
        report["status"] = "passed" if report["available_quotes"] == samples and report["resolved_future_ticks"] else "partial"
    except Exception as exc:  # noqa: BLE001 -- keep failure evidence without changing the active application.
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "detail": str(exc)}
    finally:
        market.close()
        db.close()
        report["terminal_processes_unchanged"] = before == running_terminals()
        report["finished_at"] = utc_now()
        if not report["terminal_processes_unchanged"]:
            report["status"] = "failed"
        (output / "quote-cutoff-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--settings-data-root", required=True)
    parser.add_argument("--bridge-python", required=True)
    parser.add_argument("--bridge-source", required=True)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    result = run(args.output, args.settings_data_root, bridge_python=args.bridge_python,
                 bridge_source=args.bridge_source, samples=args.samples)
    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
