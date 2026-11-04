"""Run AC-22's fixed synthetic API workload and preserve every measured sample."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from common import ROOT, APIServer, CPULoad, freeze_runtime, machine, process_resources, summarize, write_json
from seed import seed
from soak import measure_resources


def request_sample(client, route: str, *, index: int, profile: str, job=False) -> dict:
    started = time.perf_counter()
    if job:
        response = client.post(
            route,
            json={"include_history": False, "history_from": None, "history_to": None},
            headers={"Idempotency-Key": f"{profile}-{index}-{uuid4().hex}"},
        )
    else:
        response = client.get(route)
    elapsed = (time.perf_counter() - started) * 1000
    body = response.json()
    expected = 202 if job else 200
    result = {
        "route": route,
        "elapsed_ms": round(elapsed, 3),
        "status": response.status_code,
        "response_bytes": len(response.content),
        "success": response.status_code == expected,
    }
    if response.status_code != expected:
        result["error"] = response.text[:1000]
        return result
    if "items" in body:
        result["item_count"] = len(body["items"])
        result["success"] = result["success"] and len(body["items"]) == 50
    if job:
        # Timing ends when durable HTTP acceptance returns. Read-back and cleanup happen outside that sample.
        check = client.get("/v1/jobs/" + body["job_id"])
        result["durable_readback"] = check.status_code == 200 and check.json()["job_id"] == body["job_id"]
        cancelled = client.post(
            "/v1/jobs/" + body["job_id"] + "/cancel",
            json=None,
            headers={"Idempotency-Key": "cancel-" + uuid4().hex},
        )
        result["queue_cleanup_status"] = cancelled.status_code
        result["success"] = result["success"] and result["durable_readback"] and cancelled.status_code == 202
    return result


def profile(server, *, name: str, samples: int, warmups: int, versions: int, selected_metrics=None) -> dict:
    middle = versions // 2
    endpoints = {
        "workspace": (["/v1/workspace"], 300, False),
        "forecasts_50": (
            ["/v1/forecasts?limit=50", f"/v1/forecasts?limit=50&cursor=perf-forecast-{middle:05d}"],
            500,
            False,
        ),
        "review_latest_50": (
            [
                "/v1/reviews?revisions=latest&limit=50",
                f"/v1/reviews?revisions=latest&limit=50&cursor=perf-review-{middle:05d}",
            ],
            500,
            False,
        ),
        "review_revisions_50": (
            [
                "/v1/reviews?revisions=all&limit=50",
                f"/v1/reviews?revisions=all&limit=50&cursor=perf-review-{middle:05d}-r00000005",
            ],
            500,
            False,
        ),
        "persistent_job_accept": (["/v1/market/refresh"], 300, True),
    }
    result = {}
    for metric, (routes, target, job) in endpoints.items():
        if selected_metrics and metric not in selected_metrics:
            continue
        for index in range(warmups):
            request_sample(
                server.client, routes[index % len(routes)], index=index, profile=name + "-warm", job=job
            )
        raw = [
            request_sample(server.client, routes[index % len(routes)], index=index, profile=name, job=job)
            for index in range(samples)
        ]
        stats = summarize([row["elapsed_ms"] for row in raw])
        errors = sum(not row["success"] for row in raw)
        result[metric] = {
            **stats,
            "p95_target_ms": target,
            "error_count": errors,
            "error_rate": errors / samples,
            "target_met": errors == 0 and stats["p95_ms"] <= target,
            "raw_samples": raw,
        }
        print(
            json.dumps(
                {
                    "profile": name,
                    "metric": metric,
                    **{k: v for k, v in result[metric].items() if k != "raw_samples"},
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-root", type=Path, default=Path("D:/XAU-Next-Data/performance"))
    parser.add_argument("--existing-data-root", type=Path)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--short-soak-seconds", type=float, default=60)
    parser.add_argument("--report", type=Path, default=ROOT / "docs/performance-results.json")
    parser.add_argument(
        "--metrics",
        nargs="+",
        choices=(
            "workspace",
            "forecasts_50",
            "review_latest_50",
            "review_revisions_50",
            "persistent_job_accept",
        ),
    )
    args = parser.parse_args()
    if args.samples < 20:
        parser.error("正式基准每组至少 20 个样本")
    output = args.base_root.resolve() / ("run-" + uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    data_root = args.existing_data_root.resolve() if args.existing_data_root else output / "data"
    if args.existing_data_root:
        dataset = json.loads((data_root / "dataset-manifest.json").read_text(encoding="utf-8"))
        if dataset["kind"] != "synthetic-performance-fixture":
            raise ValueError("不能把真实候选数据用于此性能基准")
    else:
        dataset = seed(data_root)
    code = freeze_runtime(output / "runtime")
    code["git_head_at_freeze"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    write_json(output / "code-inventory.json", code)
    result = {
        "kind": "XAU-Next-AC22-synthetic-performance",
        "machine": machine(),
        "code": code,
        "dataset_manifest": str(data_root / "dataset-manifest.json"),
        "data_root": str(data_root),
        "output_root": str(output),
        "scale": {
            key: dataset[key]
            for key in (
                "expected_versions",
                "review_heads",
                "review_revisions",
                "archived_m1_bars",
                "current_workspace_series_counts",
            )
        },
        "measurement": {
            "transport": "real localhost TCP HTTP, single fresh Uvicorn process, authenticated requests",
            "samples_per_metric": args.samples,
            "selected_metrics": args.metrics
            or [
                "workspace",
                "forecasts_50",
                "review_latest_50",
                "review_revisions_50",
                "persistent_job_accept",
            ],
            "warmups_per_metric": args.warmups,
            "quantile": "nearest-rank",
            "timing": "client request through receipt of complete body; JSON inspection outside timed interval",
            "external_wait": "MT5, model calls, and persistent worker execution disabled; their latencies are not folded into local API figures",
            "cpu_load": "one isolated process repeatedly runs real compute_prediction on a 12h synthetic M1 window, with no business DB writes",
            "cold_start": "new Python/API process launch to first successful full workspace response; OS filesystem cache was not flushed",
        },
        "profiles": {},
        "cold_workspace_ms": [],
        "limitations": list(dataset["limitations"]),
    }
    for index in range(2):
        with APIServer(data_root, output / "runtime", output / f"cold-{index}") as server:
            result["cold_workspace_ms"].append(server.cold_workspace_ms)
    with APIServer(data_root, output / "runtime", output / "measured") as server:
        result["cold_workspace_ms"].append(server.cold_workspace_ms)
        result["api_port"] = server.port
        result["api_pid"] = server.api_pid
        result["launcher_pid"] = server.process.pid
        result["api_process_identity"] = json.loads(
            (output / "measured/server-state.json").read_text(encoding="utf-8")
        )
        result["resource_measurement_valid"] = True
        result["resource_before_profiles"] = process_resources(server.api_pid)
        result["profiles"]["baseline"] = profile(
            server,
            name="baseline",
            samples=args.samples,
            warmups=args.warmups,
            versions=dataset["expected_versions"],
            selected_metrics=args.metrics,
        )
        write_json(output / "intermediate-results.json", result)
        with CPULoad(output / "runtime", data_root / "cpu-input.json", output / "cpu-load"):
            result["profiles"]["background_review_cpu"] = profile(
                server,
                name="background-review",
                samples=args.samples,
                warmups=args.warmups,
                versions=dataset["expected_versions"],
                selected_metrics=args.metrics,
            )
        result["cpu_load_evidence"] = json.loads(
            (output / "cpu-load/cpu-stats.json").read_text(encoding="utf-8")
        )
        result["short_resource_probe"] = measure_resources(
            server, output / "measured", duration_seconds=args.short_soak_seconds, interval_seconds=2
        )
        result["resource_after_profiles"] = process_resources(server.api_pid)
    result["cold_start_summary"] = {
        **summarize(result["cold_workspace_ms"]),
        "target_ms": 5000,
        "target_met": max(result["cold_workspace_ms"]) <= 5000,
    }
    result["eight_hour_status"] = "NOT_RUN"
    result["eight_hour_command"] = (
        f'.venv\\Scripts\\python.exe tests/performance/soak.py --data-root "{data_root}" --duration-seconds 28800 --interval-seconds 5'
    )
    result["limitations"].extend(
        [
            "8-hour stability is not established by this short resource observation.",
            "Browser click p95 is not measured by an HTTP benchmark.",
        ]
    )
    result["all_measured_api_targets_met"] = all(
        metric["target_met"] for metrics in result["profiles"].values() for metric in metrics.values()
    )
    write_json(output / "results.json", result)
    prior = json.loads(args.report.read_text(encoding="utf-8")) if args.report.exists() else {"runs": []}
    for previous in prior["runs"]:
        if "api_pid" not in previous and "short_resource_probe" in previous:
            previous["resource_measurement_valid"] = False
            previous["resource_invalidated_reason"] = (
                "Original helper sampled the Windows venv launcher Popen PID, not its actual Python API child. "
                "Original numbers and external raw files are preserved; they are not API memory/stability evidence. "
                "HTTP timing samples remain valid. Use the later self-reported API PID probe."
            )
    prior["runs"].append(result)
    prior["latest_run"] = str(output / "results.json")
    # A later run must never keep a derived summary pointing to older measurements.
    prior.pop("current_measurement_summary", None)
    write_json(args.report, prior)
    print(
        json.dumps(
            {
                "result": str(output / "results.json"),
                "report": str(args.report),
                "api_targets_met": result["all_measured_api_targets_met"],
                "cold_start": result["cold_start_summary"],
                "eight_hour_status": "NOT_RUN",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
