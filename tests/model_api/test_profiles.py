"""SYNTHETIC saved connections; all transports and credentials are injected."""
import copy
import json

import pytest

from tests.model_api.test_completion import update
from tests.model_api.test_workflow import KEY, ModelFlow


@pytest.fixture
def flow(tmp_path):
    f = ModelFlow(tmp_path)
    yield f
    f.close()


def profiles(flow):
    return flow.get("/v1/model/profiles")


def create(flow, name="本地 CPA", **changes):
    settings = flow.get("/v1/model/settings")
    return flow.post("/v1/model/profiles", {
        "expected_revision": settings["revision"], "name": name,
        "values": {**settings["values"], "provider": name, "base_url": "http://127.0.0.1:8317/v1",
                   "protocol": "responses", "output_format": "json_schema", "stream": True, **changes},
    }, 201)


def switch(flow, id, expected=200, revision=None):
    return flow.post("/v1/model/profiles/" + id + "/activate", {
        "expected_revision": revision or flow.get("/v1/model/settings")["revision"],
    }, expected)


def test_original_connection_and_credential_roundtrip_without_sending_or_reading_key(flow, monkeypatch):
    flow.ready()
    original = flow.get("/v1/model/settings")
    old_id = profiles(flow)["active_profile_id"]
    before_credentials = dict(flow.credentials.values)
    monkeypatch.setattr(flow.credentials, "read", lambda *_: pytest.fail("Switch must not read a saved key"))
    made = create(flow)
    cpa_id = profiles(flow)["active_profile_id"]
    assert cpa_id != old_id and not made["key_configured"] and not made["capability_verified"]
    assert made["checks"] == [] and made["last_successful_usage"] is None
    assert flow.credentials.values == before_credentials and len(flow.sent) == 1
    restored = switch(flow, old_id)
    assert restored["values"] == original["values"] and restored["key_configured"]
    assert restored["capability_verified"] and restored["checks"] == original["checks"]
    with flow.w.db.read() as tx:
        proof = flow.w.model_api.settings.docs.get(tx, "model_verified", str(restored["revision"]))
    assert proof["result"]["checked_at"] == original["checks"][-1]["checked_at"]
    assert "reused_from_revision" in proof
    assert switch(flow, cpa_id)["values"] == made["values"] and len(flow.sent) == 1
    public = json.dumps(profiles(flow)) + json.dumps(restored)
    assert KEY not in public and "credential_ref" not in public


def test_anthropic_messages_profile_is_saved_without_reusing_credentials_or_sending(flow):
    before = flow.get("/v1/model/settings")
    made = create(
        flow,
        "SYNTHETIC Anthropic",
        base_url="https://xcmapi.org/v1",
        protocol="anthropic_messages",
    )

    assert made["values"]["protocol"] == "anthropic_messages"
    assert not made["key_configured"]
    assert flow.get("/v1/model/settings")["values"]["protocol"] == "anthropic_messages"
    summary = next(item for item in profiles(flow)["items"] if item["name"] == "SYNTHETIC Anthropic")
    assert summary["protocol"] == "anthropic_messages"
    assert before["key_configured"] is False and flow.sent == []


def test_edit_and_credential_are_saved_for_each_profile_and_survive_restart(flow, tmp_path):
    flow.ready()
    old = profiles(flow)["active_profile_id"]
    create(flow)
    cpa = profiles(flow)["active_profile_id"]
    config = update(flow, timeout_seconds=600, max_output_tokens=25000)
    flow.post("/v1/model/credential", {"expected_revision": config["revision"], "key": "SYNTHETIC-CPA-key"}, 200)
    saved = copy.deepcopy(flow.get("/v1/model/settings")["values"])
    assert switch(flow, old)["key_configured"]
    flow.close()
    reopened = ModelFlow(tmp_path, flow.clock, flow.credentials)
    try:
        result = switch(reopened, cpa)
        assert result["values"] == saved and result["key_configured"] and not result["capability_verified"]
        with reopened.w.db.read() as tx:
            ref = reopened.w.model_api.settings.snapshot(scope=tx)["credential_ref"]
        assert reopened.credentials.values[ref] == "SYNTHETIC-CPA-key"
        assert reopened.sent == []
    finally:
        reopened.close()


def test_model_and_usage_checks_never_bleed_between_profiles(flow):
    flow.ready()
    flow.post("/v1/model/checks", {"kind": "usage"}, 200)
    original = flow.get("/v1/model/settings")
    old = profiles(flow)["active_profile_id"]
    assert original["last_successful_usage"] is not None
    assert create(flow)["last_successful_usage"] is None
    assert switch(flow, old)["last_successful_usage"] == original["last_successful_usage"]
    assert len(flow.sent) == 2


def test_changing_connection_does_not_resurrect_old_proof(flow):
    flow.ready()
    old = profiles(flow)["active_profile_id"]
    update(flow, model_id="SYNTHETIC-different-model")
    create(flow)
    assert not switch(flow, old)["capability_verified"]
    assert len(flow.sent) == 1


def test_switch_and_create_block_during_queued_model_job(flow):
    req = flow.ready()
    old = profiles(flow)["active_profile_id"]
    create(flow)
    cpa = profiles(flow)["active_profile_id"]
    switch(flow, old)
    flow.start(req)
    before = flow.get("/v1/model/settings")
    assert switch(flow, cpa, expected=409)["code"] == "MODEL_BUSY"
    body = {"expected_revision": before["revision"], "name": "SYNTHETIC-other", "values": before["values"]}
    assert flow.post("/v1/model/profiles", body, 409)["code"] == "MODEL_BUSY"
    assert flow.get("/v1/model/settings") == before and len(flow.sent) == 1


def test_switch_waits_for_cancelling_job_even_after_call_is_marked_cancelled(flow):
    req = flow.ready()
    old = profiles(flow)["active_profile_id"]
    create(flow)
    cpa = profiles(flow)["active_profile_id"]
    switch(flow, old)
    started = flow.start(req)
    assert flow.w.jobs.claim("SYNTHETIC-worker", kind="model_call") is not None
    flow.post("/v1/jobs/" + started["call"]["job_id"] + "/cancel", None, 202)
    assert flow.record(started)["state"] == "cancelled"
    assert switch(flow, cpa, expected=409)["code"] == "MODEL_BUSY"
    assert len(flow.sent) == 1


def test_late_usage_result_is_attributed_to_its_original_profile(flow):
    flow.ready()
    old = profiles(flow)["active_profile_id"]
    revision = flow.get("/v1/model/settings")["revision"]
    create(flow)
    result = {"kind": "usage", "checked_at": flow.clock(), "config_revision": revision,
              "status": "passed", "detail": "SYNTHETIC late result", "model_ids": [],
              "remaining": "7", "unit": "SYNTHETIC", "is_active": True, "call_id": None}
    with flow.w.db.write() as tx:
        flow.w.model_api.settings.record_check(result, uow=tx)
    assert flow.get("/v1/model/settings")["last_successful_usage"] is None
    assert switch(flow, old)["last_successful_usage"] == result


def test_stale_revision_duplicate_name_and_same_profile_are_safe(flow):
    flow.ready()
    initial = profiles(flow)
    old = initial["active_profile_id"]
    create(flow)
    cpa = profiles(flow)["active_profile_id"]
    assert switch(flow, old, expected=409, revision=initial["settings_revision"])["code"] == "REVISION_CONFLICT"
    before = flow.get("/v1/model/settings")
    assert switch(flow, cpa) == before
    assert flow.post("/v1/model/profiles", {"expected_revision": before["revision"],
                     "name": " 本地 CPA ", "values": before["values"]}, 409)["code"] == "MODEL_PROFILE_EXISTS"
    assert flow.get("/v1/model/settings") == before and len(flow.sent) == 1


def test_synthetic_generation_uses_only_the_selected_profile_key(flow):
    request = flow.ready()
    old = profiles(flow)["active_profile_id"]
    config = create(flow)
    flow.post("/v1/model/credential", {"expected_revision": config["revision"], "key": "SYNTHETIC-CPA-key"}, 200)
    probe = flow.post("/v1/model/probe", {"retry_of": None, "acknowledge_possible_charge": True}, 202)
    assert flow.w.model_api.process_once(flow.w)
    assert flow.get("/v1/model/calls/" + probe["call_id"])["state"] == "succeeded"
    assert flow.sent[-1].headers["authorization"] == "Bearer SYNTHETIC-CPA-key"
    assert str(flow.sent[-1].url) == "http://127.0.0.1:8317/v1/responses"
    switched = switch(flow, old)
    assert switched["capability_verified"] and len(flow.sent) == 2
    started = flow.start(request)
    assert flow.w.model_api.process_once(flow.w) and flow.record(started)["state"] == "succeeded"
    assert flow.sent[-1].headers["authorization"] == "Bearer " + KEY
    assert str(flow.sent[-1].url) == "https://api.zyuou.com/v1/responses"


def test_legacy_current_row_and_key_reference_are_not_rewritten_on_profile_adoption(flow, tmp_path):
    flow.ready()
    flow.close()
    import sqlite3
    connection = sqlite3.connect(tmp_path / "db/xau-next.sqlite3")
    before = connection.execute("SELECT payload,revision FROM documents WHERE owner='settings' AND kind='model_settings'").fetchone()
    connection.execute("DELETE FROM document_revisions WHERE owner='settings' AND kind LIKE 'model_profile%'")
    connection.execute("DELETE FROM documents WHERE owner='settings' AND kind LIKE 'model_profile%'")
    connection.commit()
    reopened = ModelFlow(tmp_path, flow.clock, flow.credentials)
    try:
        assert profiles(reopened)["active_profile_id"] == "default"
        assert reopened.get("/v1/model/settings")["capability_verified"]
        after = connection.execute("SELECT payload,revision FROM documents WHERE owner='settings' AND kind='model_settings'").fetchone()
        assert after == before and reopened.sent == []
    finally:
        reopened.close()
        connection.close()


def test_save_disabled_supplier_without_activation_or_credential_reuse(flow):
    before=flow.get('/v1/model/settings');old=profiles(flow)['active_profile_id']
    values={**before['values'],'provider':'SYNTHETIC-relay','base_url':'https://relay.invalid/v1',
        'enabled':False,'model_id':'待选择模型','model_ids':[],'protocol':'chat_completions'}
    made=flow.post('/v1/model/profiles',{'expected_revision':before['revision'],'name':'SYNTHETIC-pending','values':values,'activate':False},201)
    assert made==before and profiles(flow)['active_profile_id']==old
    item=next(i for i in profiles(flow)['items'] if i['name']=='SYNTHETIC-pending')
    assert not item['enabled'] and item['protocol']=='chat_completions'
    with flow.w.db.read() as tx:assert flow.w.model_api.settings.comparison_profile(item['profile_id'],scope=tx)['credential_ref'] is None
    assert not flow.sent
