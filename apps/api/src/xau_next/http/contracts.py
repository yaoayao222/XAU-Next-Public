"""Validate wire boundaries against the offline authority, including JSON Schema keywords."""

import json
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id


class Contracts:
    def __init__(self, root: Path):
        self.root = root.resolve()
        api_path = self.root / "openapi.json"
        answer_path = self.root / "analysis-answer.schema.json"
        self.api = json.loads(api_path.read_text(encoding="utf-8"))
        self.answer = json.loads(answer_path.read_text(encoding="utf-8"))
        self.registry = Registry().with_resources(
            [
                (api_path.as_uri(), Resource.from_contents(self.api, default_specification=DRAFT202012)),
                (
                    answer_path.as_uri(),
                    Resource.from_contents(self.answer, default_specification=DRAFT202012),
                ),
                (self.answer["$id"], Resource.from_contents(self.answer, default_specification=DRAFT202012)),
            ]
        )
        self.base = api_path.as_uri()
        self.documents = {
            self.base: self.api,
            answer_path.as_uri(): self.answer,
            self.answer["$id"]: self.answer,
        }
        self._expanded = {}
        self._validators = {}

    def _inline(self, node, document, stack=()):
        """Expand the frozen local reference DAG once, retaining $ref sibling assertions."""
        if isinstance(node, list):
            return [self._inline(value, document, stack) for value in node]
        if not isinstance(node, dict):
            return node
        if "$ref" not in node:
            return {
                key: self._inline(value, document, stack)
                for key, value in node.items()
                if key not in {"$id", "$schema"}
            }
        absolute = urljoin(document, node["$ref"])
        target_document, fragment = urldefrag(absolute)
        if absolute in stack:
            return {**node, "$ref": absolute}
        if absolute not in self._expanded:
            target = self.documents[target_document]
            for part in unquote(fragment).lstrip("/").split("/") if fragment else []:
                target = target[part.replace("~1", "/").replace("~0", "~")]
            self._expanded[absolute] = self._inline(target, target_document, (*stack, absolute))
        target = self._expanded[absolute]
        siblings = self._inline({k: v for k, v in node.items() if k != "$ref"}, document, stack)
        return {"allOf": [target, siblings]} if siblings else target

    def validate(self, name: str, value):
        if name not in self._validators:
            schema = self._inline({"$ref": self.base + "#/components/schemas/" + name}, self.base)
            self._validators[name] = Draft202012Validator(
                schema, registry=self.registry, format_checker=FormatChecker()
            )
        errors = list(self._validators[name].iter_errors(value))
        if errors:
            raise DomainError(
                "SCHEMA_INVALID",
                "输入格式不符合规范，请修正标出的字段。",
                422,
                [
                    {
                        "path": "/" + "/".join(map(str, e.absolute_path)),
                        "code": "SCHEMA_INVALID",
                        "message": "字段格式、范围或必填项不符合规范：" + e.message[:800],
                    }
                    for e in errors[:30]
                ],
            )
        return value


def problem(error, *, trace_id=None):
    return {
        "type": "urn:xau-next:problem:" + error.code.lower(),
        "title": "操作未完成",
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
        "trace_id": trace_id or new_id(),
        "field_errors": error.field_errors,
        "retryable": False,
    }
