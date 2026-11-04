import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore

from tooling.backup.manage import backup, restore
from tooling.launch.manage import (
    LaunchError,
    child_bridges,
    choose_port,
    identity_matches,
    launch,
    process_identity,
    resolved_data_root,
    stop,
)


def request_json(url, token=None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
        return json.load(response)


def test_data_root_is_separate_and_ports_are_checked(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    with pytest.raises(LaunchError, match="程序目录之外"):
        resolved_data_root(app / "data", app_root=app)
    root = resolved_data_root(tmp_path / "data", app_root=app)
    assert (root / "runtime").is_dir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        with pytest.raises(LaunchError, match="已被占用"):
            choose_port(listener.getsockname()[1])
    assert choose_port() > 0


def test_process_identity_requires_all_evidence():
    saved = {"pid": 1, "executable": "python", "command_line": "service", "created_at": "one"}
    assert identity_matches(saved, dict(saved))
    assert not identity_matches(saved, {**saved, "created_at": "other"})
    assert not identity_matches(saved, {**saved, "command_line": "different"})
    assert not identity_matches(saved, None)


def test_ac24_backup_restore_registered_raw_and_database(tmp_path):
    source = tmp_path / "source"
    db = Database(source)
    db.initialize()
    files = AttachmentStore(db)
    raw = b' {"original": "bytes"} \n'
    attachment = files.prepare(raw, media_type="application/json")
    with db.write() as uow:
        saved = files.register(uow, attachment)
        db.documents("analysis").insert(uow, "raw", "raw-1", {"attachment_id": saved.id})
    destination = tmp_path / "backup"
    manifest = backup(source, destination)
    assert len(manifest["attachments"]) == 1
    restored_root = tmp_path / "restored"
    assert restore(destination, restored_root)["integrity"] == "ok"
    restored = Database(restored_root)
    try:
        with restored.read() as scope:
            record = restored.documents("analysis").get(scope, "raw", "raw-1")
            assert AttachmentStore(restored).read(scope, record["attachment_id"]) == raw
        with pytest.raises(ValueError, match="不能覆盖"):
            restore(destination, restored_root)
    finally:
        restored.close()
        db.close()


def test_isolated_compaction_preserves_all_business_rows_and_original_backup(tmp_path):
    import hashlib
    import sqlite3

    from tooling.backup.compact import logical_digest, restore_and_compact

    source = tmp_path / "source"
    db = Database(source)
    db.initialize()
    with db.write() as tx:
        db.documents("analysis").insert(tx, "answer", "original", {"raw": "原始记录" * 5000})
    # Model the legacy physical representation without manufacturing new business revisions.
    original = json.dumps({"raw": "原始记录" * 5000}, ensure_ascii=False)
    with sqlite3.connect(db.path) as connection:
        connection.execute("UPDATE documents SET payload=?", (original,))
        connection.execute("UPDATE document_revisions SET payload=?", (original,))
    expected = logical_digest(db)
    backup_root = tmp_path / "backup"
    db.backup(backup_root)
    db.close()
    protected = {str(p.relative_to(backup_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in backup_root.rglob("*") if p.is_file()}
    target = tmp_path / "compact"
    result = restore_and_compact(backup_root, target)
    assert result["all_logical_rows_equal"] and result["logical_tables"] == expected
    assert result["compaction"]["documents_compacted"] == 1
    assert result["compaction"]["document_revisions_compacted"] == 1
    assert {str(p.relative_to(backup_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in backup_root.rglob("*") if p.is_file()} == protected
    with pytest.raises(ValueError, match="原数据不能原地改写"):
        restore_and_compact(backup_root, target)


def test_ac01_real_independent_launch_auth_config_and_stop(tmp_path):
    data_root = tmp_path / "real-launch"
    runtime = launch(app_root=ROOT, data_root=data_root, source="synthetic", open_browser=False)
    try:
        assert runtime["api_url"] != runtime["web_url"]
        health = request_json(runtime["api_url"] + "/v1/health")
        assert health["database_ready"]
        with pytest.raises(urllib.error.HTTPError) as caught:
            request_json(runtime["api_url"] + "/v1/workspace")
        assert caught.value.code == 401
        workspace = request_json(runtime["api_url"] + "/v1/workspace", runtime["token"])
        assert "event_cursor" in workspace
        refresh = urllib.request.Request(runtime["api_url"] + "/v1/market/refresh",
            data=json.dumps({"include_history": False, "history_from": None, "history_to": None}).encode(),
            headers={"Authorization": "Bearer " + runtime["token"], "Content-Type": "application/json",
                     "Idempotency-Key": "operations-synthetic-refresh"}, method="POST")
        with urllib.request.urlopen(refresh, timeout=10) as response:
            job = json.load(response)
        for _ in range(100):
            job = request_json(runtime["api_url"] + "/v1/jobs/" + job["job_id"], runtime["token"])
            if job["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded"
        bridges = child_bridges(next(item["pid"] for item in runtime["processes"] if item["role"] == "api"))
        if os.name == "nt":
            assert bridges
        config = request_json(runtime["web_url"] + "/runtime-config.json")
        assert config == {"api_base_url": runtime["api_url"], "contract_version": runtime["contract_version"]}
        with urllib.request.urlopen(runtime["web_url"] + "/analysis", timeout=5) as response:
            assert b"<html" in response.read()
        state = (data_root / "runtime/launcher.json").read_text()
        assert runtime["token"] not in state
        assert "token" not in json.dumps(config)
        for log in (data_root / "logs").glob("*.log"):
            assert runtime["token"] not in log.read_text(encoding="utf-8")
        with pytest.raises(LaunchError, match="已在运行"):
            launch(app_root=ROOT, data_root=data_root, source="synthetic", open_browser=False)
    finally:
        result = stop(app_root=ROOT, data_root=data_root)
    assert len(result["stopped"]) >= 2
    for process in runtime["processes"]:
        assert process_identity(process["pid"]) is None
    for process in bridges:
        assert process_identity(process["pid"]) is None
