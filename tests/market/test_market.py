import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from xau_next.domains.market.bridge import BridgeError
from xau_next.domains.market.public import MarketService
from xau_next.platform.db import Database
from xau_next.platform.files import AttachmentStore
from xau_next.shared.errors import DomainError

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("missing_close", [None, "2026-10-12T02:15:00Z", "2026-10-12T01:45:00Z"])
def test_native_m15_window_uses_close_events_and_keeps_real_condition_gaps(tmp_path, missing_close):
    from dataclasses import replace

    from xau_next.domains.review.public import compute_reference
    from xau_next.shared.snapshots import Snapshot
    from xau_next.shared.values import parse_utc

    db = Database(tmp_path)
    db.initialize()
    service = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T03:03:00Z")
    try:
        command = {
            "include_history": True,
            "history_from": "2026-10-12T02:12:00Z",
            "history_to": "2026-10-12T03:03:00Z",
        }
        prepared = service.capture(command, {"values": {"symbol": "XAUUSD"}})
        market = prepared.market.to_dict()
        native = next(s for s in market["series"] if s["timeframe"] == "M15")
        if missing_close:
            native["bars"] = [
                b for b in native["bars"] if parse_utc(b["close_at"]) != parse_utc(missing_close)
            ]
            native["status"].update(state="incomplete", reasons=["合成来源包含一根缺柱"])
        prepared = replace(prepared, market=Snapshot.of(market))
        window = service.window_from_capture(
            prepared, command["history_from"], command["history_to"]
        ).to_dict()
        projected = next(s for s in window["series"] if s["timeframe"] == "M15")
        definition = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))[
            "plans"
        ][0]
        definition.update(
            activation={"kind": "manual_observation", "instruction": "没有人工确认，不补认触发"},
            invalidation={"kind": "bar_close", "timeframe": "M15", "operator": "lt", "price": "98"},
            valid_from="2026-10-12T02:14:50.792Z",
            entry_deadline="2026-10-12T02:46:00Z",
            exit_deadline="2026-10-12T03:00:50.792Z",
        )
        plan = {
            "plan_id": "m15-plan",
            "version_id": "m15-version",
            "available_at": "2026-10-12T02:14:51.792Z",
            "definition": definition,
            "manual_observations": [],
            "lifecycle": "WAITING_TRIGGER",
        }
        result = compute_reference(plan, window, as_of="2026-10-12T03:03:00Z").to_dict()["review"]
        if missing_close == "2026-10-12T02:15:00Z":
            assert projected["status"]["state"] == "incomplete"
            assert result["outcome"] == "DATA_INCOMPLETE"
        else:
            assert projected["status"]["state"] == "available"
            assert parse_utc(projected["bars"][0]["open_at"]) == parse_utc("2026-10-12T02:00:00Z")
            assert result["outcome"] == "NOT_TRIGGERED" and result["coverage"] == "complete"
    finally:
        service.close()
        db.close()


def test_market_storage_references_evidence_and_reads_legacy_inline(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    service = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T02:07:00Z")
    try:
        prepared = service.capture({}, {"values": {"symbol": "XAUUSD"}})
        original = prepared.market.to_dict()
        with db.write() as tx:
            service.commit_capture(prepared, tx)
            legacy = {**original, "snapshot_id": "legacy-inline-market"}
            db.documents("market").insert(tx, "snapshot", legacy["snapshot_id"], legacy)
        with db.read() as scope:
            stored = db.documents("market").get(scope, "snapshot", original["snapshot_id"])
            assert stored["storage_format"] == "market-snapshot/3"
            assert "series" not in stored["header"]
            assert len(json.dumps(stored)) < 4096
        assert service.get_snapshot(original["snapshot_id"]).to_dict() == original
        assert service.get_snapshot(legacy["snapshot_id"]).to_dict() == legacy
        with db.write() as tx:
            stored["header"]["symbol"] = "WRONG"
            db.documents("market").update(tx, "snapshot", original["snapshot_id"], stored, stored["revision"])
        with pytest.raises(DomainError) as exc:
            service.get_snapshot(original["snapshot_id"])
        assert exc.value.code == "SNAPSHOT_EVIDENCE_MISMATCH"
    finally:
        service.close()
        db.close()


def validate(name, value):
    root = json.loads((ROOT / "contracts/openapi.json").read_text(encoding="utf-8"))
    answer = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
    registry = Registry().with_resource("./analysis-answer.schema.json", Resource.from_contents(answer))
    Draft202012Validator(
        {**root, "$ref": f"#/components/schemas/{name}"}, registry=registry, format_checker=FormatChecker()
    ).validate(value)


def test_ac02_synthetic_subprocess_capture_contract_and_restart(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    market = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T02:07:00Z")
    prepared = market.capture(
        {"include_history": False, "history_from": None, "history_to": None},
        {"values": {"symbol": "XAUUSD", "connection": {"account_scope_id": None}}},
    )
    validate("MarketSnapshot", prepared.market.to_dict())
    validate("Portfolio", prepared.portfolio.to_dict())
    assert prepared.market.to_dict()["source_kind"] == "synthetic"
    assert prepared.market.to_dict()["quote"]["ask"] == "100"
    assert len(prepared.market.to_dict()["series"]) == 4
    with db.write() as tx:
        result = market.commit_capture(prepared, tx)
    market.close()
    db.close()
    recovered = Database(tmp_path)
    recovered.initialize()
    reopened = MarketService(recovered, mode="synthetic")
    assert reopened.latest().to_dict() == result.to_dict()
    assert reopened.read_window(
        result.to_dict()["snapshot_id"], "2026-10-12T02:00:00Z", "2026-10-12T02:07:00Z"
    ).to_dict()["bars"]
    with recovered.read() as scope:
        assert AttachmentStore(recovered).read(scope, prepared.raw_attachment.id).startswith(b"\x1f\x8b")
    reopened.close()
    recovered.close()


def test_history_stored_as_attachment_has_frozen_ticks_and_independent_bar_status(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    service = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T02:07:00Z")
    command = {
        "include_history": True,
        "history_from": "2026-10-12T02:00:00Z",
        "history_to": "2026-10-12T02:07:00Z",
    }
    prepared = service.capture(command, {"values": {"symbol": "XAUUSD"}})
    pure_window = service.window_from_capture(prepared, command["history_from"], command["history_to"])
    with db.read() as scope:
        assert db.documents("market").list(scope, "snapshot") == []
    with db.write() as tx:
        snapshot = service.commit_capture(prepared, tx)
    window = service.read_window(
        snapshot.to_dict()["snapshot_id"], command["history_from"], command["history_to"]
    ).to_dict()
    assert pure_window.to_dict() == window
    assert window["tick_status"] == "available"
    assert len(window["bars"]) == 7 and len(window["ticks"]) == 8
    assert window["bar_status"]["state"] == "available"
    assert window["coverage"]["complete"] is True
    with db.read() as scope:
        metadata = db.documents("market").get(scope, "history", snapshot.to_dict()["snapshot_id"])
        assert "ticks" not in metadata and "attachment_id" in metadata
    service.close()
    db.close()


def test_history_chunks_keep_boundary_ticks_once_and_adapt_size(tmp_path):
    from datetime import timedelta

    from xau_mt5_bridge.synthetic import SyntheticSource
    from xau_next.domains.market.bridge import BridgeError
    from xau_next.shared.values import parse_utc

    db = Database(tmp_path)
    db.initialize()
    service = MarketService(db, mode="synthetic")
    source = SyntheticSource()
    requests = []

    class LimitedBridge:
        def request(self, operation, params):
            requests.append((params["from"], params["to"]))
            if parse_utc(params["to"]) - parse_utc(params["from"]) > timedelta(minutes=15):
                raise BridgeError("HISTORY_TOO_LARGE", "模拟单段上限")
            return {"result": source.handle(operation, params), "source_identity": source.identity()}

        def close(self):
            pass

    service._bridge = LimitedBridge()
    result = service.fetch_history(
        {"from": "2026-10-12T00:00:00Z", "to": "2026-10-12T01:00:00Z"}, {"values": {"symbol": "XAUUSD"}}
    ).to_dict()
    assert len(result["bars"]) == 60
    assert len(result["ticks"]) == 61
    assert len({t["event_time"] for t in result["ticks"]}) == 61
    assert len(result["raw"]["parts"]) == 4
    assert len(requests) > 4
    service.close()
    db.close()


def test_old_history_replaces_current_m1_series_and_keeps_its_price_basis(tmp_path):
    from dataclasses import replace

    from xau_next.shared.snapshots import Snapshot

    db = Database(tmp_path)
    db.initialize()
    service = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T02:07:00Z")
    command = {
        "include_history": True,
        "history_from": "2026-10-10T02:00:00Z",
        "history_to": "2026-10-10T02:10:00Z",
    }
    prepared = service.capture(command, {"values": {"symbol": "XAUUSD"}})
    historical = {**prepared.history.to_dict(), "price_basis": "ask"}
    prepared = replace(prepared, history=Snapshot.of(historical))
    window = service.window_from_capture(prepared, command["history_from"], command["history_to"]).to_dict()
    m1 = next(series for series in window["series"] if series["timeframe"] == "M1")
    assert m1["bars"] == window["bars"] and len(m1["bars"]) == 10
    assert m1["bars"][0]["open_at"] == "2026-10-10T02:00:00.000Z"
    assert m1["price_basis"] == window["price_basis"] == "ask"
    assert all(
        bar["close_at"] <= "2026-10-10T02:10:00.000Z" for series in window["series"] for bar in series["bars"]
    )
    m5 = next(series for series in window["series"] if series["timeframe"] == "M5")
    assert m5["bars"] == [] and m5["status"]["state"] == "unavailable"
    service.close()
    db.close()


def test_real_mode_rejects_synthetic_bridge_without_fallback(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    command = [sys.executable, str(ROOT / "apps/mt5_bridge/src/xau_mt5_bridge/__main__.py")]
    service = MarketService(db, bridge_command=command, mode="mt5")
    from xau_mt5_bridge.synthetic import SyntheticSource

    source = SyntheticSource()

    class WrongSource:
        def request(self, operation, params):
            return {"source_identity": source.identity(), "result": source.handle(operation, params)}

        def close(self):
            pass

    service._bridge = WrongSource()
    with pytest.raises(BridgeError) as failure:
        service.capture({"include_history": False}, {"values": {"symbol": "XAUUSD"}})
    assert failure.value.code == "SOURCE_IDENTITY_MISMATCH"
    with db.read() as scope:
        assert db.documents("market").list(scope, "snapshot") == []
    service.close()
    db.close()
