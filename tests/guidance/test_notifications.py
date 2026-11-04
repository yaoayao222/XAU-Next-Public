from xau_next.platform.db.public import Database
from xau_next.platform.notifications.public import NotificationStore


class Sink:
    def __init__(self, state="submitted"):
        self.state, self.calls = state, []

    def send(self, notice):
        self.calls.append(notice)
        return self.state, "合成投递结果"


def enqueue(store, db, *, key="same", expiry="2026-10-12T00:01:00Z"):
    with db.write() as uow:
        return store.enqueue(uow, key, "scope", {"kind": "entry", "resource_id": "p", "title": "合成测试",
                                               "body": "不会交易", "context": {}}, expires_at=expiry)


def test_dedup_survives_restart_and_unknown_delivery_is_not_retried(tmp_path):
    db = Database(tmp_path, clock=lambda: "2026-10-12T00:00:00Z")
    db.initialize()
    sink = Sink("unknown")
    store = NotificationStore(db, sink=sink, allow_desktop=True)
    first, created = enqueue(store, db)
    assert created and not enqueue(store, db)[1]
    assert store.deliver_one(lambda _: (True, ""))["state"] == "unknown"
    restarted = NotificationStore(db, sink=sink, allow_desktop=True)
    restarted.recover()
    assert restarted.deliver_one(lambda _: (True, "")) is None
    assert len(sink.calls) == 1
    with db.read() as scope:
        assert restarted.list(scope, scope_key="scope")[0]["notification_id"] == first["notification_id"]
    db.close()


def test_expired_or_no_longer_valid_never_reaches_native_sink(tmp_path):
    db = Database(tmp_path, clock=lambda: "2026-10-12T00:00:00Z")
    db.initialize()
    sink = Sink()
    store = NotificationStore(db, sink=sink, allow_desktop=True)
    enqueue(store, db, expiry="2026-10-12T00:00:00Z")
    assert store.deliver_one(lambda _: (True, ""))["state"] == "suppressed"
    enqueue(store, db, key="changed")
    assert store.deliver_one(lambda _: (False, "方案已被新版本替代"))["state"] == "suppressed"
    assert not sink.calls
    db.close()


def test_delivery_failure_is_visible_and_does_not_rewrite_original_content(tmp_path):
    db = Database(tmp_path, clock=lambda: "2026-10-12T00:00:00Z")
    db.initialize()
    store = NotificationStore(db, sink=Sink("failed"), allow_desktop=True)
    before, _ = enqueue(store, db)
    after = store.deliver_one(lambda _: (True, ""))
    assert after["state"] == "failed"
    assert after["body"] == before["body"] and after["created_at"] == before["created_at"]
    assert after["submitted_at"] is None
    db.close()
