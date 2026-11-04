"""Install pinned API and bridge environments outside the program directory."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from .manage import NO_WINDOW, LaunchError, read_json, resolved_data_root, write_json
except ImportError:
    from manage import NO_WINDOW, LaunchError, read_json, resolved_data_root, write_json


def find_uv(app_root):
    candidates = [app_root / "tooling/bin/uv.exe", app_root / ".tooling-venv/Scripts/uv.exe"]
    if shutil.which("uv"):
        candidates.append(Path(shutil.which("uv")))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise LaunchError("未找到 uv。请使用包含 tooling/bin/uv.exe 的候选目录，或安装锁定工具版本 uv 0.13.0。")


def run(command, *, cwd, env=None):
    result = subprocess.run([str(item) for item in command], cwd=cwd, env=env, creationflags=NO_WINDOW,
                            text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode:
        raise LaunchError("依赖安装或构建失败：" + (result.stderr or result.stdout)[-2500:])
    return result.stdout.strip()


def find_pnpm(app_root):
    expected = json.loads((app_root / "package.json").read_text(encoding="utf-8"))["packageManager"].split("@", 1)[1]
    pnpm = shutil.which("pnpm")
    if pnpm and run([pnpm, "--version"], cwd=app_root) == expected:
        return [pnpm]
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not npx:
        raise LaunchError(f"源码构建需要 pnpm {expected}；PATH 中版本不匹配，请安装对应版本。")
    command = [npx, "--yes", "pnpm@" + expected]
    if run([*command, "--version"], cwd=app_root) != expected:
        raise LaunchError("未能运行锁定的 pnpm 版本。")
    return command


def setup(app_root, data_root=None):
    app_root = Path(app_root).resolve()
    data_root = resolved_data_root(data_root, app_root=app_root)
    if sys.version_info[:2] != (3, 12):
        raise LaunchError("首版已锁定 Python 3.12，请用 py -3.12 运行安装入口。")
    uv = find_uv(app_root)
    uv_version = run([uv, "--version"], cwd=app_root)
    manifest = read_json(app_root / "release-manifest.json", {})
    if manifest:
        wheel = app_root / manifest["api_wheel"]
        requirements = app_root / manifest["api_requirements"]
        bridge = app_root / "bridge"
        for relative, digest in manifest["files"].items():
            path = (app_root / relative).resolve()
            if not path.is_relative_to(app_root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise LaunchError("候选文件校验失败：" + relative)
        if manifest["tools"]["uv"] != uv_version:
            raise LaunchError("uv 版本与候选清单不一致，请使用候选自带工具。")
        build_id = manifest["build_id"]
    else:
        # Developer setup may build source; application runtime never writes to APP_ROOT.
        artifacts = data_root / "runtime/setup-artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        requirements = artifacts / "api-requirements.lock.txt"
        run([uv, "export", "--frozen", "--package", "xau-next-api", "--no-dev", "--no-emit-workspace", "--no-header",
             "--output-file", requirements], cwd=app_root)
        run([uv, "build", "--package", "xau-next-api", "--wheel", "--out-dir", artifacts], cwd=app_root)
        wheels = sorted(artifacts.glob("xau_next_api-*.whl"), key=lambda path: path.stat().st_mtime_ns)
        if not wheels:
            raise LaunchError("未生成 API wheel，请检查构建日志。")
        wheel = wheels[-1]
        bridge = app_root / "apps/mt5_bridge"
        build_id = hashlib.sha256(wheel.read_bytes() + requirements.read_bytes()).hexdigest()[:16]
        pnpm = find_pnpm(app_root)
        run([*pnpm, "install", "--frozen-lockfile"], cwd=app_root)
        run([*pnpm, "--filter", "@xau/web", "build"], cwd=app_root)
    target = data_root / "runtime" / ("environments-" + build_id)
    api_env, bridge_env = target / "api", target / "bridge"
    api_python, bridge_python = api_env / "Scripts/python.exe", bridge_env / "Scripts/python.exe"
    if os.name != "nt":
        api_python, bridge_python = api_env / "bin/python", bridge_env / "bin/python"
    base_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    for environment in (api_env, bridge_env):
        if not environment.exists():
            run([uv, "venv", "--python", base_python, "--no-python-downloads", environment], cwd=app_root)
    run([uv, "pip", "sync", "--require-hashes", "--python", api_python, requirements], cwd=app_root)
    run([uv, "pip", "install", "--no-deps", "--python", api_python, wheel], cwd=app_root)
    environment = os.environ.copy()
    environment.update(UV_PROJECT_ENVIRONMENT=str(bridge_env), PYTHONDONTWRITEBYTECODE="1")
    run([uv, "sync", "--project", bridge, "--frozen", "--no-dev", "--python", base_python,
         "--no-python-downloads"], cwd=app_root, env=environment)
    versions = json.loads(run([api_python, "-c", ("import json,sys,importlib.metadata as m; "
        "print(json.dumps({'python':sys.version.split()[0],'api':m.version('xau-next-api'),'uvicorn':m.version('uvicorn')}))")],
        cwd=data_root))
    result = {"format": "xau-next-install/1", "app_root": str(app_root), "data_root": str(data_root),
              "api_python": str(api_python), "bridge_python": str(bridge_python), "versions": versions,
              "build_id": build_id, "uv": uv_version}
    write_json(data_root / "runtime/install.json", result)
    return result
