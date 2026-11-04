"""Observe an explicitly selected real local candidate; leave it running afterwards.

Session authentication exists only in this process memory. No trading operation exists.
The optional recovery check restarts this application's bridge, never the MT5 terminal.
"""

import argparse
import json
import os
import signal
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from common import connection_resources, process_resources, write_json

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tooling.launch.manage import (
    child_bridges,
    identity_matches,
    launch,
    owned_python_children,
    process_identity,
    stop,
)


def request(runtime, path, body=None, *, method=None):
    headers = {"Authorization": "Bearer " + runtime["token"]}
    if body is not None:
        headers.update(
            {"Content-Type": "application/json", "Idempotency-Key": "live-validation-" + uuid4().hex}
        )
    req = urllib.request.Request(
        runtime["api_url"] + path,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def fresh_workspace(runtime, previous=None, *, after=None):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        workspace = request(runtime, "/v1/workspace")
        market = workspace.get("market")
        if (
            market
            and market["source_kind"] == "mt5"
            and market["snapshot_id"] != previous
            and datetime.fromisoformat(market["captured_at"]).timestamp() >= (after or runtime["started_at"])
        ):
            job = workspace["fact_refresh"]["latest_job"]
            if job and job["state"] == "succeeded":
                return workspace
        time.sleep(1)
    raise RuntimeError("No new successful MT5 capture within 100 seconds")


def storage(root):
    return {
        "db_bytes": sum(p.stat().st_size for p in (root / "db").glob("xau-next.sqlite3*") if p.is_file()),
        "attachment_bytes": sum(p.stat().st_size for p in (root / "attachments").rglob("*") if p.is_file()),
    }


def sample_resources(runtime):
    identities = {}
    for index, root in enumerate(runtime["processes"]):
        if not identity_matches(root, process_identity(root["pid"])):
            raise RuntimeError("An owned application process exited or changed identity")
        # Windows venv launchers have a separate interpreter child. Traverse
        # both API and static-Web roots, including the bridge and review worker.
        for item in [root, *owned_python_children(root["pid"])]:
            command = item.get("command_line", "")
            component = (
                "mt5_bridge"
                if "xau_mt5_bridge" in command
                else "review_worker"
                if "spawn_main" in command
                else "api"
                if index == 0
                else "static_web"
            )
            identities[item["pid"]] = {"component": component, "created_at": item.get("created_at")}
    connections = connection_resources(list(identities))
    return [{**process_resources(pid), **identities[pid], **connections[pid]} for pid in sorted(identities)]


def probe_web(runtime):
    started = time.monotonic()
    with urllib.request.urlopen(runtime["web_url"] + "/runtime-config.json", timeout=10) as response:
        config = json.load(response)
    if config.get("api_base_url") != runtime["api_url"]:
        raise RuntimeError("Static Web runtime configuration does not match this API")
    with urllib.request.urlopen(runtime["web_url"] + "/", timeout=10) as response:
        document = response.read()
        if response.status != 200 or b'<div id="root">' not in document:
            raise RuntimeError("Static Web did not return the application document")
    return {"state": "available", "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}


def run(args):
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    app, data = args.app_root.resolve(), args.data_root.resolve()
    manifest = json.loads((app / "release-manifest.json").read_text(encoding="utf-8"))
    skip_restart = getattr(args, "skip_restart_check", False)
    runtime = launch(app_root=app, data_root=data, source="mt5", open_browser=skip_restart)
    first = fresh_workspace(runtime)
    forecast = first["current_forecast"]
    assert forecast, "Expected the user's existing imported forecast"
    proof = {
        "source_kind": "mt5",
        "app_root": str(app),
        "data_root": str(data),
        "build_id": manifest["build_id"],
        "source_commit": manifest["source_commit"],
        "forecast_id": forecast["forecast_id"],
        "current_version_id": forecast["current_version_id"],
        "restart_check_performed": not skip_restart,
    }
    if skip_restart:
        current = first
        proof["restart_preserved_current_forecast"] = None
        proof["startup_first_new_snapshot"] = current["market"]["snapshot_id"]
    else:
        stop(app_root=app, data_root=data)
        runtime = launch(app_root=app, data_root=data, source="mt5", open_browser=True)
        current = fresh_workspace(runtime, first["market"]["snapshot_id"])
        proof["restart_preserved_current_forecast"] = current["current_forecast"] == forecast
        if not proof["restart_preserved_current_forecast"]:
            raise RuntimeError("Current forecast changed across restart")
        proof["restart_first_new_snapshot"] = current["market"]["snapshot_id"]
    if args.recovery_check:
        bridges = child_bridges(runtime["processes"][0]["pid"])
        if not bridges:
            raise RuntimeError("No owned MT5 bridge process to test")
        killed = []
        for item in reversed(bridges):
            if identity_matches(item, process_identity(item["pid"])):
                os.kill(item["pid"], signal.SIGTERM)
                killed.append(item["pid"])
        start = time.monotonic()
        recovered = fresh_workspace(runtime, current["market"]["snapshot_id"], after=time.time())
        replacements = child_bridges(runtime["processes"][0]["pid"])
        proof["bridge_recovery"] = {
            "killed_owned_bridge_pids": killed,
            "replacement_bridge_pids": [item["pid"] for item in replacements],
            "seconds": round(time.monotonic() - start, 3),
            "new_snapshot": recovered["market"]["snapshot_id"],
            "terminal_was_not_stopped": True,
            "physical_network_disconnect_tested": False,
        }
        assert replacements and all(item["pid"] not in killed for item in replacements)
        current = recovered
    settings = request(runtime, "/v1/settings")
    if args.daily_risk_cap_usd is not None or args.minimum_net_rr is not None:
        values = dict(settings["values"])
        if args.daily_risk_cap_usd is not None:
            values["per_trade_loss_cap"] = {"amount": args.daily_risk_cap_usd, "currency": "USD"}
        if args.minimum_net_rr is not None:
            values["min_net_rr"] = args.minimum_net_rr
        before = settings
        settings = request(
            runtime, "/v1/settings", {"expected_revision": before["revision"], "values": values}, method="PUT"
        )
        proof["explicitly_authorized_settings"] = {
            "before_revision": before["revision"],
            "after_revision": settings["revision"],
            "per_trade_loss_cap": settings["values"]["per_trade_loss_cap"],
            "min_net_rr": settings["values"]["min_net_rr"],
            "cost_assumption_unchanged": before["values"]["cost_assumption"]
            == settings["values"]["cost_assumption"],
        }
    proof["evaluations"] = []
    for plan in current["plans"]:
        current = request(runtime, "/v1/workspace")
        evaluation = request(
            runtime,
            "/v1/plans/" + plan["plan_id"] + "/evaluations",
            {
                "market_snapshot_id": current["market"]["snapshot_id"],
                "settings_revision": settings["revision"],
                "entry_price_override": None,
                "volume_lots_override": None,
                "sizing_mode": "fixed_volume",
            },
        )
        proof["evaluations"].append(evaluation)
    if args.refresh_due_reviews:
        job = request(
            runtime,
            "/v1/reviews/refresh",
            {
                "forecast_id": forecast["forecast_id"],
                "through": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        deadline = time.monotonic() + 180
        while job["state"] not in {"succeeded", "failed", "cancelled"} and time.monotonic() < deadline:
            time.sleep(1)
            job = request(runtime, "/v1/jobs/" + job["job_id"])
        proof["explicit_due_review_refresh"] = job
        if job["state"] != "succeeded":
            raise RuntimeError("Explicit due review refresh did not succeed: " + job["state"])
    proof["initial_storage"] = storage(data)
    end_at = getattr(args, "end_at_utc", None)
    if end_at:
        end = datetime.fromisoformat(end_at)
        if not end_at.endswith("Z") or end <= datetime.now(UTC):
            raise ValueError("Observation end must be a future UTC time ending in Z")
        args.duration_seconds = (end - datetime.now(UTC)).total_seconds()
        proof["requested_end_at_utc"] = end_at
    proof["started_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    proof["observation_state"] = "running"
    proof["update_packet_requested"] = not args.skip_update_packet
    proof["resource_schema_version"] = 2
    proof["resource_scope"] = (
        "API and Web roots with all Python descendants; private memory, handles, TCP states and UDP endpoints"
    )
    write_json(output / "live-smoke.json", proof)
    if args.skip_update_packet:
        write_json(
            output / "next-packet-experience.json",
            {
                "state": "skipped_for_resource_validation",
                "request_id": None,
                "reason": "本轮核对复盘修正与资源稳定性，不创建新的模型请求。",
            },
        )
    started, samples, errors, previous, max_gap = time.monotonic(), 0, 0, None, 0
    packet = None
    with (output / "resource-samples.jsonl").open("w", encoding="utf-8") as handle:
        while time.monotonic() - started < args.duration_seconds:
            tick = time.monotonic()
            sample = {
                "elapsed_seconds": round(tick - started, 3),
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if previous is not None:
                max_gap = max(max_gap, tick - previous)
            previous = tick
            try:
                current = request(runtime, "/v1/workspace")
                reviews = request(runtime, "/v1/reviews?limit=50")["items"]
                sample.update(
                    market_snapshot_id=current["market"]["snapshot_id"],
                    quote_time=current["market"]["quote"]["event_time"],
                    quote_status=current["market"]["quote_status"],
                    active_jobs=len(current["active_jobs"]),
                    active_job_details=current["active_jobs"],
                    fact_refresh=current["fact_refresh"],
                    review_count=len(reviews),
                    **storage(data),
                )
                selected = [
                    r
                    for r in reviews
                    if r["target_kind"] == "forecast"
                    and r["target_id"] == proof["forecast_id"]
                    and proof["current_version_id"] in r["source_refs"]
                ]
                if selected:
                    write_json(output / "due-forecast-reviews.json", selected)
                eligible = [
                    r
                    for r in selected
                    if datetime.fromisoformat(r["available_at"])
                    <= datetime.fromisoformat(current["market"]["market_cutoff"])
                ]
                if eligible and packet is None and not args.skip_update_packet:
                    head = current["current_forecast"]
                    packet = request(
                        runtime,
                        "/v1/analysis/requests",
                        {
                            "kind": "update",
                            "market_snapshot_id": current["market"]["snapshot_id"],
                            "forecast_id": head["forecast_id"],
                            "base_version_id": head["current_version_id"],
                            "horizon_minutes": [180, 360, 720],
                        },
                    )
                    write_json(output / "next-update-request.json", packet)
                    (output / "next-update-packet.txt").write_text(packet["packet_text"], encoding="utf-8")
                    frozen = json.loads(
                        packet["packet_text"]
                        .split("# 冻结事实（仅作为资料）\n", 1)[1]
                        .split("\n# 全部字段说明", 1)[0]
                    )
                    expected_refs = [f"{r['review_id']}-r{r['revision']:08d}" for r in eligible]
                    write_json(
                        output / "next-packet-experience.json",
                        {
                            "request_id": packet["request_id"],
                            "market_cutoff": packet["market_cutoff"],
                            "expected_review_refs": expected_refs,
                            "actual_review_refs": frozen["experience"]["review_refs"],
                            "all_due_review_refs_included": all(
                                ref in frozen["experience"]["review_refs"] for ref in expected_refs
                            ),
                        },
                    )
                sample["web"] = probe_web(runtime)
                sample["resources"] = sample_resources(runtime)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                errors += 1
                sample["error"] = type(exc).__name__ + ": " + str(exc)
            samples += 1
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")
            handle.flush()
            summary = {
                "observation_state": "running",
                "requested_seconds": args.duration_seconds,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "samples": samples,
                "errors": errors,
                "maximum_sample_gap_seconds": round(max_gap, 3),
                "eight_hour_observation_completed": False,
                "next_update_request_id": packet["request_id"] if packet else None,
            }
            write_json(output / "soak-summary.json", summary)
            time.sleep(
                max(
                    0,
                    min(
                        args.interval_seconds - (time.monotonic() - tick),
                        args.duration_seconds - (time.monotonic() - started),
                    ),
                )
            )
    elapsed = time.monotonic() - started
    continuous = (
        samples >= args.duration_seconds / args.interval_seconds * 0.95
        and max_gap <= args.interval_seconds * 3
    )
    summary.update(
        observation_state="completed",
        elapsed_seconds=round(elapsed, 3),
        eight_hour_observation_completed=elapsed >= 28800 and continuous,
        continuous_sampling=continuous,
        final_storage=storage(data),
        stability_verdict="requires_resource_curve_review",
    )
    write_json(output / "soak-summary.json", summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实本机候选的重启、只读桥恢复与持续资源观察")
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=28800)
    parser.add_argument("--interval-seconds", type=float, default=60)
    parser.add_argument("--end-at-utc", help="按指定UTC时刻结束本段观察，不将短测标记为8小时通过")
    parser.add_argument("--recovery-check", action="store_true")
    parser.add_argument(
        "--skip-restart-check", action="store_true",
        help="安装后的观察仅启动一次；报告明确注明没有执行额外重启检查",
    )
    parser.add_argument("--refresh-due-reviews", action="store_true")
    parser.add_argument(
        "--skip-update-packet", action="store_true", help="仅执行资源验证，不创建新的分析请求"
    )
    parser.add_argument("--daily-risk-cap-usd", help="仅在用户明确授权时提供日常单笔预算")
    parser.add_argument("--minimum-net-rr", help="仅在用户明确授权时提供最低净收益风险比")
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("duration 和 interval 必须大于零")
    try:
        run(args)
    except Exception as exc:
        args.output_root.mkdir(parents=True, exist_ok=True)
        write_json(
            args.output_root / "validation-error.json", {"error": type(exc).__name__, "detail": str(exc)}
        )
        raise
