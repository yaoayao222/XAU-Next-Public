import hashlib
import json
import sqlite3
import zlib

import pytest
from xau_next.platform.db import Database, PayloadIntegrityError
from xau_next.platform.db.core import canonical_json
from xau_next.platform.db.payloads import PAYLOAD_PREFIX


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "source", clock=lambda: "2026-10-12T02:00:00Z")
    db.initialize()
    yield db
    db.close()


def query(db, sql, parameters=()):
    with sqlite3.connect(db.path) as connection:
        return connection.execute(sql, parameters).fetchall()


def legacy_insert(db, id, text, *, kind="snapshot", owner="market"):
    """Build historical inline bytes as an older application would have stored them."""
    with sqlite3.connect(db.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (owner, kind, id, text, "original-parent", "original-state", None, "2026-10-11T01:00:00Z"),
        )
        connection.execute(
            "INSERT INTO document_revisions VALUES (?, ?, ?, 1, ?, ?)",
            (owner, kind, id, text, "2026-10-11T01:00:00Z"),
        )


def metadata(db):
    return {
        "documents": query(
            db,
            "SELECT owner,kind,id,revision,parent_id,state,available_at,recorded_at "
            "FROM documents ORDER BY owner,kind,id",
        ),
        "revisions": query(
            db,
            "SELECT owner,kind,id,revision,recorded_at FROM document_revisions ORDER BY owner,kind,id,revision",
        ),
        "events": query(db, "SELECT * FROM events ORDER BY id"),
    }


def test_small_inline_and_shared_large_blob_preserve_logical_json(database):
    db = database
    large = {"series": ["2600.01", None, True, {"文本": "合成数据" * 600}]}
    text = canonical_json(large)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with db.write() as scope:
        first = db.documents("market").insert(scope, "snapshot", "one", large)
        second = db.documents("plans").insert(scope, "evaluation", "two", large)
        small = db.documents("market").insert(scope, "snapshot", "small", {"empty": []})
    assert first == {**large, "id": "one", "revision": 1, "recorded_at": db.now()}
    assert query(db, "SELECT payload FROM documents WHERE id IN ('one','two')") == [
        (PAYLOAD_PREFIX + digest,),
        (PAYLOAD_PREFIX + digest,),
    ]
    assert query(db, "SELECT payload FROM document_revisions WHERE id IN ('one','two')") == [
        (PAYLOAD_PREFIX + digest,),
        (PAYLOAD_PREFIX + digest,),
    ]
    blobs = query(db, "SELECT sha256,codec,uncompressed_size,compressed FROM payload_blobs")
    assert len(blobs) == 1
    assert blobs[0][:3] == (digest, "zlib", len(text.encode("utf-8")))
    assert zlib.decompress(blobs[0][3]) == text.encode("utf-8")
    assert query(db, "SELECT payload FROM documents WHERE id='small'") == [('{"empty":[]}',)]
    with db.read() as scope:
        assert db.documents("market").get(scope, "snapshot", "one") == first
        assert db.documents("plans").get(scope, "evaluation", "two") == second
        assert {item["id"]: item for item in db.documents("market").list(scope, "snapshot")} == {
            "one": first,
            "small": small,
        }
        assert db.documents("market").history(scope, "snapshot", "one") == [first]


@pytest.mark.parametrize("size", [4095, 4096])
def test_encoding_threshold_uses_utf8_bytes(database, size):
    db = database
    text = '{"x":"' + "汉" * 1362 + "a" * (size - 4094) + '"}'
    assert len(text) < 4096
    assert len(text.encode("utf-8")) == size
    with db.write() as scope:
        stored = db.payload_codec().encode_text(scope, text)
    assert stored.startswith(PAYLOAD_PREFIX) == (size >= 4096)
    with db.read() as scope:
        assert db.payload_codec().decode_text(scope, stored) == text
    assert query(db, "SELECT COUNT(*) FROM payload_blobs")[0][0] == (1 if size >= 4096 else 0)


def test_mixed_old_inline_and_blob_revisions_remain_readable(database):
    db = database
    legacy_text = '{ "price" : 1.000, "raw" : "\\u4e2d", "padding" : "' + "旧数据" * 2000 + '" }\n'
    legacy_insert(db, "old", legacy_text)
    old = json.loads(legacy_text)
    replacement = {"new": "当前数据" * 2000}
    store = db.documents("market")
    with db.read() as scope:
        old_wire = store.get(scope, "snapshot", "old")
        assert old_wire == {**old, "id": "old", "revision": 1, "recorded_at": "2026-10-11T01:00:00Z"}
    with db.write() as scope:
        new_wire = store.update(scope, "snapshot", "old", replacement, 1)
        inline_wire = store.insert(scope, "snapshot", "small", {"value": None})
    with db.read() as scope:
        assert store.history(scope, "snapshot", "old") == [old_wire, new_wire]
        assert store.get(scope, "snapshot", "old") == new_wire
        assert {row["id"] for row in store.list(scope, "snapshot")} == {"old", inline_wire["id"]}
    assert query(db, "SELECT payload FROM document_revisions WHERE id='old' AND revision=1") == [
        (legacy_text,)
    ]
    assert query(db, "SELECT payload FROM document_revisions WHERE id='old' AND revision=2")[0][0].startswith(
        PAYLOAD_PREFIX
    )


def test_blob_and_business_revision_rollback_in_same_transaction(database):
    db = database
    store = db.documents("market")
    with db.write() as scope:
        original = store.insert(scope, "snapshot", "old", {"small": True})
    with pytest.raises(RuntimeError, match="interrupted"), db.write() as scope:
        store.update(scope, "snapshot", "old", {"large": "fixture" * 2000}, 1)
        store.insert(scope, "snapshot", "new", {"large": "different" * 2000})
        raise RuntimeError("interrupted")
    assert query(db, "SELECT COUNT(*) FROM payload_blobs") == [(0,)]
    with db.read() as scope:
        assert store.get(scope, "snapshot", "old") == original
        assert store.history(scope, "snapshot", "old") == [original]
        assert store.get(scope, "snapshot", "new") is None


@pytest.mark.parametrize(
    "damage", ["missing", "codec", "length", "truncated", "sha256", "trailing", "marker", "version"]
)
def test_corrupt_payload_fails_closed_for_get_list_and_history(database, damage):
    db = database
    store = db.documents("market")
    body = {"large": "synthetic" * 2000}
    raw = canonical_json(body).encode("utf-8")
    with db.write() as scope:
        store.insert(scope, "snapshot", "broken", body)
    with sqlite3.connect(db.path) as connection:
        if damage == "missing":
            connection.execute("DELETE FROM payload_blobs")
        elif damage == "codec":
            connection.execute("UPDATE payload_blobs SET codec='unknown'")
        elif damage == "length":
            connection.execute("UPDATE payload_blobs SET uncompressed_size=uncompressed_size-1")
        elif damage == "truncated":
            connection.execute("UPDATE payload_blobs SET compressed=?", (zlib.compress(raw)[:-2],))
        elif damage == "sha256":
            connection.execute("UPDATE payload_blobs SET compressed=?", (zlib.compress(b"x" * len(raw)),))
        elif damage == "trailing":
            connection.execute("UPDATE payload_blobs SET compressed=?", (zlib.compress(raw) + b"junk",))
        else:
            marker = PAYLOAD_PREFIX + "invalid" if damage == "marker" else "@xau-payload-v9:" + "0" * 64
            connection.execute("UPDATE documents SET payload=?", (marker,))
            connection.execute("UPDATE document_revisions SET payload=?", (marker,))
    for method in ("get", "list", "history"):
        with db.read() as scope, pytest.raises(PayloadIntegrityError) as failure:
            if method == "list":
                store.list(scope, "snapshot")
            else:
                getattr(store, method)(scope, "snapshot", "broken")
        assert failure.value.code == "PAYLOAD_CORRUPT"


def test_valid_hash_with_invalid_utf8_is_rejected(database):
    db = database
    raw = b"\xff" * 4096
    digest = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(db.path) as connection:
        connection.execute(
            "INSERT INTO payload_blobs VALUES (?, 'zlib', ?, ?)", (digest, len(raw), zlib.compress(raw))
        )
    with db.read() as scope, pytest.raises(PayloadIntegrityError) as failure:
        db.payload_codec().decode_text(scope, PAYLOAD_PREFIX + digest)
    assert failure.value.reason == "invalid_utf8"


def test_reusing_a_corrupt_blob_poisons_the_whole_write(database):
    db = database
    store = db.documents("market")
    body = {"large": "same payload" * 1000}
    with db.write() as scope:
        store.insert(scope, "snapshot", "original", body)
    with sqlite3.connect(db.path) as connection:
        connection.execute("UPDATE payload_blobs SET compressed=?", (b"broken",))
    with pytest.raises(RuntimeError, match="回滚"), db.write() as scope:
        store.insert(scope, "snapshot", "also-rollback", {"small": True})
        try:
            store.insert(scope, "snapshot", "reused", body)
        except PayloadIntegrityError:
            pass
    with db.read() as scope:
        assert store.get(scope, "snapshot", "also-rollback") is None
        assert store.get(scope, "snapshot", "reused") is None


def test_0003_only_adds_blob_storage_and_does_not_rewrite_old_payloads(tmp_path):
    db = Database(tmp_path / "legacy")
    try:
        db.initialize(revision="0002_jobs_events")
        raw = '{ "number" : 1.000, "padding" : "' + "historical" * 1000 + '" }\n'
        legacy_insert(db, "old", raw)
        before = metadata(db)
        with db.read() as scope:
            logical = db.documents("market").get(scope, "snapshot", "old")
        db.initialize()
        assert db.check_integrity()["schema_version"] == "0004_local_notifications"
        assert metadata(db) == before
        assert query(db, "SELECT payload FROM documents") == [(raw,)]
        assert query(db, "SELECT payload FROM document_revisions") == [(raw,)]
        assert query(db, "SELECT COUNT(*) FROM payload_blobs") == [(0,)]
        with db.read() as scope:
            assert db.documents("market").get(scope, "snapshot", "old") == logical
        assert len(list((db.data_root / "backups").glob("pre-migration-*/manifest.json"))) == 1
    finally:
        db.close()


def test_offline_compaction_preserves_exact_bytes_metadata_and_repeats_safely(database):
    db = database
    raw = '{ "number" : 1.000, "escaped" : "\\u4e2d", "padding" : "' + "保留文本" * 2000 + '" }\n'
    legacy_insert(db, "one", raw)
    legacy_insert(db, "two", raw)
    legacy_insert(db, "small", '{ "still" : "inline" }')
    before = metadata(db)
    with db.read() as scope:
        logical = db.documents("market").list(scope, "snapshot")
    notifications = []

    def progress(value):
        # The callback can read after a committed batch without a nested write lock.
        with db.read() as scope:
            assert db.documents("market").get(scope, "snapshot", "one") is not None
        notifications.append(value)

    result = db.compact_payloads(batch_size=1, progress=progress)
    assert result["documents_scanned"] == result["document_revisions_scanned"] == 3
    assert result["documents_compacted"] == result["document_revisions_compacted"] == 2
    assert result["blobs_created"] == 1
    assert result["business_revisions_changed"] == 0
    assert result["inline_bytes_replaced"] == len(raw.encode("utf-8")) * 4
    assert len(notifications) == 6
    assert metadata(db) == before
    stored = query(db, "SELECT payload FROM documents WHERE id='one'")[0][0]
    assert stored == PAYLOAD_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    with db.read() as scope:
        assert db.payload_codec().decode_text(scope, stored).encode("utf-8") == raw.encode("utf-8")
        assert db.documents("market").list(scope, "snapshot") == logical
    repeated = db.compact_payloads(batch_size=2)
    assert repeated["documents_compacted"] == repeated["document_revisions_compacted"] == 0
    assert repeated["blobs_created"] == 0
    assert repeated["existing_markers_verified"] == 4
    assert metadata(db) == before


def test_offline_compaction_can_resume_after_a_committed_batch(database):
    db = database
    legacy_insert(db, "a", canonical_json({"body": "first" * 2000}))
    legacy_insert(db, "b", canonical_json({"body": "second" * 2000}))
    before = metadata(db)

    def interrupt(_):
        raise RuntimeError("maintenance interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        db.compact_payloads(batch_size=1, progress=interrupt)
    assert query(db, "SELECT payload FROM documents WHERE id='a'")[0][0].startswith(PAYLOAD_PREFIX)
    assert query(db, "SELECT payload FROM documents WHERE id='b'")[0][0].startswith("{")
    result = db.compact_payloads(batch_size=1)
    assert result["documents_compacted"] == 1
    assert result["document_revisions_compacted"] == 2
    assert result["blobs_after"]["count"] == 2
    assert metadata(db) == before


def test_corruption_rolls_back_the_entire_current_compaction_batch(database):
    db = database
    raw = canonical_json({"body": "first" * 2000})
    legacy_insert(db, "a", raw)
    legacy_insert(db, "z", "@xau-payload-v1:missing")
    before = metadata(db)
    with pytest.raises(PayloadIntegrityError):
        db.compact_payloads(batch_size=100)
    assert query(db, "SELECT payload FROM documents WHERE id='a'") == [(raw,)]
    assert query(db, "SELECT COUNT(*) FROM payload_blobs") == [(0,)]
    assert metadata(db) == before


def test_backup_restores_blob_and_inline_payloads_with_revision_history(database, tmp_path):
    db = database
    body = {"body": "backup synthetic" * 2000}
    store = db.documents("market")
    legacy_insert(db, "old", canonical_json({"body": "legacy synthetic" * 2000}))
    with db.write() as scope:
        first = store.insert(scope, "snapshot", "new", body)
        second = store.update(scope, "snapshot", "new", {"body": "changed synthetic" * 2000}, 1)
    with db.read() as scope:
        old = store.get(scope, "snapshot", "old")
    before = metadata(db)
    manifest = db.backup(tmp_path / "backup")
    assert manifest["schema_version"] == "0004_local_notifications"
    restored = Database.restore(tmp_path / "backup", tmp_path / "restored")
    try:
        assert query(restored, "SELECT COUNT(*) FROM payload_blobs") == [(2,)]
        assert metadata(restored) == before
        with restored.read() as scope:
            restored_store = restored.documents("market")
            assert restored_store.get(scope, "snapshot", "old") == old
            assert restored_store.get(scope, "snapshot", "new") == second
            assert restored_store.history(scope, "snapshot", "new") == [first, second]
    finally:
        restored.close()
