from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id, parse_utc, utc_now

OPERATIONS = frozenset({"health", "identity", "snapshot", "history", "profit", "margin"})


class BridgeError(DomainError):
    def __init__(self, code: str, detail: str, *, retryable=True):
        super().__init__(code, detail, 503)
        self.retryable = retryable


class BridgeClient:
    """One serial SDK channel; a timeout only terminates this client's own subprocess."""

    def __init__(
        self,
        command: list[str],
        *,
        timeout: float = 20,
        environment: dict | None = None,
        error_log: Path | None = None,
    ):
        self.command, self.timeout, self.environment = list(command), timeout, environment
        self._process = None
        self._lock = threading.Lock()
        self._responses = None
        self._error_log, self._stderr = error_log, None

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            if self._error_log:
                self._error_log.parent.mkdir(parents=True, exist_ok=True)
                self._stderr = self._error_log.open("ab")
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr or subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=flags,
                env=self.environment,
            )
        except OSError as exc:
            self.close()
            raise BridgeError(
                "BRIDGE_START_FAILED", "无法启动只读桥，请检查独立 Python 路径和桥环境"
            ) from exc
        self._responses = queue.Queue(maxsize=2)
        process, responses = self._process, self._responses

        def read():
            try:
                for line in process.stdout:
                    if len(line) > 32 * 1024 * 1024:
                        responses.put_nowait(
                            BridgeError("BRIDGE_RESPONSE_TOO_LARGE", "读取结果超过协议大小限制")
                        )
                        return
                    responses.put(line, timeout=1)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                try:
                    responses.put_nowait(None)
                except queue.Full:
                    pass

        threading.Thread(target=read, name="xau-bridge-reader", daemon=True).start()

    def request(self, operation: str, params: dict) -> dict:
        if operation not in OPERATIONS:
            raise BridgeError("OPERATION_NOT_ALLOWED", "仅允许定义的只读操作", retryable=False)
        with self._lock:
            self._start()
            request_id = new_id()
            deadline = (
                (parse_utc(utc_now()) + timedelta(seconds=self.timeout)).isoformat().replace("+00:00", "Z")
            )
            request = {
                "protocol_version": "1",
                "request_id": request_id,
                "operation": operation,
                "params": params,
                "deadline": deadline,
            }
            try:
                self._process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
                line = self._responses.get(timeout=self.timeout)
                if line is None:
                    raise BridgeError("BRIDGE_EXITED", "只读桥已退出，请重试刷新")
                if isinstance(line, Exception):
                    raise line
                response = json.loads(line)
                if response.get("request_id") != request_id or response.get("protocol_version") != "1":
                    raise BridgeError("BRIDGE_PROTOCOL_ERROR", "只读桥响应身份不匹配")
                if response.get("error"):
                    error = response["error"]
                    raise BridgeError(error["code"], error["detail"], retryable=error["retryable"])
                if not isinstance(response.get("result"), dict):
                    raise BridgeError("BRIDGE_PROTOCOL_ERROR", "只读桥未返回有效结果")
                return response
            except queue.Empty as exc:
                self.close()
                raise BridgeError("BRIDGE_TIMEOUT", "只读读取超时，桥接进程已停止，可重新刷新") from exc
            except (OSError, ValueError, BridgeError) as exc:
                self.close()
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError("BRIDGE_PROTOCOL_ERROR", "桥接通信失败，请重新刷新") from exc

    def close(self):
        process, self._process = self._process, None
        if process is None:
            if self._stderr:
                self._stderr.close()
                self._stderr = None
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout):
            if stream:
                stream.close()
        if self._stderr:
            self._stderr.close()
            self._stderr = None
