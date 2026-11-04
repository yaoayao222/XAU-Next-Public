"""Windows lifecycle checks use isolated SYNTHETIC data, never the daily service."""
import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from tests.operations.test_operations import ROOT, request_json
from tooling.launch import manage
from tooling.launch.desktop import in_windows_job


def test_independence_failure_precedes_data_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(manage, "in_windows_job", lambda *args: True)
    root = tmp_path / "untouched"
    with pytest.raises(manage.LaunchError, match="仍受宿主管理"):
        manage.launch(data_root=root, require_independent=True)
    assert not root.exists()


def test_desktop_receiver_rechecks_before_restart_stops_services(monkeypatch, tmp_path):
    receipt = tmp_path / "data/runtime/desktop-launch-test.json"
    monkeypatch.setattr(sys, "argv", ["manage.py", "restart", "--data-root", str(tmp_path / "data"),
                                     "--desktop-receipt", str(receipt), "--desktop-breakaway"])
    monkeypatch.setattr(manage, "in_windows_job", lambda *args: True)
    def unexpected_stop(**kwargs):
        pytest.fail("An unverified desktop receiver must not stop the existing service")
    monkeypatch.setattr(manage, "stop", unexpected_stop)
    assert manage.main() == 1
    result = json.loads(receipt.read_text(encoding="utf-8"))
    assert result["ok"] is False and "未执行启停" in result["detail"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job and desktop shell required")
def test_services_survive_caller_job_termination(tmp_path, record_property):
    app_root = Path(os.environ.get("XAU_NEXT_TEST_CANDIDATE", ROOT)).resolve()
    root = tmp_path / "synthetic data 中文"
    launcher = app_root / "tooling/launch/manage.py"
    if app_root != ROOT:
        setup = subprocess.run([sys.executable, str(launcher), "setup", "--data-root", str(root)],
                               capture_output=True, timeout=120, check=False)
        assert setup.returncode == 0, setup.stderr
    trigger, returned = tmp_path / "go", tmp_path / "returned.json"
    caller = tmp_path / "caller.py"
    caller.write_text(
        "import json,subprocess,time\nfrom pathlib import Path\n"
        f"trigger=Path({str(trigger)!r})\n"
        "while not trigger.exists(): time.sleep(0.05)\n"
        f"r=subprocess.run({[sys.executable, str(launcher), 'start', '--data-root', str(root), '--source', 'synthetic', '--no-browser']!r}, capture_output=True)\n"
        f"Path({str(returned)!r}).write_text(json.dumps({{'code':r.returncode,'output':r.stdout.decode('utf-8'),'error':r.stderr.decode('utf-8')}}),encoding='utf-8')\n"
        "while True: time.sleep(1)\n", encoding="utf-8",
    )
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    assert job
    host = subprocess.Popen([sys._base_executable, str(caller)], creationflags=subprocess.CREATE_NO_WINDOW)
    process = kernel.OpenProcess(0x0100 | 0x0001, False, host.pid)
    runtime = None
    try:
        assert kernel.AssignProcessToJobObject(job, process), ctypes.get_last_error()
        trigger.touch()
        deadline = time.monotonic() + 90
        while not returned.exists() and time.monotonic() < deadline:
            assert host.poll() is None
            time.sleep(0.1)
        assert returned.exists(), "Caller did not receive a launch result"
        result = json.loads(returned.read_text(encoding="utf-8"))
        assert result["code"] == 0, result
        runtime = json.loads((root / "runtime/launcher.json").read_text(encoding="utf-8"))
        assert runtime["lifetime"]["launcher_in_job"] is False
        assert runtime["lifetime"]["independent_required"] is True
        def capture_count():
            with sqlite3.connect(f"{(root / 'db/xau-next.sqlite3').as_uri()}?mode=ro", uri=True) as db:
                return db.execute("SELECT count(*) FROM documents WHERE owner='market' AND kind='snapshot'").fetchone()[0]
        before = capture_count()
        assert kernel.TerminateJobObject(job, 99)
        host.wait(timeout=10)
        assert host.returncode == 99
        # Both launcher and forcibly terminated caller are gone; services remain.
        deadline = time.monotonic() + 5
        while manage.process_identity(runtime["lifetime"]["launcher_pid"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert manage.process_identity(runtime["lifetime"]["launcher_pid"]) is None
        for item in runtime["processes"]:
            assert manage.identity_matches(item, manage.process_identity(item["pid"]))
            assert not in_windows_job(item["pid"])
        for _ in range(2):
            assert request_json(runtime["api_url"] + "/v1/health")["database_ready"]
            assert request_json(runtime["web_url"] + "/runtime-config.json")["api_base_url"] == runtime["api_url"]
            bridges = manage.child_bridges(runtime["processes"][0]["pid"])
            assert bridges
            time.sleep(3)
        deadline = time.monotonic() + 12
        while capture_count() <= before and time.monotonic() < deadline:
            time.sleep(0.2)
        assert capture_count() > before, "Bridge process exists but automatic capture stopped after caller exit"
        record_property("source_kind", "SYNTHETIC")
        record_property("candidate", str(app_root))
        record_property("launcher_in_job", runtime["lifetime"]["launcher_in_job"])
        record_property("caller_job_terminated", True)
        record_property("captures_before_caller_termination", before)
        record_property("captures_after_caller_termination", capture_count())
        assert "token" not in json.dumps(runtime)
        for receipt in (root / "runtime").glob("desktop-launch-*.json"):
            assert "token" not in receipt.read_text(encoding="utf-8")
    finally:
        # Only this test's Job and separately identity-checked service tree.
        kernel.TerminateJobObject(job, 99)
        host.wait(timeout=10)
        kernel.CloseHandle(process)
        kernel.CloseHandle(job)
        manage.stop(app_root=app_root, data_root=root)
    if runtime:
        for item in runtime["processes"]:
            assert manage.process_identity(item["pid"]) is None
        for item in bridges:
            assert manage.process_identity(item["pid"]) is None
        record_property("owned_services_and_bridge_stopped", True)
