"""Isolated synthetic files/SQLite. No real bridge, credentials or model calls."""
import copy
import gzip
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from xau_next.domains.market import archive
from xau_next.domains.market.public import MarketService
from xau_next.platform.db import Database
from xau_next.platform.files import AttachmentStore
from xau_next.shared.errors import DomainError


def response():
    start = datetime(2026, 10, 12, 0, 31, tzinfo=UTC)
    bars, rates = [], []
    for i in range(600):
        at = start + timedelta(minutes=i)
        bars.append({"open_at": at.isoformat().replace("+00:00", "Z"), "closed": i < 599,
                     "open": "100.00", "high": "101.00", "low": "99.00", "close": "100.10",
                     "tick_volume": i, "extra": {"原始": None}})
        rates.append({"time": int(at.timestamp()), "open": 100.0, "high": 101.0,
                      "low": 99.0, "close": 100.1, "spread": 0, "real_volume": i})
    return {"source_identity": {"source_kind": "synthetic", "server": "fixture"},
            "result": {"market": {"snapshot_id": "first", "source_kind": "synthetic",
                                   "account_scope_id": "synthetic", "symbol": "XAUUSD",
                                   "captured_at": "2026-10-12T01:40:01Z", "quote": {"bid": "100.10"},
                                   "series": [{"timeframe": "M1", "price_basis": "bid", "bars": bars}]},
                       "raw": {"rates": {"M1": rates, "M5": None}, "time_normalization": {"source": "test"}},
                       "portfolio": {"snapshot_id": "portfolio", "orders": []}, "unknown": [None, 0, -0.0, ""]}}


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "source")
    db.initialize()
    yield db, AttachmentStore(db)
    db.close()


def commit(db, files, prepared):
    with db.write() as tx:
        for part in (*prepared.parts, prepared.manifest):
            files.register(tx, part)


def decode(db, files, prepared):
    with db.read() as read:
        return archive.decode(files.read(read, prepared.manifest.id), lambda ids: files.read_many(read, ids))


def test_roundtrip_full_serialized_bytes_and_shared_registration_without_temp_writes(store, monkeypatch):
    db, files = store
    value = response()
    first = archive.prepare(value, files)
    commit(db, files, first)
    assert archive.wire(decode(db, files, first)) == archive.wire(value)
    import xau_next.platform.files.store as module
    monkeypatch.setattr(module.os, "replace", lambda *_: pytest.fail("same content must not write a new temp file"))
    again = archive.prepare(value, files)
    commit(db, files, again)
    assert first == again
    with sqlite3.connect(db.path) as c:
        assert c.execute("select count(*) from attachments").fetchone()[0] == len(first.parts) + 1


@pytest.mark.parametrize("mutation", ["quote", "forming", "closed_revision", "closure", "slide", "source", "timebase"])
def test_each_observation_retains_its_original_content_and_reuses_unchanged_blocks(store, mutation):
    db, files = store
    a = response()
    b = copy.deepcopy(a)
    b["result"]["market"]["snapshot_id"] = "second"
    bars = b["result"]["market"]["series"][0]["bars"]
    if mutation == "quote": b["result"]["market"]["quote"]["bid"] = "100.11"
    if mutation == "forming": bars[-1]["close"] = "100.12"
    if mutation == "closed_revision": bars[0]["high"] = "101.50"
    if mutation == "closure": bars[-1]["closed"] = True
    if mutation == "slide": bars.pop(0); b["result"]["raw"]["rates"]["M1"].pop(0)
    if mutation == "source": b["source_identity"]["server"] = "other"
    if mutation == "timebase": b["result"]["raw"]["time_normalization"]["source"] = "other"
    one, two = archive.prepare(a, files), archive.prepare(b, files)
    commit(db, files, one); commit(db, files, two)
    assert archive.wire(decode(db, files, one)) == archive.wire(a)
    assert archive.wire(decode(db, files, two)) == archive.wire(b)
    shared = {p.id for p in one.parts} & {p.id for p in two.parts}
    assert bool(shared) == (mutation not in {"source", "timebase"})
    again = archive.prepare(a, files)
    assert again == one  # A -> B -> A preserves both B and original A.


@pytest.mark.parametrize("case", ["empty", "null", "missing", "duplicate", "reversed", "unknown_time", "unknown_period"])
def test_incomplete_and_unrecognized_arrays_preserve_order_types_and_extra_fields(store, case):
    db, files = store
    value = response()
    series = value["result"]["market"]["series"][0]
    if case == "empty": series["bars"] = []
    if case == "null": series["bars"] = None
    if case == "missing": del series["bars"]
    if case == "duplicate": series["bars"].insert(1, copy.deepcopy(series["bars"][0]))
    if case == "reversed": series["bars"].reverse()
    if case == "unknown_time": series["bars"][0]["open_at"] = "UNKNOWN"
    if case == "unknown_period": series["timeframe"] = "unknown"
    prepared = archive.prepare(value, files); commit(db, files, prepared)
    assert archive.wire(decode(db, files, prepared)) == archive.wire(value)


@pytest.mark.parametrize("fault", ["missing", "corrupt", "unregistered", "wrong_hash", "bad_codec", "bad_path"])
def test_missing_or_inconsistent_parts_and_manifests_fail_closed(store, fault):
    db, files = store
    prepared = archive.prepare(response(), files); commit(db, files, prepared)
    content = (db.data_root / prepared.manifest.relative_path).read_bytes()
    manifest = archive.unpack(content)
    part = prepared.parts[0]
    if fault == "missing": (db.data_root / part.relative_path).rename(db.data_root / "missing-fixture")
    if fault == "corrupt": (db.data_root / part.relative_path).write_bytes(b"broken")
    if fault == "unregistered":
        with sqlite3.connect(db.path) as c: c.execute("delete from attachments where id=?", (part.id,))
    if fault == "wrong_hash": manifest["arrays"][0]["chunks"][0]["sha256"] = "0" * 64
    if fault == "bad_codec": manifest["codec"] = "unknown"
    if fault == "bad_path": manifest["arrays"][0]["path"] = ["result", "unknown"]
    content = gzip.compress(archive.wire(manifest), mtime=0)
    with db.read() as read, pytest.raises((ValueError, KeyError, OSError)):
        archive.decode(content, lambda ids: files.read_many(read, ids))


def test_prepare_then_change_or_identity_conflict_and_rollback_cannot_commit_snapshot(store):
    db, files = store
    prepared = archive.prepare(response(), files)
    part = prepared.parts[0]
    (db.data_root / part.relative_path).write_bytes(b"changed")
    with pytest.raises(ValueError), db.write() as tx:
        files.register(tx, part)
    original = files.prepare_shared(b"first", media_type="fixture")
    with db.write() as tx: files.register(tx, original)
    other = replace(files.prepare_shared(b"second", media_type="fixture"), id=original.id)
    with pytest.raises(ValueError), db.write() as tx: files.register(tx, other)
    with pytest.raises(RuntimeError), db.write() as tx:
        files.register(tx, files.prepare_shared(b"uncommitted", media_type="fixture"))
        db.documents("market").insert(tx, "snapshot", "rolled-back", {"fixture": True})
        raise RuntimeError("isolated failure")
    with db.read() as read: assert db.documents("market").get(read, "snapshot", "rolled-back") is None


def test_all_changing_tail_rows_share_one_observation_file(store):
    db, files = store
    first = response()
    second = copy.deepcopy(first)
    second["result"]["market"]["series"][0]["bars"][-1]["close"] = "100.20"
    second["result"]["raw"]["rates"]["M1"][-1]["close"] = 100.2
    one = archive.prepare(first, files); commit(db, files, one)
    two = archive.prepare(second, files); commit(db, files, two)
    assert one.parts == two.parts
    assert decode(db, files, one) == first and decode(db, files, two) == second
    with sqlite3.connect(db.path) as c:
        assert c.execute("select count(*) from attachments").fetchone()[0] == len(one.parts) + 2


def test_old_and_new_snapshots_and_write_rollback_remain_readable_across_restart(tmp_path):
    db = Database(tmp_path / "mixed"); db.initialize()
    old = MarketService(db, mode="synthetic", storage_format="legacy")
    new = MarketService(db, mode="synthetic")
    try:
        captures = [old.capture({}, {"symbol": "XAUUSD"}), new.capture({}, {"symbol": "XAUUSD"})]
        for capture in captures:
            with db.write() as tx: new.commit_capture(capture, tx, settings_revision=17)
        ids = [c.market.to_dict()["snapshot_id"] for c in captures]
        with db.read() as read:
            assert [db.documents("market").get(read, "snapshot", id)["storage_format"] for id in ids] == [
                "market-snapshot/2", "market-snapshot/3"]
            for id, c in zip(ids, captures):
                assert new.get_snapshot(id, read).to_dict() == c.market.to_dict()
                assert new.capture_binding(id, scope=read)["capture_settings_revision"] == 17
                assert new.read_evidence(id, scope=read).to_dict()["result"]["market"] == c.market.to_dict()
        old.close(); new.close(); db.close()
        db = Database(tmp_path / "mixed")
        new = MarketService(db, mode="synthetic", storage_format="legacy")
        for id, c in zip(ids, captures):
            assert new.get_snapshot(id).to_dict() == c.market.to_dict()
        # Missing files fail even after a new Database/connection is opened.
        part = captures[1].archive.parts[0]
        (db.data_root / part.relative_path).rename(db.data_root / "missing-fixture")
        with pytest.raises(DomainError) as exc: new.get_snapshot(ids[1])
        assert exc.value.code == "SNAPSHOT_EVIDENCE_MISMATCH"
        assert new.get_snapshot(ids[0])
    finally:
        old.close(); new.close(); db.close()


def test_capture_commit_failure_rolls_back_every_registered_part_and_snapshot(store):
    db, files = store
    service = MarketService(db, files, mode="synthetic")
    try:
        prepared = service.capture({}, {"symbol": "XAUUSD"})
        with pytest.raises(RuntimeError), db.write() as tx:
            service.commit_capture(prepared, tx)
            raise RuntimeError("fail after all writes, before transaction commit")
        with sqlite3.connect(db.path) as c:
            assert c.execute("select count(*) from attachments").fetchone()[0] == 0
            assert c.execute("select count(*) from documents").fetchone()[0] == 0
        with db.write() as tx: service.commit_capture(prepared, tx)
        assert service.get_snapshot(prepared.market.to_dict()["snapshot_id"])
    finally: service.close()


def test_new_capture_and_all_parts_are_restorable_with_existing_backup_formats(store, tmp_path):
    from tooling.backup import fast
    db, files = store
    prepared = archive.prepare(response(), files); commit(db, files, prepared)
    with db.write() as tx:
        db.documents("market").insert(tx, "fixture", "independent", {"attachment_id": prepared.manifest.id})
    fast.backup(db.data_root, tmp_path / "full")
    fast.restore(tmp_path / "full", tmp_path / "restored")
    recovered = Database(tmp_path / "restored")
    try:
        assert decode(recovered, AttachmentStore(recovered), prepared) == response()
    finally: recovered.close()
    fast.backup(db.data_root, tmp_path / "snapshot", mode="snapshot")
    p = db.data_root / prepared.parts[0].relative_path
    p.rename(db.data_root / "unavailable-fixture")
    with pytest.raises(FileNotFoundError): fast.restore(tmp_path / "snapshot", tmp_path / "rejected")
    assert not (tmp_path / "rejected/db/xau-next.sqlite3").exists()
