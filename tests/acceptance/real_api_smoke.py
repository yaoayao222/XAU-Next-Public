"""Explicit real read-only acceptance; never collected as a normal unit test.

Creates one pending real analysis request but never manufactures a model answer,
sends anything to an external model, or executes a broker trade.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # the acceptance harness must also preserve immutable APP_ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, default=ROOT)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument(
        "--data-root", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "XAU-Next" / "data"
    )
    args = parser.parse_args()
    args.app_root = args.app_root.resolve()
    spec = importlib.util.spec_from_file_location("xau_launcher", args.app_root / "tooling/launch/manage.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    output = args.data_root / "validation" / ("real-api-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "source": "real_mt5",
        "model_answer_imported": False,
        "broker_write_calls": 0,
        "app_root": str(args.app_root),
    }
    manifest_path = args.app_root / "release-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result["candidate"] = {key: manifest[key] for key in ("build_id", "source_commit", "source_dirty")}
    state = launcher.launch(
        app_root=args.app_root, data_root=args.data_root, source="mt5", open_browser=False
    )
    try:
        with httpx.Client(
            base_url=state["api_url"], headers={"Authorization": "Bearer " + state["token"]}, timeout=45
        ) as client:

            def put_settings(settings):
                response = client.put(
                    "/v1/settings",
                    json={"expected_revision": settings["revision"], "values": settings["values"]},
                    headers={"Idempotency-Key": uuid4().hex},
                )
                response.raise_for_status()
                return response.json()

            settings = client.get("/v1/settings").json()
            settings["values"]["connection"]["broker_timezone"] = "UTC+03:00"  # explicitly confirmed by user
            settings = put_settings(settings)
            accepted = client.post(
                "/v1/market/refresh",
                json={"include_history": False, "history_from": None, "history_to": None},
                headers={"Idempotency-Key": uuid4().hex},
            )
            accepted.raise_for_status()
            job_id = accepted.json()["job_id"]
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                job = client.get("/v1/jobs/" + job_id).json()
                if job["state"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.1)
            if job["state"] != "succeeded":
                (output / "job.json").write_text(
                    json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                raise RuntimeError("Real read-only refresh did not succeed; local evidence retained")
            workspace = client.get("/v1/workspace").json()
            assert workspace["market"]["source_kind"] == "mt5"
            assert workspace["market"]["quote_status"]["state"] == "available"
            assert workspace["portfolio"]["account_scope_id"] == workspace["market"]["account_scope_id"]
            (output / "workspace.json").write_text(
                json.dumps(workspace, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            settings["values"]["connection"]["account_scope_id"] = workspace["market"]["account_scope_id"]
            put_settings(settings)
            response = client.post(
                "/v1/analysis/requests",
                json={
                    "kind": "new",
                    "market_snapshot_id": workspace["market"]["snapshot_id"],
                    "forecast_id": None,
                    "base_version_id": None,
                    "horizon_minutes": [180, 360, 720],
                },
                headers={"Idempotency-Key": uuid4().hex},
            )
            response.raise_for_status()
            request = response.json()
            packet_hash = hashlib.sha256(request["packet_text"].encode("utf-8")).hexdigest()
            (output / "analysis-package.txt").write_text(request["packet_text"], encoding="utf-8")
            result.update(
                captured_at=workspace["market"]["captured_at"],
                quote_status=workspace["market"]["quote_status"]["state"],
                contract_status=workspace["market"]["contract_status"]["state"],
                orders_status=workspace["portfolio"]["orders_status"]["state"],
                trades_status=(workspace["portfolio"].get("trades_status") or {}).get("state", "unknown"),
                request_id=request["request_id"],
                packet_sha256=packet_hash,
                packet_bytes=len(request["packet_text"].encode("utf-8")),
            )
            ui_input = {
                "api_url": state["api_url"],
                "web_url": state["web_url"],
                "token": state["token"],
                "output": str(output),
            }
            ui = subprocess.run(
                ["node", str(ROOT / "tests/e2e/real-ui.cjs")],
                check=False,
                input=json.dumps(ui_input),
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=45,
                cwd=ROOT,
            )
            if ui.returncode:
                raise RuntimeError("Real UI acceptance failed; inspect local screenshot")
            result["ui"] = json.loads(ui.stdout)
    finally:
        launcher.stop(data_root=args.data_root, app_root=args.app_root)
    # Exercise a real new process and session against the same stored original bytes.
    state = launcher.launch(
        app_root=args.app_root, data_root=args.data_root, source="mt5", open_browser=args.keep_running
    )
    result["restart_launcher_ready_seconds"] = state["ready_seconds"]
    verified = False
    try:
        with httpx.Client(
            base_url=state["api_url"], headers={"Authorization": "Bearer " + state["token"]}, timeout=15
        ) as client:
            restored = client.get("/v1/analysis/requests/" + result["request_id"])
            restored.raise_for_status()
            result["request_restored_after_process_restart"] = (
                hashlib.sha256(restored.json()["packet_text"].encode("utf-8")).hexdigest()
                == result["packet_sha256"]
            )
            assert result["request_restored_after_process_restart"]
            verified = True
    finally:
        if not (args.keep_running and verified):
            launcher.stop(data_root=args.data_root, app_root=args.app_root)
    result["left_running"] = bool(args.keep_running and verified)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "PASS",
                "evidence": str(output),
                "real_model_loop": "NOT_RUN",
                "user_acceptance": "NOT_RUN",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
