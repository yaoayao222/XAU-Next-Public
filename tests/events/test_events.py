from concurrent.futures import ThreadPoolExecutor

import pytest
from xau_next.platform.db import Database
from xau_next.platform.events import EventStore, format_sse


def test_ac19_snapshot_cursor_same_transaction_and_reconnect(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    events, docs = EventStore(db), db.documents("analysis")
    with db.write() as tx:
        docs.insert(tx, "forecast", "f", {"current": "v1"})
        events.append(tx, "resource.changed", resource={"kind": "forecast", "id": "f"}, revision=1)
    with db.read() as scope:
        assert docs.get(scope, "forecast", "f")["revision"] == 1

        def writer():
            with db.write() as tx:
                docs.update(tx, "forecast", "f", {"current": "v2"}, 1)
                events.append(tx, "resource.changed", resource={"kind": "forecast", "id": "f"}, revision=2)

        with ThreadPoolExecutor() as pool:
            pool.submit(writer).result()
        assert events.cursor(scope) == 1
    assert [e["id"] for e in events.replay(1)] == [2]
    assert [e["id"] for e in events.replay(0, last_event_id="1")] == [2]
    frame = format_sse(events.replay(1)[0])
    assert frame.startswith("id: 2\nevent: resource.changed\ndata: ") and frame.endswith("\n\n")
    with pytest.raises(RuntimeError), db.write() as tx:
        events.append(tx, "job.changed", job_id="rolled-back", revision=1)
        raise RuntimeError("rollback")
    assert events.replay(2) == []
    db.close()


def test_ac19_retention_requires_resync_and_ids_never_reuse(tmp_path):
    time = ["2026-10-01T00:00:00Z"]
    db = Database(tmp_path, clock=lambda: time[0])
    db.initialize()
    events = EventStore(db)
    with db.write() as tx:
        events.append(tx, "job.changed", job_id="j", revision=1)
    assert events.prune("2026-10-20T00:00:00Z") == 0  # Cannot shorten the promised seven days.
    time[0] = "2026-10-10T00:00:00Z"
    assert events.prune() == 1
    assert events.replay(0)[0]["type"] == "resync.required"
    with db.read() as scope:
        assert events.cursor(scope) == 1
    with db.write() as tx:
        assert events.append(tx, "job.changed", job_id="j", revision=2)["id"] == 2
    assert [e["id"] for e in events.replay(1)] == [2]
    assert events.replay(900)[0]["type"] == "resync.required"
    db.close()
