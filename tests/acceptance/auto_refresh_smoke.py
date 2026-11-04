"""Explicit candidate check against an already configured read-only real terminal."""

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True


def tree(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    app, data = args.app_root.resolve(), args.data_root.resolve()
    before = tree(app)
    spec = importlib.util.spec_from_file_location("candidate_launcher", app / "tooling/launch/manage.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    sys.path.insert(0, str(app / "bridge/src"))
    from xau_mt5_bridge.windows import running_terminals

    terminals = running_terminals()
    assert terminals
    output = data / "validation" / ("auto-refresh-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    startup_at = datetime.now(UTC)
    state = launcher.launch(app_root=app, data_root=data, source="mt5", open_browser=False)
    samples = []
    result = {
        "source": "mt5",
        "manual_refresh_calls": 0,
        "application_trading_operations": 0,
        "sample_count": 0,
    }
    verified = False
    try:
        with httpx.Client(
            base_url=state["api_url"], headers={"Authorization": "Bearer " + state["token"]}, timeout=30
        ) as client:
            deadline = time.monotonic() + 55
            while time.monotonic() < deadline:
                response = client.get("/v1/workspace")
                response.raise_for_status()
                w = response.json()
                market, portfolio = w["market"], w["portfolio"]
                assert w["fact_refresh"]["enabled"]
                if (
                    market
                    and market["source_kind"] == "mt5"
                    and market["quote_status"]["state"] == "available"
                    and datetime.fromisoformat(market["captured_at"]) >= startup_at
                ) and (not samples or market["snapshot_id"] != samples[-1]["market_id"]):
                    samples.append(
                        {
                            "market_id": market["snapshot_id"],
                            "portfolio_id": portfolio["snapshot_id"],
                            "captured_at": market["captured_at"],
                            "account_scope_id": market["account_scope_id"],
                            "orders_status": portfolio["orders_status"]["state"],
                            "trades_status": portfolio["trades_status"]["state"],
                        }
                    )
                    (output / f"workspace-{len(samples)}.json").write_text(
                        json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                if len(samples) >= 3:
                    break
                time.sleep(0.5)
            assert len(samples) >= 3, "No three autonomous captures within the bounded observation"
            assert len({s["portfolio_id"] for s in samples}) == len(samples)
            assert len({s["account_scope_id"] for s in samples}) == 1
            assert all(s["orders_status"] == s["trades_status"] == "available" for s in samples)
            assert all(a["captured_at"] < b["captured_at"] for a, b in itertools.pairwise(samples))
            # Manual refresh remains immediately available, including an in-flight reuse.
            command = {"include_history": False, "history_from": None, "history_to": None}
            response = client.post(
                "/v1/market/refresh", json=command, headers={"Idempotency-Key": uuid4().hex}
            )
            response.raise_for_status()
            result["manual_refresh_calls"] = 1
            job_id = response.json()["job_id"]
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                job = client.get("/v1/jobs/" + job_id).json()
                if job["state"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.2)
            assert job["state"] == "succeeded", job.get("error")
            ui = subprocess.run(
                ["node", str(ROOT / "tests/e2e/auto-refresh-ui.cjs")],
                input=json.dumps(
                    {"web_url": state["web_url"], "token": state["token"], "output": str(output)}
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=50,
                check=False,
            )
            if ui.returncode:
                (output / "ui-failure.txt").write_text(ui.stderr, encoding="utf-8")
                raise AssertionError("Browser automatic refresh check failed; see local evidence")
            result["ui"] = json.loads(ui.stdout)
            result.update(sample_count=len(samples), samples=samples, manual_refresh_succeeded=True)
            last_before_restart = client.get("/v1/workspace").json()["market"]["snapshot_id"]
    finally:
        launcher.stop(app_root=app, data_root=data)
    # Restart with no manual collection, verify preserved history and a fresh startup capture.
    state = launcher.launch(app_root=app, data_root=data, source="mt5", open_browser=args.keep_running)
    try:
        with httpx.Client(
            base_url=state["api_url"], headers={"Authorization": "Bearer " + state["token"]}, timeout=30
        ) as client:
            original = client.get("/v1/market/snapshots/" + samples[0]["market_id"])
            original.raise_for_status()
            assert original.json()["snapshot_id"] == samples[0]["market_id"]
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                w = client.get("/v1/workspace").json()
                if (
                    w["market"]
                    and w["market"]["snapshot_id"] != last_before_restart
                    and w["fact_refresh"]["latest_job"]["state"] == "succeeded"
                ):
                    break
                time.sleep(0.2)
            assert w["market"]["snapshot_id"] != last_before_restart
            assert w["fact_refresh"]["latest_job"]["state"] == "succeeded"
            assert running_terminals() == terminals
            assert tree(app) == before
            verified = True
            result.update(
                restart_auto_capture=True,
                history_preserved=True,
                terminal_processes_unchanged=True,
                app_root_unchanged=True,
                restart_ready_seconds=state["ready_seconds"],
                candidate=json.loads((app / "release-manifest.json").read_text(encoding="utf-8"))["build_id"],
            )
    finally:
        if not (verified and args.keep_running):
            launcher.stop(app_root=app, data_root=data)
    result.update(
        status="PASS", left_running=bool(verified and args.keep_running), eight_hour_stability="NOT_RUN"
    )
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "evidence": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    main()
