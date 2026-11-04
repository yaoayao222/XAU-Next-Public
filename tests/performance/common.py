"""Reproducible local-only performance helpers; no application implementation changes."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from ctypes import wintypes
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "synthetic-performance-isolated-data-only"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "count": len(samples),
        "p50_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.50) - 1)], 3),
        "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3),
        "min_ms": round(min(ordered), 3),
        "max_ms": round(max(ordered), 3),
        "mean_ms": round(sum(ordered) / len(ordered), 3),
    }


def code_inventory(root: Path) -> dict:
    paths = []
    for prefix in ("apps/api/src", "apps/api/migrations", "contracts"):
        paths.extend(path for path in (root / prefix).rglob("*") if path.suffix in {".py", ".json"})
    files = {path.relative_to(root).as_posix(): sha256(path) for path in sorted(paths)}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"content_sha256": digest, "files": files}


def freeze_runtime(destination: Path) -> dict:
    if destination.exists():
        raise ValueError("性能运行快照目标已存在，禁止覆盖")
    before = code_inventory(ROOT)
    for name in ("apps/api/src", "apps/api/migrations", "contracts"):
        shutil.copytree(
            ROOT / name, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    copied = code_inventory(destination)
    after = code_inventory(ROOT)
    if before["content_sha256"] != after["content_sha256"] or copied != before:
        raise RuntimeError("复制期间源代码发生变化，请以新目录重新冻结，不使用混合快照")
    return copied


def machine() -> dict:
    result = {
        "platform": platform.platform(),
        "python": sys.version,
        "logical_cpus": os.cpu_count(),
        "processor": platform.processor(),
        "performance_clock_resolution_seconds": time.get_clock_info("perf_counter").resolution,
        "concurrent_desktop_work": "Codex development and other local processes were not stopped; this is not a dedicated laboratory host.",
    }
    if os.name == "nt":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            result["processor_name"] = winreg.QueryValueEx(key, "ProcessorNameString")[0]

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "physical_total",
                    "physical_available",
                    "page_total",
                    "page_available",
                    "virtual_total",
                    "virtual_available",
                    "extended_available",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        result["physical_memory_bytes"] = status.physical_total
    return result


class MemoryCounters(ctypes.Structure):
    # ctypes pointer types retain their pointee. Define this once, not per probe.
    _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t)
        for name in (
            "peak_working_set",
            "working_set",
            "peak_paged_pool",
            "paged_pool",
            "peak_nonpaged_pool",
            "nonpaged_pool",
            "pagefile",
            "peak_pagefile",
            "private_bytes",
        )
    ]


def connection_resources(pids: list[int]) -> dict[int, dict]:
    """Counts only: no remote addresses, ports, command lines or credentials."""
    if os.name != "nt":
        raise RuntimeError("TCP/UDP resource verification requires Windows")
    selected = sorted({int(pid) for pid in pids})
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $ErrorActionPreference='Stop'; "
        "$selected=@(" + ",".join(map(str, selected)) + "); "
        "$tcp=@(Get-NetTCPConnection | Where-Object {$_.OwningProcess -in $selected} | "
        "Select-Object OwningProcess,@{Name='State';Expression={$_.State.ToString()}}); "
        "$udp=@(Get-NetUDPEndpoint | Where-Object {$_.OwningProcess -in $selected} | "
        "Select-Object OwningProcess); "
        "[ordered]@{tcp=$tcp;udp=$udp} | ConvertTo-Json -Depth 4 -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode:
        raise RuntimeError("Windows TCP/UDP connection probe failed: " + completed.stderr[-500:])
    tables = json.loads(completed.stdout)
    result = {pid: {"tcp_states": {}, "tcp_connections": 0, "udp_endpoints": 0} for pid in selected}
    for row in tables["tcp"]:
        value = result[int(row["OwningProcess"])]
        state = row["State"]
        value["tcp_states"][state] = value["tcp_states"].get(state, 0) + 1
        value["tcp_connections"] += 1
    for row in tables["udp"]:
        result[int(row["OwningProcess"])]["udp_endpoints"] += 1
    return result


def process_resources(pid: int) -> dict:
    if os.name != "nt":
        return {"pid": pid, "unsupported_resource_probe": platform.system()}
    kernel, psapi = (
        ctypes.WinDLL("kernel32", use_last_error=True),
        ctypes.WinDLL("psapi", use_last_error=True),
    )
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    process = kernel.OpenProcess(0x1000 | 0x10, False, pid)
    if not process:
        raise OSError(ctypes.get_last_error(), "无法读取本次 API 进程资源")
    try:
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handles = wintypes.DWORD()
        if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), ctypes.sizeof(counters)):
            raise OSError(ctypes.get_last_error(), "内存读取失败")
        kernel.GetProcessHandleCount(process, ctypes.byref(handles))
        created, exited, kernel_time, user_time = [wintypes.FILETIME() for _ in range(4)]
        kernel.GetProcessTimes(
            process,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        cpu_ticks = ((kernel_time.dwHighDateTime << 32) | kernel_time.dwLowDateTime) + (
            (user_time.dwHighDateTime << 32) | user_time.dwLowDateTime
        )
        return {
            "pid": pid,
            "working_set_bytes": counters.working_set,
            "private_bytes": counters.private_bytes,
            "handles": handles.value,
            "cpu_seconds": cpu_ticks / 10_000_000,
        }
    finally:
        kernel.CloseHandle(process)


class APIServer(AbstractContextManager):
    def __init__(self, data_root: Path, runtime: Path, output: Path, *, extra_environment=None):
        self.data_root, self.runtime, self.output = data_root, runtime, output
        self.process = None
        self.client = None
        self.log = None
        self.api_pid = None
        self.extra_environment = extra_environment or {}

    def __enter__(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.output.mkdir(parents=True, exist_ok=True)
        self.log = (self.output / "api.log").open("ab")
        env = {
            **os.environ,
            "PYTHONPATH": str(self.runtime / "apps/api/src"),
            "PYTHONIOENCODING": "utf-8",
            **self.extra_environment,
        }
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", timeout=30, headers={"Authorization": "Bearer " + TOKEN}
        )
        started = time.perf_counter()
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/performance/server.py"),
                "--data-root",
                str(self.data_root),
                "--contract-root",
                str(self.runtime / "contracts"),
                "--port",
                str(self.port),
                "--state-file",
                str(self.output / "server-state.json"),
            ],
            env=env,
            stdout=self.log,
            stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        while time.perf_counter() - started < 45:
            if self.process.poll() is not None:
                raise RuntimeError(f"独立 API 提前退出，见 {self.output / 'api.log'}")
            try:
                response = self.client.get("/v1/workspace")
                if response.status_code == 200:
                    self.cold_workspace_ms = (time.perf_counter() - started) * 1000
                    self.initial_workspace_bytes = len(response.content)
                    self.api_pid = json.loads(
                        (self.output / "server-state.json").read_text(encoding="utf-8")
                    )["pid"]
                    return self
                self.startup_error = response.text[:1000]
            except httpx.TransportError:
                pass
            time.sleep(0.02)
        self.__exit__(None, None, None)
        raise RuntimeError("API 45 秒内未达到历史工作台可读状态")

    def __exit__(self, *_):
        if self.client:
            self.client.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()


class CPULoad(AbstractContextManager):
    def __init__(self, runtime: Path, input_path: Path, output: Path):
        self.runtime, self.input_path, self.output = runtime, input_path, output

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.stop = self.output / "cpu-stop"
        self.log = (self.output / "cpu.log").open("ab")
        env = {**os.environ, "PYTHONPATH": str(self.runtime / "apps/api/src"), "PYTHONIOENCODING": "utf-8"}
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "tests/performance/cpu_load.py"),
                "--input",
                str(self.input_path),
                "--stop-file",
                str(self.stop),
                "--stats",
                str(self.output / "cpu-stats.json"),
            ],
            env=env,
            stdout=self.log,
            stderr=self.log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        time.sleep(0.4)
        if self.process.poll() is not None:
            raise RuntimeError("纯复盘 CPU 进程提前退出")
        return self

    def __exit__(self, *_):
        self.stop.write_text("stop", encoding="ascii")
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.log.close()
