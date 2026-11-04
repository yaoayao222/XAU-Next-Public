"""Use the interactive Windows desktop for a host-independent CLI launch.

No service, scheduled task or elevation is used. Explorer's compatibility Job
can allow breakaway; the receiver must then be outside *all* Jobs before launch.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path


def in_windows_job(pid=None):
    if os.name != "nt":
        return False
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsProcessInJob.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        contained = wintypes.BOOL()
        if not kernel.IsProcessInJob(process, None, ctypes.byref(contained)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(contained.value)
    finally:
        if pid is not None:
            kernel.CloseHandle(process)


def shell_execute(executable, arguments, directory):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("缺少 Windows PowerShell，无法通过桌面启动。请在资源管理器双击 Start.cmd。")
    # Encode data, then decode it in a fixed script: no user path is PowerShell code.
    payload = base64.b64encode(json.dumps({
        "executable": str(executable), "arguments": subprocess.list2cmdline(arguments),
        "directory": str(directory),
    }).encode("utf-8")).decode("ascii")
    script = (
        "$ErrorActionPreference='Stop'; "
        "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "'))|ConvertFrom-Json; "
        "$shell=New-Object -ComObject Shell.Application; $hwnd=0; "
        "$desktop=$shell.Windows().FindWindowSW(0,0,8,[ref]$hwnd,1); "
        "if($null -eq $desktop){throw 'Windows desktop unavailable'}; "
        "$desktop.Document.Application.ShellExecute($p.executable,$p.arguments,$p.directory,'open',0)"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )
    if result.returncode:
        raise RuntimeError("Windows 桌面启动失败。请在资源管理器双击 Start.cmd；未回退到宿主内启动。")


def relay_start(app_root, data_root, args, *, timeout=75):
    receipt = data_root / "runtime" / ("desktop-launch-" + secrets.token_hex(12) + ".json")
    command = [str(app_root / "tooling/launch/manage.py"), args.action,
               "--data-root", str(data_root), "--api-port", str(args.api_port),
               "--web-port", str(args.web_port), "--desktop-receipt", str(receipt)]
    source = args.source or os.environ.get("XAU_NEXT_SOURCE")
    if source:
        command += ["--source", source]
    if args.no_browser:
        command += ["--no-browser"]
    # Windows venv redirectors create their own child-lifetime Job. This stdlib-only
    # launcher uses base Python; API/bridge still use their installed environments.
    executable = getattr(sys, "_base_executable", sys.executable)
    shell_execute(Path(executable).resolve(), command, app_root)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if receipt.exists():
            result = json.loads(receipt.read_text(encoding="utf-8"))
            if not result["ok"]:
                raise RuntimeError(result["detail"])
            return result
        time.sleep(0.1)
    raise RuntimeError("桌面启动结果尚未确认；不要自动重试。请查看 DATA_ROOT/runtime/launcher.json 和 logs。")
