"""Validate specification materials independently of application acceptance."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path
from urllib.parse import unquote

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/SPEC_VALIDATION.json"
getcontext().prec = 50
checks: list[dict] = []


def load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8-sig"))


def check(name, fn):
    try:
        detail = fn()
        checks.append({"check": name, "status": "PASS", "detail": detail})
    except Exception as exc:  # noqa: BLE001 -- report every independent material check without aborting the rest
        checks.append({"check": name, "status": "FAIL", "detail": str(exc)})


def require(condition, message):
    if not condition:
        raise AssertionError(message)


api_path = ROOT / "contracts/openapi.json"
answer_path = ROOT / "contracts/analysis-answer.schema.json"
api = load("contracts/openapi.json")
answer = load("contracts/analysis-answer.schema.json")
fixture = load("fixtures/analysis-answer.valid.json")
variants = load("fixtures/answer-variants.json")
cases = load("fixtures/acceptance-cases.json")
tasks = load("planning/work-items.json")

resources = [
    (api_path.as_uri(), Resource.from_contents(api, default_specification=DRAFT202012)),
    (answer_path.as_uri(), Resource.from_contents(answer, default_specification=DRAFT202012)),
    (answer["$id"], Resource.from_contents(answer, default_specification=DRAFT202012)),
]
registry = Registry().with_resources(resources)


def material_files(pattern):
    return [p for folder in ["contracts", "fixtures", "planning", "docs", "tooling/vendor"]
            for p in (ROOT / folder).rglob(pattern)] + list(ROOT.glob(pattern))


def json_files():
    files = [p for p in material_files("*.json") if p != REPORT]
    for p in files:
        json.loads(p.read_text(encoding="utf-8-sig"))
    return {"files": len(files)}


def openapi_structure():
    meta = load("tooling/vendor/openapi-3.1.meta.json")
    source = load("tooling/vendor/SOURCE.json")
    actual = hashlib.sha256((ROOT / "tooling/vendor/openapi-3.1.meta.json").read_bytes()).hexdigest()
    require(actual == source["sha256"], "Vendored OpenAPI meta-schema hash mismatch")
    Draft202012Validator(meta).validate(api)
    Draft202012Validator.check_schema(answer)
    for schema in api["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)
    return {"openapi": api["openapi"], "schemas": len(api["components"]["schemas"]),
            "note": "Official structural meta-schema plus individual JSON Schema checks; not runtime API validation."}


def ref_targets():
    count = 0
    docs = {api_path.resolve(): api, answer_path.resolve(): answer}

    def walk(value, owner):
        nonlocal count
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                filename, _, fragment = ref.partition("#")
                require(not re.match(r"^[a-zA-Z]+:", filename), f"Remote reference: {ref}")
                target = (owner.parent / filename).resolve() if filename else owner
                require(target in docs, f"Unbundled reference: {ref}")
                item = docs[target]
                if fragment:
                    require(fragment.startswith("/"), f"Unsupported anchor: {ref}")
                    for part in fragment[1:].split("/"):
                        part = part.replace("~1", "/").replace("~0", "~")
                        item = item[int(part)] if isinstance(item, list) else item[part]
                count += 1
            for child in value.values():
                walk(child, owner)
        elif isinstance(value, list):
            for child in value:
                walk(child, owner)

    for owner, doc in docs.items():
        walk(doc, owner)
    return {"resolved_local_refs": count}


def answer_examples():
    validator = Draft202012Validator(answer, registry=registry, format_checker=FormatChecker())
    validator.validate(fixture)
    for variant in variants:
        value = copy.deepcopy(fixture)
        for mutation in variant["mutations"]:
            parent = value
            for part in mutation["path"][:-1]:
                parent = parent[part]
            key = mutation["path"][-1]
            if mutation["op"] == "remove":
                del parent[key]
            else:
                parent[key] = mutation["value"]
        valid = not list(validator.iter_errors(value))
        require(valid == variant["schema_valid"], f"Unexpected schema result: {variant['id']}")
    return {"valid_answer": 1, "variants": len(variants),
            "business_semantics": "Expected results only; this material check does not execute application behavior."}


def api_examples():
    examples = load("fixtures/api-examples.json")
    for example in examples:
        schema = {"$ref": api_path.as_uri() + "#/components/schemas/" + example["schema"]}
        Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(example["value"])
    return {"examples": len(examples)}


def operation_integrity():
    operations = []
    for path, methods in api["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations.append(operation["operationId"])
            declared = {p["name"] for p in operation.get("parameters", []) if p["in"] == "path" and p.get("required")}
            require(declared == set(re.findall(r"\{([^}]+)\}", path)), f"Path parameters mismatch: {path}")
            if method in {"post", "put", "patch", "delete"}:
                require(any(p["name"] == "Idempotency-Key" and p.get("required") for p in operation.get("parameters", [])),
                        f"Mutation missing idempotency: {path}")
    require(len(operations) == len(set(operations)), "Duplicate operationId")
    return {"operations": len(operations), "paths": len(api["paths"])}


def arithmetic():
    for vector in cases["money_vectors"]:
        i, expected, formula = vector["input"], vector["expected"], vector["formula"]
        d = lambda key, data=i: Decimal(data[key])
        if formula == "plan":
            direction = Decimal(1 if i["side"] == "long" else -1)
            entry, stop, target = d("entry"), d("stop"), d("target")
            require(stop < entry < target if direction == 1 else target < entry < stop, "Invalid vector geometry")
            gross_loss = abs(direction * (stop - entry) * d("contract_size") * d("lots"))
            loss = gross_loss + d("cost_at_stop")
            profit = direction * (target - entry) * d("contract_size") * d("lots") - d("cost_at_target")
            actual = {"gross_stop_loss": gross_loss, "planned_loss": loss,
                      "planned_net_profit": profit, "net_rr": profit / loss}
        elif formula == "budget":
            values = list(map(Decimal, i["net_results"]))
            consumed = sum((max(-v, Decimal(0)) for v in values), Decimal(0))
            equity = d("initial_equity") + sum(values)
            remaining = max(d("cumulative_cap") - consumed, Decimal(0))
            actual = {"equity": equity, "consumed": consumed, "remaining": remaining,
                      "allowed_next_risk": min(d("per_trade_cap"), remaining, max(equity, Decimal(0)))}
        elif formula == "budget_compare":
            actual = {"display": d("exact_risk").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                      "fits": d("exact_risk") <= d("cap")}
        elif formula == "actual":
            volumes, prices = list(map(Decimal, i["closed_lots"])), list(map(Decimal, i["exit_prices"]))
            actual = {"weighted_exit": sum(v * price for v, price in zip(volumes, prices)) / sum(volumes),
                      "complete_net": sum((Decimal(x) for key in ["realized_profit", "signed_commission", "signed_swap", "signed_other_fee"] for x in i[key]), Decimal(0))}
        elif formula == "range":
            lower, upper = max(d("pred_low") - d("actual_low"), Decimal(0)), max(d("actual_high") - d("pred_high"), Decimal(0))
            actual = {"lower_excess": lower, "upper_excess": upper, "fully_contained": lower == 0 and upper == 0}
        else:
            raise AssertionError(f"Unknown vector formula: {formula}")
        for key, value in expected.items():
            wanted = value if isinstance(value, bool) else Decimal(value)
            require(actual[key] == wanted, f"Arithmetic mismatch: {vector['id']}.{key}")
    return {"independent_decimal_vectors": len(cases["money_vectors"]), "not_application_tests": True}


def task_graph():
    by_id = {t["id"]: t for t in tasks["tasks"]}
    require(len(by_id) == len(tasks["tasks"]), "Duplicate task IDs")
    known_ac = {c["id"] for c in cases["cases"]}
    require(known_ac == {f"AC-{i:02}" for i in range(1, 25)}, "Acceptance case inventory mismatch")
    visited, visiting = set(), set()
    ancestors = {}

    def visit(task_id):
        require(task_id in by_id, f"Missing dependency: {task_id}")
        require(task_id not in visiting, f"Task cycle: {task_id}")
        if task_id in visited:
            return ancestors[task_id]
        visiting.add(task_id)
        parents = set()
        for dep in by_id[task_id]["depends_on"]:
            parents.add(dep)
            parents |= visit(dep)
        visiting.remove(task_id)
        visited.add(task_id)
        ancestors[task_id] = parents
        return parents

    coverage = set()
    for task_id, task in by_id.items():
        visit(task_id)
        require(task["status"] in {"planned", "in_progress", "ready_for_integration", "done", "blocked"},
                f"Unknown task status: {task_id}")
        require(bool(task["acceptance_ids"]), f"No acceptance mapping: {task_id}")
        require(set(task["acceptance_ids"]) <= known_ac, f"Unknown acceptance ID: {task_id}")
        coverage |= set(task["acceptance_ids"])
    require(coverage == known_ac, "Unmapped acceptance requirements")
    require("T19" not in ancestors["T17"], "Optional experiment blocks V1")

    # Conservative path-prefix comparison detects exact and directory ownership overlap.
    def prefix(path):
        return path.split("*")[0].rstrip("/")

    concurrent_overlaps = []
    names = list(by_id)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if left in ancestors[right] or right in ancestors[left]:
                continue
            for lp in by_id[left]["owned_paths"]:
                for rp in by_id[right]["owned_paths"]:
                    l, rr = prefix(lp), prefix(rp)
                    if l == rr or (lp.endswith("/**") and rr.startswith(l + "/")) or (rp.endswith("/**") and l.startswith(rr + "/")):
                        concurrent_overlaps.append([left, right, lp, rp])
    require(not concurrent_overlaps, f"Concurrent ownership overlap: {concurrent_overlaps}")
    return {"tasks": len(by_id), "mapped_acceptance_ids": len(coverage),
            "acyclic": True, "concurrent_owned_path_overlaps": 0, "optional_experiment_blocks_v1": False}


def local_documents():
    links = 0
    documents = material_files("*.md")
    for file in documents:
        content = file.read_text(encoding="utf-8-sig")
        require("\ufffd" not in content, f"Replacement character: {file}")
        require(content.count("```") % 2 == 0, f"Unclosed code fence: {file}")
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", content):
            target = match.group(1).split("#", 1)[0]
            if not target or re.match(r"^[a-zA-Z]+://", target):
                continue
            resolved = (file.parent / unquote(target)).resolve()
            require(resolved.exists() or resolved == REPORT, f"Broken local link: {file.name} -> {target}")
            links += 1
    return {"markdown_files": len(documents), "valid_local_links": links}


for name, fn in [
    ("json_parse", json_files), ("openapi_and_json_schema_structure", openapi_structure),
    ("offline_contract_references", ref_targets), ("answer_fixture_structure", answer_examples),
    ("api_fixture_structure", api_examples), ("http_operation_integrity", operation_integrity),
    ("independent_arithmetic_expectations", arithmetic), ("task_dag_ownership_and_acceptance", task_graph),
    ("document_links_and_format", local_documents),
]:
    check(name, fn)

passed = all(c["status"] == "PASS" for c in checks)
report = {
    "status": "PASS" if passed else "FAIL",
    "verified_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "scope": "design_materials_only",
    "checks": checks,
    "not_verified": ["application implementation", "Python/TypeScript generation toolchain", "runtime HTTP API", "real MT5", "model performance", "application performance", "trading profitability"],
    "file_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(set(material_files("*"))) if p.is_file() and p != REPORT},
}
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": report["status"], "checks": checks, "report": str(REPORT)}, ensure_ascii=False, indent=2))
sys.exit(0 if passed else 1)
