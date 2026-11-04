"""SYNTHETIC regressions for the observed timestamp/required-array failure shape."""
import json
import re

import pytest
from xau_next.domains.analysis.model_api import output_checklist

from tests.model_api.test_workflow import ModelFlow


@pytest.fixture
def flow(tmp_path):
    instance = ModelFlow(tmp_path)
    yield instance
    instance.close()


def test_bad_date_and_missing_arrays_preserve_original_and_field_errors_without_retry(flow):
    req = flow.ready()
    original_request = flow.get("/v1/analysis/requests/" + req["request_id"])
    answer = json.loads(flow.answer_text)
    for assessment in answer["candidate_assessments"]:
        del assessment["missing_conditions"]
    invalid = re.sub(r"(\d{4})-(\d{2})-(\d{2})T", r"\1\2\3T", json.dumps(answer, ensure_ascii=False))
    flow.answer_text = invalid
    started = flow.start(req)
    prompt = json.loads(flow.w.model_api.calls.read(started["call"]["input"]))["input"]
    assert req["market_cutoff"] in prompt.split("# API 输出提交前检查", 1)[1]
    assert "missing_conditions: []" in prompt
    assert started["call"]["prompt_version"].endswith("+api-output/2")
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["transport_succeeded"] and record["answer_valid"] is False
    assert record["error_code"] == "SCHEMA_INVALID" and record["version_id"] is None
    problem = flow.get("/v1/jobs/" + record["job_id"])["error"]
    assert any(item["path"] == "/market_cutoff" for item in problem["field_errors"])
    assert sum("missing_conditions" in item["message"] for item in problem["field_errors"]) == len(answer["candidate_assessments"])
    assert flow.get("/v1/model/calls/" + record["call_id"] + "/raw")["answer"] == invalid
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2
    assert flow.get("/v1/workspace")["current_forecast"] is None
    assert flow.get("/v1/analysis/requests/" + req["request_id"])["packet_text"] == original_request["packet_text"]


def test_same_instant_with_changed_identity_string_remains_business_error(flow):
    req = flow.ready()
    answer = json.loads(flow.answer_text)
    assert req["market_cutoff"].endswith("Z") and "." not in req["market_cutoff"]
    answer["market_cutoff"] = req["market_cutoff"][:-1] + ".000Z"
    flow.answer_text = json.dumps(answer)
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["error_code"] == "REQUEST_MISMATCH" and record["version_id"] is None
    assert record["answer_valid"] is False


def test_checklist_uses_frozen_values_and_candidate_required_keys_from_schema(flow):
    req = flow.ready()
    with flow.w.db.read() as tx:
        _, schema, request = flow.w.analysis.model_packet(req["request_id"], scope=tx)
    checklist = output_checklist(request, schema)
    identity = json.loads(checklist.split("逐字符原样返回", 1)[1].splitlines()[1])
    for field in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        assert identity[field] == request[field]
    assert identity["schema_version"] == request["answer_schema_version"]
    schema["$defs"]["CandidateAssessment"]["required"].append("SYNTHETIC_future_required")
    assert "SYNTHETIC_future_required" in output_checklist(request, schema)
