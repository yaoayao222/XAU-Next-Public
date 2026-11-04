"""Offline schema semantics and strict rejection tests; no provider requests."""

import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from xau_next.domains.settings.model_api import defaults
from xau_next.platform.model_gateway.deepseek_schema import annotate_union_types
from xau_next.platform.model_gateway.public import provider_schema, request_body
from xau_next.shared.errors import DomainError


@pytest.mark.parametrize("kind,valid", [("string", "a"), ("object", {}), ("number", 0.5), ("integer", 1)])
def test_nullable_union_preserves_acceptance(kind, valid):
    branch = {"type": kind}
    if kind == "object":
        branch.update(properties={"x": {"type": "integer"}}, required=["x"], additionalProperties=False)
        valid = {"x": 1}
    before = {"anyOf": [branch, {"type": "null"}]}
    original = deepcopy(before)
    after = annotate_union_types(before)
    assert before == original
    assert after == before
    for value in [None, valid, {}, [], True, "a", 0, 0.5]:
        assert Draft202012Validator(before).is_valid(value) == Draft202012Validator(after).is_valid(value)


def test_nested_rule_union_and_existing_parent_intersection_are_preserved():
    rules = {"anyOf": [
        {"type": "object", "properties": {"side": {"type": "string", "enum": ["above"]}},
         "required": ["side"], "additionalProperties": False},
        {"type": "object", "properties": {"period": {"type": "integer"}},
         "required": ["period"], "additionalProperties": False},
    ]}
    before = {"type": "object", "properties": {
        "rule": {"anyOf": [rules, {"type": "null"}]},
        "minutes": {"type": "integer", "anyOf": [{"type": "integer"}, {"type": "null"}]},
    }, "required": ["rule", "minutes"], "additionalProperties": False}
    after = annotate_union_types(before)
    assert "type" not in after["properties"]["rule"]
    assert after["properties"]["rule"]["anyOf"][0]["type"] == "object"
    assert after["properties"]["minutes"]["type"] == "integer"
    old, new = Draft202012Validator(before), Draft202012Validator(after)
    for rule in [None, {"side": "above"}, {"period": 1}, {}, {"side": "below"}, {"period": 1, "extra": 1}]:
        for minutes in [None, 1, "1", True, 1.5]:
            assert old.is_valid({"rule": rule, "minutes": minutes}) == new.is_valid({"rule": rule, "minutes": minutes})
    assert not new.is_valid({"rule": None, "minutes": None})


def test_observed_parser_errors_are_avoided_without_object_type_arrays():
    branch = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"], "additionalProperties": False}
    schema = {"anyOf": [{"description": "rule", "anyOf": [branch, branch]}, {"type": "null"}]}
    result = annotate_union_types(schema)
    assert result == {"anyOf": [{**branch, "description": "rule"}, {**branch, "description": "rule"}, {"type": "null"}]}
    assert Draft202012Validator(result).is_valid(None)
    assert Draft202012Validator(result).is_valid({"x": 1})
    assert not Draft202012Validator(result).is_valid("invalid")
    assert annotate_union_types(result) == result


def test_property_names_and_enum_payload_are_not_treated_as_schema_keywords():
    before = {"type": "object", "properties": {
        "anyOf": {"type": "string"}, "type": {"type": "object", "properties": {"anyOf": {"type": "string"}},
        "required": ["anyOf"], "additionalProperties": False, "enum": [{"anyOf": "literal"}]}},
        "required": ["anyOf", "type"], "additionalProperties": False}
    assert annotate_union_types(before) == before


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_only_official_deepseek_responses_schema_changes(model):
    schema = {"type": "object", "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
              "required": ["x"], "additionalProperties": False}
    base = {**defaults(), "protocol": "responses", "model_id": model, "output_format": "json_schema",
            "base_url": "https://api.deepseek.com/v1"}
    official = json.loads(request_body(base, "SYNTHETIC", schema))
    assert official["text"]["format"]["schema"] == annotate_union_types(provider_schema(schema))
    assert official["text"]["format"]["strict"] is True
    proxy = json.loads(request_body({**base, "base_url": "http://127.0.0.1:8317/v1"}, "SYNTHETIC", schema))
    assert proxy["text"]["format"]["schema"] == provider_schema(schema)
    official["text"]["format"]["schema"] = proxy["text"]["format"]["schema"]
    assert official == proxy


def test_non_schema_formats_do_not_enter_adapter():
    config = {**defaults(), "protocol": "responses", "model_id": "deepseek-flash",
              "base_url": "https://api.deepseek.com/v1", "output_format": "plain_json"}
    body = json.loads(request_body(config, "SYNTHETIC", {}))
    assert "text" not in body


@pytest.mark.parametrize("schema", [
    {"anyOf": [{"type": "string"}, {}]},
    {"anyOf": [{"properties": {"x": {"type": "string"}}}, {"type": "null"}]},
    {"type": "object", "$ref": "#/$defs/X"},
    {"anyOf": []}, {"type": "made-up"}, {"type": ["string", "string"]}, True,
    {"type": ["object", "null"]},
    {"anyOf": [{"anyOf": [{"type": "string"}, {"type": "null"}], "enum": [None]}, {"type": "integer"}]},
    {"type": "object", "anyOf": [{"type": "string"}]},
])
def test_unproved_types_refuse_locally(schema):
    with pytest.raises(DomainError) as error:
        annotate_union_types(schema)
    assert error.value.code == "MODEL_SCHEMA_UNSUPPORTED"
