"""Reproducible DTO / SDK generation from the only public contract sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY_OUT = ROOT / "apps/api/src/xau_next/transport/generated/models.py"
TS_OUT = ROOT / "packages/api-client/src/schema.ts"


def contract_digest(root=ROOT):
    """Hash UTF-8 sources with LF newlines, independently of Git checkout policy."""
    root = Path(root)
    return hashlib.sha256(
        b"".join(
            (root / name).read_text(encoding="utf-8").encode("utf-8")
            for name in ("contracts/openapi.json", "contracts/analysis-answer.schema.json")
        )
    ).hexdigest()


def generate():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    digest = contract_digest()
    with tempfile.TemporaryDirectory(prefix="xau-contract-") as directory:
        work = Path(directory)
        # Bundle external answer refs without duplicating authoritative definitions.
        api = json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8"))
        answer = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))

        def replace(node, answer_context=False):
            if isinstance(node, list):
                return [replace(v, answer_context) for v in node]
            if not isinstance(node, dict):
                return node
            output = {}
            for k, v in node.items():
                if k in ("$id", "$schema"):
                    continue
                if k == "$ref":
                    if v in ("analysis-answer.schema.json", "./analysis-answer.schema.json"):
                        v = "#/components/schemas/AnalysisAnswerBody"
                    elif (
                        v.lstrip("./").startswith("analysis-answer.schema.json#/$defs/")
                        or answer_context
                        and v.startswith("#/$defs/")
                    ):
                        v = "#/components/schemas/Answer_" + v.split("/")[-1]
                    output[k] = v
                else:
                    output[k] = replace(v, answer_context)
            return output

        api = replace(api)
        api["components"]["schemas"]["AnalysisAnswerBody"] = replace(
            {k: v for k, v in answer.items() if k != "$defs"}, True
        )
        for name, value in answer.get("$defs", {}).items():
            api["components"]["schemas"]["Answer_" + name] = replace(value, True)
        bundled = work / "openapi.json"
        bundled.write_text(json.dumps(api), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(bundled),
                "--input-file-type",
                "openapi",
                "--output",
                str(work / "models.py"),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.12",
                "--use-standard-collections",
                "--use-union-operator",
                "--disable-timestamp",
                "--use-schema-description",
                "--field-constraints",
                "--type-mappings",
                "string+date-time=string",
                "--formatters",
                "black",
                "isort",
            ],
            check=True,
            cwd=ROOT,
        )
        binary = ROOT / "node_modules/.bin/openapi-typescript.cmd"
        subprocess.run(
            [str(binary), str(bundled), "-o", str(work / "schema.ts")],
            check=True,
            cwd=ROOT,
            shell=os.name == "nt",
        )
        for source, target, prefix in [(work / "models.py", PY_OUT, "#"), (work / "schema.ts", TS_OUT, "//")]:
            value = f"{prefix} contract-sha256: {digest}\n" + source.read_text(encoding="utf-8")
            if args.check:
                if not target.exists() or target.read_text(encoding="utf-8") != value:
                    raise SystemExit(f"Generated contract drift: {target.relative_to(ROOT)}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value, encoding="utf-8", newline="\n")
    print("Contract generation verified" if args.check else "Generated Python DTO and TypeScript SDK types")


if __name__ == "__main__":
    generate()
