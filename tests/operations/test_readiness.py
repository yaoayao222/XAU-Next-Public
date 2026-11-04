"""A slow local workspace response should finish once within the startup deadline."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tooling.launch import manage


def test_readiness_allows_slow_workspace_without_duplicate_requests():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            time.sleep(1.2)
            body = json.dumps({'ready': True}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = manage._wait_ready(f'http://127.0.0.1:{server.server_port}/v1/workspace',
                                  SimpleNamespace(poll=lambda: None), timeout=2.0)
        assert result == {'ready': True}
        assert requests == ['/v1/workspace']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_readiness_request_does_not_exceed_remaining_startup_budget(monkeypatch):
    budgets = []
    clock = iter([10.0, 10.1, 11.8, 12.1])
    monkeypatch.setattr(manage.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(manage.time, 'sleep', lambda _: None)

    def failing(request, *, timeout):
        budgets.append(timeout)
        raise TimeoutError('SYNTHETIC slow local service')

    monkeypatch.setattr(manage.urllib.request, 'urlopen', failing)
    with pytest.raises(manage.LaunchError, match='启动等待超时'):
        manage._wait_ready('http://127.0.0.1:1/v1/workspace', SimpleNamespace(poll=lambda: None), timeout=2.0)
    assert len(budgets) == 1
    assert 0 < budgets[0] <= 0.21
