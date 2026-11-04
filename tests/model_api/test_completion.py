"""SYNTHETIC completion budgets, protocol evidence reuse and lossless API compaction."""

import copy
import hashlib
import json

import httpx
import pytest
from xau_next.domains.analysis.model_api import compact_packet, outbound_packet, output_checklist
from xau_next.shared.values import new_id

from tests.guidance.test_workflow import TOKEN
from tests.model_api.test_gateway import envelope
from tests.model_api.test_workflow import ModelFlow


@pytest.fixture
def flow(tmp_path):
    instance = ModelFlow(tmp_path)
    yield instance
    instance.close()


def update(flow, **changes):
    current = flow.get("/v1/model/settings")
    response = flow.client.put(
        "/v1/model/settings",
        json={"expected_revision": current["revision"], "values": {**current["values"], **changes}},
        headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": new_id()},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_budget_change_reuses_original_probe_without_creating_a_probe_or_call(flow):
    req = flow.ready()
    before = flow.get("/v1/model/settings")
    changed = update(flow, max_response_bytes=16000000, max_output_tokens=25000, timeout_seconds=600)
    assert changed["capability_verified"] and changed["checks"] == before["checks"]
    assert len(flow.sent) == 1
    with flow.w.db.read() as tx:
        proof = flow.w.model_api.settings.docs.get(tx, "model_verified", str(changed["revision"]))
    assert proof["result"]["config_revision"] == before["revision"]
    assert proof["reused_from_revision"] == before["revision"]
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["state"] == "succeeded" and len(flow.sent) == 2
    assert record["max_response_bytes"] == 16000000 and record["max_answer_bytes"] == 2000000
    assert json.loads(flow.sent[-1].content)["max_output_tokens"] == 25000


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "https://different.example/v1"},
        {"model_id": "different-model"},
        {"protocol": "chat_completions"},
        {"reasoning_effort": "medium"},
        {"output_format": "json_object"},
        {"stream": True},
    ],
)
def test_protocol_identity_change_never_inherits_probe(flow, changes):
    flow.ready()
    assert update(flow, **changes)["capability_verified"] is False
    assert update(flow, max_response_bytes=16000000)["capability_verified"] is False
    assert len(flow.sent) == 1


def test_key_change_does_not_inherit_probe_even_when_followed_by_budget_change(flow):
    flow.ready()
    config = flow.get("/v1/model/settings")
    flow.post(
        "/v1/model/credential",
        {
            "expected_revision": config["revision"],
            "key": "SYNTHETIC-new-application-key",
        },
        200,
    )
    assert not update(flow, max_output_tokens=25000)["capability_verified"]


def test_legacy_settings_get_adds_answer_default_without_rewriting_stored_revision(flow):
    settings = flow.w.model_api.settings
    with flow.w.db.write() as tx:
        old = settings.snapshot(scope=tx)
        values = dict(old["values"])
        values.pop("max_answer_bytes")
        settings.docs.update(
            tx,
            "model_settings",
            "current",
            {
                "values": values,
                "credential_ref": old["credential_ref"],
            },
            old["revision"],
        )
    with flow.w.db.read() as tx:
        saved = settings.docs.get(tx, "model_settings", "current")
    assert flow.get("/v1/model/settings")["values"]["max_answer_bytes"] == 2000000
    with flow.w.db.read() as tx:
        assert settings.docs.get(tx, "model_settings", "current") == saved


def test_api_compaction_preserves_all_projected_facts_schema_and_prose_rules(flow):
    req = flow.ready()
    with flow.w.db.read() as tx:
        request = flow.w.analysis._docs.get(tx, "request", req["request_id"])
    packet = request["packet_text"]
    digest = hashlib.sha256(packet.encode()).hexdigest()
    output = outbound_packet(packet)
    marker = "# 冻结事实（仅作为资料）\n"
    prefix, tail = packet.split(marker, 1)
    raw_facts, suffix = tail.split("\n# 全部字段说明\n", 1)
    raw_schema = suffix.split("\n# 权威答案 JSON Schema\n", 1)[1]
    original_schema, end = json.JSONDecoder().raw_decode(raw_schema)
    compact_prefix, compact_tail = output.split(marker, 1)
    compact_facts, compact_tail = compact_tail.split("\n# 字段含义\n", 1)
    meanings, compact_schema = compact_tail.split("\n# 权威答案 JSON Schema\n", 1)
    schema, compact_end = json.JSONDecoder().raw_decode(compact_schema)
    assert prefix == compact_prefix and schema == original_schema
    assert compact_schema[compact_end:] == raw_schema[end:] + output_checklist(
        json.loads(compact_facts)["request"], schema
    )
    from xau_next.domains.analysis.package import FIELD_MEANINGS

    for meaning in FIELD_MEANINGS.values():
        if meaning in suffix.split("\n# 权威答案 JSON Schema\n", 1)[0]:
            assert meaning in meanings
    assert (
        json.loads(compact_facts)["request"]["request_id"] == json.loads(raw_facts)["request"]["request_id"]
    )
    # Exercise serialization separately from the explicit privacy projection.
    unprojected = compact_packet(prefix, json.loads(raw_facts), suffix)
    unprojected_facts = unprojected.split(marker, 1)[1].split("\n# 字段含义\n", 1)[0]
    assert json.loads(unprojected_facts) == json.loads(raw_facts)
    assert "# 全部字段说明" not in output and len(output.encode()) < len(packet.encode())
    assert hashlib.sha256(packet.encode()).hexdigest() == digest
    changed = copy.deepcopy(json.loads(raw_facts))
    changed["market"]["terminal_path"] = "SYNTHETIC-private-path"
    changed["portfolio"]["account"]["login"] = "SYNTHETIC-private-login"
    altered = prefix + marker + json.dumps(changed) + "\n# 全部字段说明\n" + suffix
    assert outbound_packet(altered) == output


def test_large_fine_grained_stream_is_saved_and_imported_once_with_small_answer(flow):
    req = flow.ready()
    update(flow, max_response_bytes=16000000)
    padding = (
        "data: "
        + json.dumps(
            {"type": "response.reasoning_summary_text.delta", "delta": "SYNTHETIC", "padding": "x" * 150}
        )
        + "\n\n"
    ).encode()
    terminal = (
        "data: " + json.dumps({"type": "response.completed", "response": envelope(flow.answer_text)}) + "\n\n"
    ).encode()
    raw = padding * 20000 + terminal
    assert len(raw) > 4000000

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(0, len(raw), 503):
                yield raw[i : i + 503]

    flow.handler = lambda _: httpx.Response(
        200, stream=Chunks(), headers={"content-type": "text/event-stream"}
    )
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["state"] == "succeeded" and record["raw_response"]["size"] == len(raw)
    assert record["raw_response"]["sha256"] == hashlib.sha256(raw).hexdigest()
    saved = flow.get("/v1/model/calls/" + record["call_id"] + "/raw")
    assert saved["raw_response"].encode() == raw and saved["answer"] == flow.answer_text
    assert flow.start(req)["version_id"] == record["version_id"] and len(flow.sent) == 2


def test_final_answer_limit_counts_utf8_bytes_retains_raw_usage_and_never_imports(flow):
    req = flow.ready()
    update(flow, max_response_bytes=16000000, max_answer_bytes=4096)
    flow.answer_text = json.dumps({"text": "界" * 1400}, ensure_ascii=False)
    assert len(flow.answer_text) < 4096 < len(flow.answer_text.encode())
    started = flow.start(req)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record["error_code"] == "MODEL_ANSWER_TOO_LARGE" and record["state"] == "failed"
    assert record["stage"] == "response_saved" and record["raw_response"] and record["answer"] is None
    assert record["input_tokens"] == 12 and record["version_id"] is None
    assert not flow.w.model_api.process_once(flow.w) and len(flow.sent) == 2
    assert flow.get("/v1/workspace")["current_forecast"] is None
