"""Dedicated benchmark server: normal API/schema/SQLite, no external-source worker."""

import argparse
import json
import os
from pathlib import Path

import uvicorn
from common import TOKEN
from xau_next.bootstrap import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--contract-root", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()
    app = create_app(
        data_root=args.data_root,
        token=TOKEN,
        source="synthetic",
        contracts_root=args.contract_root,
        start_worker=False,
    )
    Path(args.state_file).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "parent_pid": os.getppid(),
                "application_module": __import__("xau_next.bootstrap", fromlist=["__file__"]).__file__,
            }
        ),
        encoding="utf-8",
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
