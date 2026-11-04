"""Native process discovery and a no-child-process guard for SDK attachment."""

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from .protocol import BridgeFailure

_job_handle = None


class ProcessEntry(ctypes.Structure):
    # POINTER caches its pointee type for the lifetime of the interpreter.
    # Defining this inside running_terminals retained a new type on every scan.
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def running_terminals() -> list[dict]:
    if os.name != "nt":
        raise BridgeFailure("WINDOWS_REQUIRED", "真实 MT5 只读接入需要 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise BridgeFailure("PROCESS_READ_FAILED", "无法枚举当前已运行终端")
    result = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() == "terminal64.exe":
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        buffer, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                            result.append(
                                {"pid": int(entry.th32ProcessID), "path": str(Path(buffer.value).resolve())}
                            )
                    finally:
                        kernel.CloseHandle(handle)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return result


def prevent_child_processes() -> None:
    global _job_handle
    if _job_handle is not None:
        return
    if os.name != "nt":
        raise BridgeFailure("ATTACH_GUARD_UNAVAILABLE", "此环境不支持防止自动启动终端的保护")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = BasicLimits()
    limits.LimitFlags, limits.ActiveProcessLimit = 0x00000008, 1
    if (
        not handle
        or not kernel.SetInformationJobObject(handle, 2, ctypes.byref(limits), ctypes.sizeof(limits))
        or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess())
    ):
        error = ctypes.get_last_error()
        if handle:
            kernel.CloseHandle(handle)
        raise BridgeFailure(
            "ATTACH_GUARD_UNAVAILABLE", f"终端防启动保护无法建立（系统错误 {error}），未连接 SDK"
        )
    _job_handle = handle
