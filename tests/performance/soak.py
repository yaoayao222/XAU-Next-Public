"""Run manually for eight hours, or explicitly label a shorter resource probe.

Example: python tests/performance/soak.py --data-root D:/XAU-Next-Data/performance/.../data
No automation is scheduled. Only a newly launched benchmark API is stopped.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from common import APIServer, freeze_runtime, machine, process_resources, summarize, write_json


def measure_resources(server, output: Path, *, duration_seconds=28800, interval_seconds=5) -> dict:
    samples, errors, latencies = [], [], []
    started, next_sample = time.perf_counter(), time.perf_counter()
    endpoints = ["/v1/workspace", "/v1/forecasts?limit=50", "/v1/reviews?revisions=all&limit=50"]
    path = output / "resource-samples.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        while time.perf_counter() - started < duration_seconds:
            route = endpoints[len(samples) % len(endpoints)]
            request_started = time.perf_counter()
            response = server.client.get(route)
            latency = (time.perf_counter() - request_started) * 1000
            latencies.append(latency)
            if response.status_code != 200:
                errors.append({"route": route, "status": response.status_code, "body": response.text[:500]})
            sample = {
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "route": route,
                "status": response.status_code,
                "latency_ms": round(latency, 3),
                **process_resources(server.api_pid),
            }
            samples.append(sample)
            handle.write(json.dumps(sample) + "\n")
            handle.flush()
            next_sample += interval_seconds
            time.sleep(
                max(
                    0,
                    min(next_sample - time.perf_counter(), started + duration_seconds - time.perf_counter()),
                )
            )
    metrics = {}
    for name in ("working_set_bytes", "private_bytes", "handles", "cpu_seconds"):
        values = [sample[name] for sample in samples if name in sample]
        if values:
            metrics[name] = {
                "first": values[0],
                "last": values[-1],
                "min": min(values),
                "max": max(values),
                "delta": values[-1] - values[0],
            }
    elapsed = time.perf_counter() - started
    result = {
        "requested_duration_seconds": duration_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "sample_count": len(samples),
        "interval_seconds": interval_seconds,
        "http_error_count": len(errors),
        "http_error_rate": len(errors) / len(samples) if samples else None,
        "latency": summarize(latencies),
        "resources": metrics,
        "raw_samples": samples,
        "errors": errors,
        "eight_hour_observation_completed": elapsed >= 28800,
        "stability_verdict": "requires_resource_curve_review"
        if elapsed >= 28800
        else "SHORT_PROBE_ONLY_NOT_EIGHT_HOUR_ACCEPTANCE",
        "limitations": [
            "Short observations do not establish absence of long-term growth.",
            "This loop exercises authenticated workspace/history HTTP with the persistent worker disabled; external MT5/model waits are excluded.",
        ],
    }
    write_json(output / "soak-summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description="明确合成 DATA_ROOT 的 8 小时本地资源观察")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--duration-seconds", type=float, default=28800)
    parser.add_argument("--interval-seconds", type=float, default=5)
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("duration 和 interval 必须大于零")
    manifest = json.loads((args.data_root / "dataset-manifest.json").read_text(encoding="utf-8"))
    if manifest["kind"] != "synthetic-performance-fixture":
        raise ValueError("只能使用专用合成性能基准 DATA_ROOT")
    output = (args.output_root or args.data_root.parent / ("soak-" + uuid4().hex)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    inventory = freeze_runtime(output / "runtime")
    with APIServer(args.data_root, output / "runtime", output) as server:
        result = measure_resources(
            server, output, duration_seconds=args.duration_seconds, interval_seconds=args.interval_seconds
        )
    result.update(machine=machine(), code=inventory, data_root=str(args.data_root), output_root=str(output))
    write_json(output / "soak-summary.json", result)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"raw_samples", "code"}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
