"""Build an installable local candidate; never publish or touch another product."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tooling.launch.manage import LaunchError
from tooling.launch.setup_runtime import find_pnpm, find_uv, run


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(destination, *, app_root=ROOT):
    app_root, target = Path(app_root).resolve(), Path(destination).resolve()
    if target == app_root or app_root.is_relative_to(target):
        raise LaunchError("候选目标不能包含源码根目录。")
    if target.exists() and any(target.iterdir()):
        raise LaunchError("候选目标必须为空；不会覆盖已有候选。")
    target.mkdir(parents=True, exist_ok=True)
    uv = find_uv(app_root)
    pnpm = find_pnpm(app_root)
    node = shutil.which("node")
    if not pnpm or not node:
        raise LaunchError("构建候选需要已安装的 Node.js 和 pnpm 10.28.2。")
    run([*pnpm, "--filter", "@xau/web", "build"], cwd=app_root)
    (target / "api").mkdir()
    run([uv, "build", "--package", "xau-next-api", "--wheel", "--out-dir", target / "api"], cwd=app_root)
    requirements = target / "api/requirements.lock.txt"
    run([uv, "export", "--frozen", "--package", "xau-next-api", "--no-dev", "--no-emit-workspace", "--no-header",
         "--output-file", requirements], cwd=app_root)
    wheel = next((target / "api").glob("xau_next_api-*.whl"))
    shutil.copytree(app_root / "apps/web/dist", target / "web")
    shutil.copytree(app_root / "contracts", target / "contracts", ignore=shutil.ignore_patterns("__pycache__"))
    for folder in ("launch", "backup", "experiments"):
        shutil.copytree(app_root / "tooling" / folder, target / "tooling" / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "tooling/bin").mkdir()
    shutil.copy2(uv, target / "tooling/bin/uv.exe")
    shutil.copytree(app_root / "apps/mt5_bridge/src", target / "bridge/src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for filename in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copy2(app_root / "apps/mt5_bridge" / filename, target / "bridge" / filename)
    (target / "locks").mkdir()
    for filename in ("uv.lock", "pnpm-lock.yaml"):
        shutil.copy2(app_root / filename, target / "locks" / filename)
    for filename, action in (("Start.cmd", "start"), ("Stop.cmd", "stop"), ("Restart.cmd", "restart"), ("Setup.cmd", "setup")):
        (target / filename).write_text('@echo off\nchcp 65001 >nul\npy -3.12 "%~dp0tooling\\launch\\manage.py" ' + action + ' %*\n'
                                     'if errorlevel 1 pause\n', encoding="utf-8", newline="\r\n")
    if (app_root / "docs/OPERATIONS.md").is_file():
        shutil.copy2(app_root / "docs/OPERATIONS.md", target / "OPERATIONS.md")
    for filename in ("VALIDATION.md", "DELIVERY.md", "EXPERIENCE_EXPERIMENT.md"):
        source = app_root / "docs" / filename
        if source.is_file():
            (target / "docs").mkdir(exist_ok=True)
            shutil.copy2(source, target / "docs" / filename)
    revision = run(["git", "rev-parse", "HEAD"], cwd=app_root)
    dirty = bool(run(["git", "status", "--porcelain"], cwd=app_root))
    application_version = tomllib.loads((app_root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    contract_version = json.loads((app_root / "contracts/openapi.json").read_text(encoding="utf-8"))["info"]["version"]
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tools = {"python": sys.version.split()[0], "uv": run([uv, "--version"], cwd=app_root),
             "node": run([node, "--version"], cwd=app_root), "pnpm": run([*pnpm, "--version"], cwd=app_root)}
    for component, lock in (("api", "uv.lock"), ("web", "pnpm-lock.yaml")):
        component_root = target / component
        component_files = {path.relative_to(component_root).as_posix(): sha(path)
                           for path in sorted(component_root.rglob("*")) if path.is_file()}
        component_manifest = {"component": component, "application_version": application_version,
                              "contract_version": contract_version, "source_commit": revision,
                              "source_dirty": dirty, "created_at": created_at,
                              "lock_sha256": sha(app_root / lock), "files": component_files}
        (component_root / "release-manifest.json").write_text(
            json.dumps(component_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {path.relative_to(target).as_posix(): sha(path) for path in sorted(target.rglob("*")) if path.is_file()}
    build_id = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    manifest = {"format": "xau-next-release/1", "build_id": build_id,
                "application_version": application_version, "contract_version": contract_version,
                "created_at": created_at,
                "source_commit": revision, "source_dirty": dirty, "tools": tools,
                "api_wheel": wheel.relative_to(target).as_posix(),
                "api_requirements": requirements.relative_to(target).as_posix(), "files": files,
                "scope": "local_candidate_not_published", "locks": {name: sha(app_root / name) for name in ("uv.lock", "pnpm-lock.yaml")}}
    (target / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="构建独立 API wheel、静态 Web 和只读桥本机候选")
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        manifest = build(args.destination)
        print("候选已生成：" + str(Path(args.destination).resolve()) + "；build_id=" + manifest["build_id"])
        return 0
    except (LaunchError, ValueError, OSError, subprocess.SubprocessError) as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
