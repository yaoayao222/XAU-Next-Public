"""Executable dependency rules for the current implementation, independent of tests."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
errors = []
source = ROOT / "apps/api/src/xau_next"
for path in source.rglob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    relative = path.relative_to(source).as_posix()
    for node in ast.walk(tree):
        imports = (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [x.name for x in node.names]
            if isinstance(node, ast.Import)
            else []
        )
        for module in imports:
            if module.split(".")[0] == "MetaTrader5":
                errors.append(f"{relative}: API cannot import MT5 SDK")
            if relative.startswith("domains/"):
                owner = relative.split("/")[1]
                if module.startswith("xau_next.domains.") and module.split(".")[2] != owner:
                    errors.append(f"{relative}: cross-domain import {module}")
                if module.startswith(
                    ("xau_next.http", "xau_next.transport", "xau_next.workflows", "fastapi")
                ):
                    errors.append(f"{relative}: domain reverse dependency {module}")
            if relative.startswith("platform/") and module.startswith(
                ("xau_next.domains", "xau_next.http", "xau_next.workflows")
            ):
                errors.append(f"{relative}: platform reverse dependency {module}")
            if (
                relative.startswith(("http/", "workflows/"))
                and module.startswith("xau_next.domains.")
                and len(module.split(".")) > 3
                and module.split(".")[3] != "public"
            ):
                errors.append(f"{relative}: private domain import {module}")
for path in (ROOT / "apps/web/src").rglob("*"):
    if path.suffix not in (".ts", ".tsx"):
        continue
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT / "apps/web/src").as_posix()
    if re.search(r"\bfetch\s*\(", text) and not rel.startswith("api/"):
        errors.append(f"{rel}: network must use SDK adapter")
if errors:
    raise SystemExit("\n".join(errors))
print("Architecture dependency checks passed")
