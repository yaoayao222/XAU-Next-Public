"""Only the real deterministic review calculation runs here; no DB or bridge is opened."""

import argparse
import json
import time
from pathlib import Path

from xau_next.domains.review.public import compute_prediction


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--stats", required=True)
    args = parser.parse_args()
    value = json.loads(Path(args.input).read_text(encoding="utf-8"))
    started, count = time.perf_counter(), 0
    while not Path(args.stop_file).exists() and time.perf_counter() - started < 12 * 3600:
        result = compute_prediction(
            value["version"], value["market"], horizon_minutes=value["horizon_minutes"], as_of=value["as_of"]
        )
        if result.to_dict()["review"]["outcome"] != "EVALUATED":
            raise AssertionError("基准纯复盘样例未得到确定结果")
        count += 1
    Path(args.stats).write_text(
        json.dumps(
            {
                "completed_calculations": count,
                "elapsed_seconds": time.perf_counter() - started,
                "business_database_writes": 0,
                "external_calls": 0,
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
