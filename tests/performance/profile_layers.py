"""Measure response production layers on an already frozen synthetic benchmark."""

import argparse
import cProfile
import json
import sys
import time
from pathlib import Path

from common import summarize, write_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.data_root / "dataset-manifest.json").read_text(encoding="utf-8"))
    if manifest["kind"] != "synthetic-performance-fixture":
        raise ValueError("此诊断只允许专用合成基准数据")
    sys.path.insert(0, str(args.runtime / "apps/api/src"))
    from fastapi.responses import JSONResponse
    from xau_next.bootstrap import create_app
    from xau_next.transport.generated import models

    app = create_app(
        data_root=args.data_root,
        contracts_root=args.runtime / "contracts",
        source="synthetic",
        start_worker=False,
    )
    contract = app.state.contracts
    name = contract.api["paths"]["/v1/workspace"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].split("/")[-1]
    samples = {
        "workspace_queries_and_mapping": [],
        "authoritative_response_schema": [],
        "generated_dto": [],
        "json_response_serialization": [],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        for index in range(6):
            t0 = time.perf_counter()
            value = app.state.workflows.workspace()
            t1 = time.perf_counter()
            contract.validate(name, value)
            t2 = time.perf_counter()
            parsed = getattr(models, name).model_validate(value).model_dump(mode="json")
            t3 = time.perf_counter()
            response = JSONResponse(parsed)
            t4 = time.perf_counter()
            if index:
                for key, value_ms in zip(
                    samples, ((t1 - t0) * 1000, (t2 - t1) * 1000, (t3 - t2) * 1000, (t4 - t3) * 1000)
                ):
                    samples[key].append(value_ms)
        profiler = cProfile.Profile()
        profiler.runcall(contract.validate, name, value)
        profiler.dump_stats(args.output / "response-schema.prof")
        result = {
            "kind": "synthetic-workspace-layer-profile",
            "runtime": str(args.runtime),
            "data_root": str(args.data_root),
            "workspace_response_bytes": len(response.body),
            "stages": {key: {**summarize(rows), "raw_ms": rows} for key, rows in samples.items()},
            "note": "One warmup then five layer samples in a separate process; diagnostic samples are not mixed into HTTP p95.",
        }
        write_json(args.output / "layers.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        app.state.workflows.stop()
        app.state.workflows.db.close()


if __name__ == "__main__":
    main()
