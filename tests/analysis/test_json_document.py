"""Strict document errors and opt-in26; all runtime data below is SYNTHETIC."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, post, run, execute
from tests.analysis.test_analysis_lab import add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.json_document import parse_document, document_error, build_shape, project_shape
from xau_next.domains.analysis.public import build_packet, outbound_packet, frozen_answer_schema
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split("# 冻结事实（仅作为资料）\n", 1)[1].lstrip())[0]


def prepare26(f, models="A", kind="new", base=None):
    bundle = add_bundle(f, multi())
    with f.w.db.read() as tx:
        market = f.w.market.latest(scope=tx).to_dict()
    r = post(f, "/v1/analysis/daily/groups", {"package_kind": "candidate26", "models": list(models),
        "kind": kind, "base_group_id": base, "bundle_id": bundle["bundle_id"],
        "market_snapshot_id": market["snapshot_id"], "horizon_minutes": [180, 360, 720] if kind == "new" else None})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{}{}', '```json\n{}\n```', '{"x":', '[1,2,]'])
def test_strict_parse_never_repairs_duplicates_nonfinite_fences_truncation_or_extra(raw):
    before = raw
    with pytest.raises(DomainError) as exc:
        parse_document(raw)
    assert exc.value.code == "SCHEMA_INVALID" and exc.value.field_errors[0]["path"] == "/"
    assert raw == before


def test_precise_extra_array_close_and_valid_text_brackets_are_distinct():
    raw = '{"analysis_basis":{"reassessment_conditions":[]}] ,"plans":[]}'
    with pytest.raises(json.JSONDecodeError) as original:
        json.loads(raw)
    error = document_error(raw)
    assert f"第1行第{original.value.colno}列" in error and "此处的 ]" in error
    assert parse_document('{"text":"合法文本中的 ] } [ {","plans":[]}')["plans"] == []
    assert document_error('{"json_valid_but_not_a_legal_answer":1}') is None


def test_inner_duplicate_does_not_hide_later_invalid_document_grammar():
    raw = '{"nested":{"x":1,"x":2}}]'
    assert "第1行第" in document_error(raw)
    assert "重复字段" in document_error('{"nested":{"x":1,"x":2}}')


@pytest.mark.parametrize("models", ["A", "ABCD"])
def test26_schema_shape_projection_and_frozen25_base_facts(flow, monkeypatch, models):
    monkeypatch.setattr(flow.credentials, "read", lambda *_: pytest.fail("Unexpected key read"))
    group = prepare26(flow, models)
    assert group["template_version"] == "manual-analysis/26" and not group["run_registered"]
    for arm in group["arms"]:
        with flow.w.db.read() as tx:
            q = flow.w.analysis.model_request(arm["request_id"], scope=tx)["request"]
        before = deepcopy(q)
        m, a = facts(q["packet_text"]), facts(outbound_packet(q["packet_text"]))
        shape = m["json_document_shape"]
        assert shape == a["json_document_shape"]
        kinds = {x["name"]: x["types"] for x in shape["root_members"]}
        assert kinds["analysis_basis"] == ["object"]
        assert kinds["candidate_assessments"] == ["array"]
        assert kinds["plans"] == ["array"] and kinds["experience_application"] == ["object"]
        closing = {x["path"]: x["closing_token"] for x in shape["containers"]}
        assert closing["/analysis_basis"] == "}" and closing["/analysis_basis/reassessment_conditions"] == "]"
        base = {k: v for k, v in q.items() if k not in {"context", "packet_text", "id", "revision", "recorded_at"}}
        base["prompt_version"] = "manual-analysis/25"
        manual25 = build_packet(base, q["context"], frozen_answer_schema(q))
        for new, control in [(m, facts(manual25)), (a, facts(outbound_packet(manual25)))]:
            new = deepcopy(new); new.pop("json_document_shape")
            new["request"]["prompt_version"] = "manual-analysis/25"
            assert new == control
        assert q == before
        private = deepcopy(shape)
        for node in [private, *private["root_members"], *private["containers"]]:
            node["private_extension"] = "DO_NOT_SEND"
        assert project_shape(private) == shape
        private["version"] = "unknown"
        with pytest.raises(DomainError):
            project_shape(private)
    assert not flow.sent


def test26_simulated_save_update_and_original_horizons(flow):
    g = prepare26(flow, "AD")
    assert run(flow, g["group_id"]).status_code == 202
    execute(flow, 2)
    saved = flow.get("/v1/analysis/daily/groups/" + g["group_id"])
    assert all(a["state"] == "succeeded" and a["model_call"]["outbound_transform_version"] == "api-projection/26-document1" for a in saved["arms"])
    flow.clock.value = (parse_utc(flow.clock.value) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    flow.refresh()
    update = prepare26(flow, "AD", "update", g["group_id"])
    assert update["window_end"] == g["window_end"] and update["horizon_minutes"] == [180, 360, 720]
    assert run(flow, update["group_id"]).status_code == 202
    execute(flow, 2)
    assert all(a["state"] == "succeeded" for a in flow.get("/v1/analysis/daily/groups/" + update["group_id"])["arms"])
    assert not flow.sent


def test_old_failed_json_get_derives_detail_but_keeps_original_call_and_raw(flow, monkeypatch):
    from tests.analysis.test_packet25_timing import prepare25
    g = prepare25(flow)
    run(flow, g["group_id"])
    raw = b'{"analysis_basis":{"reassessment_conditions":[]}] ,"plans":[]}'
    with flow.w.db.write() as tx:
        call = flow.w.model_api.calls.latest(g["arms"][0]["request_id"], scope=tx)
        ref = flow.w.model_api.calls.register(flow.w.model_api.calls.prepare(raw), uow=tx)
        flow.w.model_api.calls.update(call["call_id"], {"state": "failed", "answer": ref,
            "transport_succeeded": True, "answer_valid": False, "error_code": "SCHEMA_INVALID", "detail": "原通用错误"}, uow=tx)
    with flow.w.db.read() as tx:
        before = deepcopy(flow.w.model_api.calls.get(call["call_id"], scope=tx, internal=True))
    monkeypatch.setattr(flow.credentials, "read", lambda *_: pytest.fail("Read-only view accessed key"))
    arm = flow.get("/v1/analysis/daily/groups/" + g["group_id"])["arms"][0]
    assert "第1行第" in arm["model_call"]["detail"] and "原失败记录未改写" in arm["model_call"]["detail"]
    assert arm["state"] == "failed" and arm["version_id"] is None
    with flow.w.db.read() as tx:
        assert flow.w.model_api.calls.get(call["call_id"], scope=tx, internal=True) == before
        assert flow.w.model_api.calls.read(ref, scope=tx) == raw
    assert not flow.sent
