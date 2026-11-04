"""Isolated production-dist browser feedback measurement, with real local API."""

import argparse
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from common import ROOT, TOKEN, APIServer, sha256, write_json


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.data_root / "dataset-manifest.json").read_text(encoding="utf-8"))
    if manifest["kind"] != "synthetic-performance-fixture":
        raise ValueError("浏览器性能验收只使用专用合成数据")
    args.output.mkdir(parents=True, exist_ok=False)
    original = ROOT / "apps/web/dist"
    before = {p.relative_to(original).as_posix(): sha256(p) for p in original.rglob("*") if p.is_file()}
    dist = args.output / "web-dist"
    shutil.copytree(original, dist)
    after = {p.relative_to(dist).as_posix(): sha256(p) for p in dist.rglob("*") if p.is_file()}
    if before != after:
        raise RuntimeError("当前 Web dist 在复制时变化，不能使用混合版本")
    api_base = {"value": None}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/runtime-config.json":
                body = json.dumps({"api_base_url": api_base["value"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not (dist / self.path.split("?")[0].lstrip("/")).is_file():
                self.path = "/index.html"
            super().do_GET()

        def log_message(self, *_):
            pass

    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(dist)))
    web_base = f"http://127.0.0.1:{web.server_port}"
    thread = threading.Thread(target=web.serve_forever, daemon=True)
    thread.start()
    try:
        with APIServer(
            args.data_root,
            args.runtime,
            args.output / "api",
            extra_environment={"XAU_NEXT_WEB_ORIGINS": web_base},
        ) as api:
            api_base["value"] = f"http://127.0.0.1:{api.port}"
            command = [
                "node",
                str(ROOT / "tests/performance/browser_metrics.mjs"),
                "--web",
                web_base,
                "--api",
                api_base["value"],
                "--token",
                TOKEN,
                "--output",
                str(args.output / "browser-results.json"),
                "--screenshot",
                str(args.output / "workspace.png"),
            ]
            result = subprocess.run(
                command,
                check=False,
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            (args.output / "browser-process.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError("浏览器测量未完成，见 browser-process.log")
            measured = json.loads((args.output / "browser-results.json").read_text(encoding="utf-8"))
            measured.update(
                web_dist_sha256=after,
                harness_sha256={
                    path.name: sha256(path)
                    for path in (Path(__file__), ROOT / "tests/performance/browser_metrics.mjs")
                },
                api_runtime=str(args.runtime),
                data_root=str(args.data_root),
                api_pid=api.api_pid,
                screenshot=str(args.output / "workspace.png"),
            )
            write_json(args.output / "browser-results.json", measured)
            print(result.stdout)
    finally:
        web.shutdown()
        web.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
