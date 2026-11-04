"""Local launcher. Runtime state belongs to DATA_ROOT; no secret is persisted."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop import in_windows_job, relay_start


class LaunchError(RuntimeError):
    pass


def default_data_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "XAU-Next" / "data"


def resolved_data_root(value=None, *, app_root=APP_ROOT):
    root = Path(value or os.environ.get("XAU_NEXT_DATA_ROOT") or default_data_root()).expanduser().resolve()
    if root == app_root.resolve() or root.is_relative_to(app_root.resolve()):
        raise LaunchError("DATA_ROOT 必须位于程序目录之外；请使用默认目录或另一个独立目录。")
    if str(root).startswith(("\\\\", "//")):
        raise LaunchError("DATA_ROOT 必须是本机磁盘目录，不能使用网络共享。")
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("logs", "runtime", "backups"):
        (root / folder).mkdir(exist_ok=True)
    probe = root / "runtime" / ("write-check-" + secrets.token_hex(5))
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise LaunchError("数据目录不可写，请选择当前用户有写入权限的目录。") from exc
    return root


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + secrets.token_hex(4))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def runtime_layout(app_root, data_root, api_python=None):
    app_root = Path(app_root).resolve()
    manifest = read_json(app_root / "release-manifest.json", {})
    installed = read_json(data_root / "runtime/install.json", {})
    if installed and Path(installed["app_root"]).resolve() != app_root:
        installed = {}
    candidate = bool(manifest)
    python = Path(api_python or installed.get("api_python") or app_root / ".venv/Scripts/python.exe")
    bridge_python = Path(installed.get("bridge_python") or app_root / "apps/mt5_bridge/.venv/Scripts/python.exe")
    bridge_source = app_root / ("bridge/src" if candidate else "apps/mt5_bridge/src")
    web = app_root / ("web" if candidate else "apps/web/dist")
    if not python.is_file():
        raise LaunchError("API 运行环境不存在，请先运行 Setup.cmd 完成安装。")
    if not bridge_python.is_file():
        raise LaunchError("只读桥运行环境不存在，请先运行 Setup.cmd 完成安装。")
    if not (web / "index.html").is_file():
        raise LaunchError("前端构建产物缺失，请先运行 Setup.cmd 或完成独立前端构建。")
    contract = read_json(app_root / "contracts/openapi.json")
    if contract is None:
        raise LaunchError("缺少公开契约文件，请使用完整候选目录。")
    version = contract["info"]["version"]
    if manifest and manifest["contract_version"] != version:
        raise LaunchError("程序与契约版本不一致，请重新生成匹配的候选。")
    if candidate:
        web_manifest = read_json(web / "release-manifest.json", {})
        if web_manifest.get("contract_version") != version:
            raise LaunchError("前端与公开契约版本不一致，请重新生成匹配的候选。")
    return {"api_python": str(python.resolve()), "bridge_python": str(bridge_python.resolve()),
            "bridge_source": str(bridge_source.resolve()), "web": str(web.resolve()),
            "contract_version": version, "candidate": candidate,
            "application_version": manifest.get("application_version"),
            "installed": bool(installed) and not api_python}


def choose_port(requested=0, *, excluded=()):
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", requested))
            except OSError as exc:
                raise LaunchError(f"端口 {requested} 已被占用，请换一个端口或省略端口让启动器自动选择。") from exc
            port = listener.getsockname()[1]
        if port not in excluded:
            return port
        if requested:
            raise LaunchError("API 与 Web 不能使用同一个端口。")
    raise LaunchError("未能分配空闲端口，请稍后重试。")


def _powershell_json(command):
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise LaunchError("缺少 PowerShell，无法核验自有进程身份。")
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW,
                            timeout=10, check=False)
    if result.returncode:
        raise LaunchError("无法核验进程身份，请检查当前用户是否可读取本机进程信息。")
    return json.loads(result.stdout.lstrip("\ufeff")) if result.stdout.strip() else None


def process_identity(pid):
    if os.name == "nt":
        # No environment variables are read; the session token is not on the command line.
        command = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                   f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
                   "if($p){[ordered]@{pid=$p.ProcessId; executable=$p.ExecutablePath; "
                   "command_line=$p.CommandLine; created_at=$p.CreationDate.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress}")
        result = _powershell_json(command)
        if result and result.get("executable") and result.get("command_line"):
            return result
        return None
    process = Path(f"/proc/{pid}")
    try:
        return {"pid": pid, "executable": str((process / "exe").resolve()),
                "command_line": (process / "cmdline").read_bytes().decode().replace("\0", " "),
                "created_at": (process / "stat").read_text().rsplit(")", 1)[1].split()[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def identity_matches(saved, actual):
    return bool(actual and all(saved.get(key) == actual.get(key)
                               for key in ("pid", "executable", "command_line", "created_at")))


def owned_python_children(parent_pid):
    if os.name != "nt":
        return []
    command = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
               "$queue=[System.Collections.Generic.Queue[int]]::new(); "
               f"$queue.Enqueue({int(parent_pid)}); $result=@(); "
               "while($queue.Count -gt 0){$parent=$queue.Dequeue(); "
               "$children=@(Get-CimInstance Win32_Process -Filter ('ParentProcessId = '+$parent) | "
               "Where-Object {$_.Name -in @('python.exe','pythonw.exe')}); "
               "foreach($child in $children){$queue.Enqueue($child.ProcessId); "
               "$result += [ordered]@{pid=$child.ProcessId; executable=$child.ExecutablePath; "
               "command_line=$child.CommandLine; created_at=$child.CreationDate.ToUniversalTime().ToString('o')}}}; "
               "ConvertTo-Json -InputObject @($result) -Compress")
    result = _powershell_json(command)
    if not result:
        return []
    return result if isinstance(result, list) else [result]


def child_bridges(api_pid):
    return [item for item in owned_python_children(api_pid) if "-m xau_mt5_bridge" in item["command_line"]]


@contextmanager
def launch_lock(data_root):
    path = data_root / "runtime/launch.lock"
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        previous = read_json(path, {})
        owner = previous.get("owner")
        if owner and not identity_matches(owner, process_identity(owner["pid"])):
            path.unlink()
            handle = path.open("x", encoding="utf-8")
        else:
            raise LaunchError("另一个启动操作尚未完成，请等待该操作结束后重试。") from exc
    try:
        with handle:
            handle.write(json.dumps({"owner": process_identity(os.getpid()), "created_at": time.time()}))
        yield
    finally:
        path.unlink(missing_ok=True)


def _wait_ready(url, process, timeout=20, token=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError("后台进程提前退出，请查看 DATA_ROOT/logs 中本次启动日志。")
        try:
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Use the remaining deadline for a single response. A shorter socket
            # timeout abandons valid cold workspace reads and queues duplicates.
            with urllib.request.urlopen(request, timeout=remaining) as response:
                if response.status == 200:
                    return json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.05)
    raise LaunchError(f"启动等待超时（{url}），请查看数据目录日志并核对端口或依赖。")


def launch(*, app_root=APP_ROOT, data_root=None, api_python=None, source=None, api_port=0, web_port=0,
           open_browser=True, require_independent=False):
    started = time.monotonic()
    caller_in_job = in_windows_job()
    if require_independent and caller_in_job:
        raise LaunchError("启动进程仍受宿主管理；已中止。请从 Windows 资源管理器双击启动入口。")
    app_root = Path(app_root).resolve()
    data_root = resolved_data_root(data_root, app_root=app_root)
    with launch_lock(data_root):
        state_path = data_root / "runtime/launcher.json"
        previous = read_json(state_path, {})
        for item in previous.get("processes", []):
            if identity_matches(item, process_identity(item["pid"])):
                raise LaunchError("工作台已在运行，请使用原浏览器标签；需要新会话时运行 restart。")
        layout = runtime_layout(app_root, data_root, api_python)
        selected_api = choose_port(api_port)
        selected_web = choose_port(web_port, excluded=(selected_api,))
        api_url, web_url = f"http://127.0.0.1:{selected_api}", f"http://127.0.0.1:{selected_web}"
        token = secrets.token_urlsafe(32)
        environment = os.environ.copy()
        environment.update(XAU_NEXT_DATA_ROOT=str(data_root), XAU_NEXT_SESSION_TOKEN=token,
            XAU_NEXT_CONTRACT_ROOT=str(app_root / "contracts"), XAU_NEXT_WEB_ORIGINS=web_url,
            XAU_NEXT_BRIDGE_PYTHON=layout["bridge_python"], XAU_NEXT_BRIDGE_SOURCE=layout["bridge_source"],
            PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        if source:
            if source not in ("mt5", "synthetic"):
                raise LaunchError("来源必须是 mt5 或 synthetic。")
            environment["XAU_NEXT_SOURCE"] = source
        if layout["candidate"] or layout["installed"]:
            environment.pop("PYTHONPATH", None)
        else:
            environment["PYTHONPATH"] = str(app_root / "apps/api/src")
        api_command = [layout["api_python"], "-m", "uvicorn", "xau_next.main:app", "--host", "127.0.0.1",
                       "--port", str(selected_api), "--workers", "1", "--no-access-log"]
        web_command = [layout["api_python"], str(app_root / "tooling/launch/static_server.py"),
                       "--directory", layout["web"], "--port", str(selected_web), "--api-base-url", api_url,
                       "--contract-version", layout["contract_version"]]
        processes, identities = [], []
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
        try:
            for name, command in (("api", api_command), ("web", web_command)):
                child_environment = environment.copy()
                if name == "web":
                    child_environment.pop("XAU_NEXT_SESSION_TOKEN", None)
                with (data_root / "logs" / f"{stamp}-{name}.log").open("ab") as log:
                    child = subprocess.Popen(command, cwd=app_root, env=child_environment,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                             creationflags=NO_WINDOW)
                processes.append(child)
                actual = process_identity(child.pid)
                if actual is None:
                    raise LaunchError("无法确认启动进程身份，已中止本次启动。")
                if require_independent and in_windows_job(child.pid):
                    raise LaunchError("后台服务仍受宿主管理，已中止本次启动。")
                identities.append({**actual, "role": name, "command": command})
            health = _wait_ready(api_url + "/v1/health", processes[0])
            _wait_ready(web_url + "/runtime-config.json", processes[1])
            _wait_ready(api_url + "/v1/workspace", processes[0], token=token)
            if health["contract_version"] != layout["contract_version"]:
                raise LaunchError("API 与前端候选契约版本不匹配。")
            if layout["application_version"] and health["application_version"] != layout["application_version"]:
                raise LaunchError("API 版本与候选清单不匹配，请重新运行 Setup.cmd。")
            state = {"format": "xau-next-launch/1", "app_root": str(app_root), "data_root": str(data_root),
                     "api_url": api_url, "web_url": web_url, "contract_version": layout["contract_version"],
                     "application_version": health["application_version"], "processes": identities,
                     "started_at": time.time(), "ready_seconds": time.monotonic() - started,
                     "source": source or "configured", "bridge_python": layout["bridge_python"]}
            state["lifetime"] = {"launcher_in_job": caller_in_job,
                                 "independent_required": require_independent,
                                 "launcher_pid": os.getpid()}
            write_json(state_path, state)
        except BaseException:
            for child in reversed(processes):
                if child.poll() is None:
                    for descendant in reversed(owned_python_children(child.pid)):
                        if identity_matches(descendant, process_identity(descendant["pid"])):
                            try:
                                os.kill(descendant["pid"], signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
            raise
        if open_browser:
            webbrowser.open(web_url + "/#token=" + token)
        # Only in-memory callers receive the token. CLI prints a separate non-sensitive result.
        return {**state, "token": token}


def stop(*, app_root=APP_ROOT, data_root=None):
    app_root = Path(app_root).resolve()
    data_root = resolved_data_root(data_root, app_root=app_root)
    with launch_lock(data_root):
        path = data_root / "runtime/launcher.json"
        state = read_json(path, {})
        if not state:
            return {"stopped": [], "skipped": [], "state": "not_running"}
        if Path(state["app_root"]).resolve() != app_root:
            raise LaunchError("该数据目录的运行进程属于另一个程序目录，请用原目录的停止入口。")
        stopped, skipped, owned = [], [], []
        for item in state["processes"]:
            actual = process_identity(item["pid"])
            if not identity_matches(item, actual):
                skipped.append(item["pid"])
                continue
            owned.append(item)
            owned.extend(owned_python_children(item["pid"]))
        # Windows venv launchers can spawn a base-Python child. Validate and stop
        # that complete owned Python tree; never traverse into the MT5 terminal.
        for item in reversed(owned):
            if identity_matches(item, process_identity(item["pid"])):
                try:
                    os.kill(item["pid"], signal.SIGTERM)
                    stopped.append(item["pid"])
                except ProcessLookupError:
                    pass
        state["stopped_at"] = time.time()
        state["stop_result"] = {"stopped": stopped, "skipped": skipped}
        write_json(path, state)
        return state["stop_result"]


def main():
    parser = argparse.ArgumentParser(description="XAU Next 本机启动与维护")
    parser.add_argument("action", choices=("start", "stop", "restart", "setup", "backup", "restore"))
    parser.add_argument("--data-root")
    parser.add_argument("--source", choices=("mt5", "synthetic"))
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--web-port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--destination")
    parser.add_argument("--backup-root")
    parser.add_argument("--desktop-receipt", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-breakaway", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    receipt = None
    try:
        if args.desktop_receipt:
            root = resolved_data_root(args.data_root)
            receipt = Path(args.desktop_receipt).resolve()
            if (args.action not in ("start", "restart") or receipt.parent != root / "runtime"
                    or not receipt.name.startswith("desktop-launch-") or receipt.exists()):
                raise LaunchError("桌面启动回执路径不正确或已使用。")
            # Explorer may add an application-compatibility Job with BREAKAWAY_OK.
            # Use its permitted breakaway once, then check ALL Job membership again.
            # This cannot accept the nested kill-on-close Job seen inside the host.
            if in_windows_job():
                if args.desktop_breakaway:
                    raise LaunchError("桌面接收进程仍受宿主管理，未执行启停。请从资源管理器双击入口。")
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--desktop-breakaway"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                )
                # Only the final worker writes success/failure. Do not impose a
                # shim timeout that kills it halfway through starting services.
                return 0
        elif args.action in ("start", "restart") and in_windows_job():
            root = resolved_data_root(args.data_root)
            result = relay_start(APP_ROOT, root, args)
            print("工作台已独立启动：" + result["web_url"] + "；数据目录：" + str(root))
            return 0
        if args.action == "setup":
            from setup_runtime import setup
            result = setup(APP_ROOT, args.data_root)
            print("安装完成，运行 Start.cmd 打开工作台。数据目录：" + result["data_root"])
        elif args.action == "stop":
            result = stop(data_root=args.data_root)
            print(f"停止完成：已停止 {len(result['stopped'])} 个自有进程，未匹配进程 {len(result['skipped'])} 个。")
        elif args.action in ("start", "restart"):
            if args.action == "restart":
                stop(data_root=args.data_root)
            result = launch(data_root=args.data_root, source=args.source, api_port=args.api_port,
                            web_port=args.web_port, open_browser=not args.no_browser, require_independent=True)
            if receipt:
                write_json(receipt, {"ok": True, "web_url": result["web_url"], "api_url": result["api_url"],
                                     "lifetime": result["lifetime"]})
            print("工作台已启动：" + result["web_url"] + "；数据目录：" + result["data_root"])
        else:
            data_root = resolved_data_root(args.data_root)
            layout = runtime_layout(APP_ROOT, data_root)
            command = [layout["api_python"], str(APP_ROOT / "tooling/backup/manage.py"), args.action,
                       "--data-root", str(data_root)]
            if args.destination:
                command += ["--destination", args.destination]
            if args.backup_root:
                command += ["--backup-root", args.backup_root]
            environment = os.environ.copy()
            if not layout["candidate"] and not layout["installed"]:
                environment["PYTHONPATH"] = str(APP_ROOT / "apps/api/src")
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(command, env=environment, creationflags=NO_WINDOW, check=False)
            return result.returncode
        return 0
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
        if receipt and not receipt.exists() and receipt.parent == resolved_data_root(args.data_root) / "runtime":
            write_json(receipt, {"ok": False, "detail": str(exc)})
        print("未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
