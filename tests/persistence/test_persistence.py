import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from xau_next.platform.db import Database, DuplicateDocument, RevisionConflict
from xau_next.platform.files import AttachmentStore


def test_ac04_ac08_ac09_atomic_owner_scopes_and_restart(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    docs, plans = db.documents("analysis"), db.documents("plans")
    with db.write() as tx:
        docs.insert(tx, "forecast", "f", {"price": "2500.01"})
        plans.insert(tx, "plan", "p", {"forecast_id": "f"}, parent_id="f")
    with pytest.raises(RevisionConflict), db.write() as tx:
        plans.insert(tx, "plan", "rollback", {})
        docs.update(tx, "forecast", "f", {}, 2)
    with db.read() as scope:
        assert plans.get(scope, "plan", "rollback") is None
        assert docs.get(scope, "forecast", "f")["price"] == "2500.01"
        assert plans.get(scope, "forecast", "f") is None
        with pytest.raises(RuntimeError):
            plans.insert(scope, "plan", "bad", {})
    with pytest.raises(RuntimeError):
        docs.get(scope, "forecast", "f")
    db.close()
    reopened = Database(tmp_path)
    reopened.initialize()
    with reopened.read() as scope:
        assert reopened.documents("analysis").get(scope, "forecast", "f")["price"] == "2500.01"
    assert reopened.check_integrity()["integrity"] == "ok"
    with sqlite3.connect(reopened.path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    reopened.close()


def test_ac16_ac18_revisions_pagination_and_snapshot(tmp_path):
    db = Database(tmp_path, clock=lambda: "2026-10-12T02:00:00Z")
    db.initialize()
    docs = db.documents("settings")
    with db.write() as tx:
        docs.insert(tx, "settings", "a", {"fee": None}, available_at="2026-10-12T01:00:00Z")
        docs.insert(tx, "settings", "b", {"fee": "2"})
    with db.read() as scope:
        assert docs.get(scope, "settings", "a")["revision"] == 1
        with ThreadPoolExecutor() as pool:

            def change():
                with db.write() as tx:
                    docs.update(tx, "settings", "a", {"fee": "1.2"}, 1)

            pool.submit(change).result()
        assert docs.get(scope, "settings", "a")["revision"] == 1
        assert [r["id"] for r in docs.list(scope, "settings", limit=1)] == ["b"]
        assert [r["id"] for r in docs.list(scope, "settings", cursor="b")] == ["a"]
        assert len(docs.list(scope, "settings", available_before="2026-10-12T01:30:00Z")) == 1
    with db.read() as scope:
        assert [r["fee"] for r in docs.history(scope, "settings", "a")] == [None, "1.2"]
    db.close()


def test_ac06_concurrent_unique_and_caught_conflict_rolls_back(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    docs = db.documents("analysis")

    def insert():
        try:
            with db.write() as tx:
                docs.insert(tx, "request_result", "request1", {"version_id": "v1"})
            return True
        except DuplicateDocument:
            return False

    with ThreadPoolExecutor() as pool:
        assert sorted(pool.map(lambda _: insert(), range(2))) == [False, True]
    with pytest.raises(RuntimeError, match="回滚"), db.write() as tx:
        docs.insert(tx, "attempt", "rolled", {})
        try:
            docs.insert(tx, "request_result", "request1", {})
        except DuplicateDocument:
            pass
    with db.read() as scope:
        assert docs.get(scope, "attempt", "rolled") is None
    db.close()


def test_ac24_attachment_backup_restore_and_corruption(tmp_path):
    db = Database(tmp_path / "source")
    db.initialize()
    files = AttachmentStore(db)
    raw = b'{"price":"2500.01","raw":true}'
    prepared = files.prepare(raw, media_type="application/json")
    with db.read() as scope, pytest.raises(KeyError):
        files.read(scope, prepared.id)
    with db.write() as tx:
        ref = files.register(tx, prepared)
        db.documents("analysis").insert(tx, "version", "v1", {"raw_id": ref.id})
    manifest = db.backup(tmp_path / "backup")
    assert manifest["attachments"][0]["id"] == prepared.id
    restored = Database.restore(tmp_path / "backup", tmp_path / "restored")
    with restored.read() as scope:
        assert AttachmentStore(restored).read(scope, prepared.id) == raw
        assert restored.documents("analysis").get(scope, "version", "v1")["raw_id"] == prepared.id
    (restored.data_root / ref.relative_path).write_bytes(b"changed")
    with restored.read() as scope, pytest.raises(ValueError, match="SHA256"):
        AttachmentStore(restored).read(scope, ref.id)
    restored.close()
    db.close()


def test_linear_migration_upgrade_preserves_data_and_recovery_point(tmp_path):
    from alembic.script import ScriptDirectory

    db = Database(tmp_path)
    db.initialize(revision="0001_persistence")
    with db.write() as tx:
        db.documents("analysis").insert(tx, "forecast", "f", {"keep": True})
    db.initialize()
    assert len(ScriptDirectory.from_config(db._migration_config()).get_heads()) == 1
    assert db.check_integrity()["schema_version"] == "0004_local_notifications"
    with db.read() as scope:
        assert db.documents("analysis").get(scope, "forecast", "f")["keep"] is True
    assert len(list((tmp_path / "backups").glob("pre-migration-*/manifest.json"))) == 1
    db.close()


def test_owner_scoped_unindexed_filter_and_parent_reindexing(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    store = db.documents("analysis")
    with db.write() as tx:
        store.insert(tx, "forecast", "old", {"summary": "old"})
        store.insert(tx, "forecast", "current", {"summary": "current"}, parent_id="scope-one")
        db.documents("review").insert(tx, "forecast", "other-owner", {})
    with db.read() as scope:
        assert [item["id"] for item in store.list(scope, "forecast", parent_is_null=True)] == ["old"]
        with pytest.raises(ValueError):
            store.list(scope, "forecast", parent_is_null=True, parent_id="scope-one")
    with db.write() as tx:
        store.update(tx, "forecast", "old", {"summary": "migrated"}, 1, parent_id="scope-one")
        store.update(tx, "forecast", "current", {"summary": "kept"}, 1)
    with db.read() as scope:
        assert store.list(scope, "forecast", parent_is_null=True) == []
        assert len(store.list(scope, "forecast", parent_id="scope-one")) == 2
        assert len(store.history(scope, "forecast", "old")) == 2
    with db.write() as tx:
        store.update(tx, "forecast", "old", {"summary": "unindexed"}, 2, parent_id=None)
    with db.read() as scope:
        assert [item["id"] for item in store.list(scope, "forecast", parent_is_null=True)] == ["old"]
    db.close()
