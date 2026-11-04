"""Independent loopback static server. Runtime config is generated in memory."""
import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def make_handler(directory, config):
    root = Path(directory).resolve()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/runtime-config.json":
                payload = json.dumps(config, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            target = (root / unquote(path).lstrip("/")).resolve()
            if not target.is_relative_to(root):
                self.send_error(403)
                return
            if not target.is_file() and not Path(path).suffix:
                self.path = "/index.html"
            super().do_GET()

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            super().end_headers()

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--contract-version", required=True)
    args = parser.parse_args()
    handler = make_handler(args.directory, {"api_base_url": args.api_base_url, "contract_version": args.contract_version})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
