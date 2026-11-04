"""SYNTHETIC: only changed facts for the same source/window unblock a review."""
from copy import deepcopy

from xau_next.domains.market.public import MarketService
from xau_next.platform.db.public import Database


def test_history_content_token_ignores_capture_metadata_and_is_source_window_bound(tmp_path, monkeypatch):
    db = Database(tmp_path); db.initialize()
    service = MarketService(db, mode="synthetic", clock=lambda: "2026-10-12T02:07:00Z")
    command = {"include_history": True, "history_from": "2026-10-12T02:00:00Z",
               "history_to": "2026-10-12T02:04:00Z"}
    settings = {"values": {"symbol": "XAUUSD"}}
    try:
        first = service.capture(command, settings)
        with db.write() as tx:
            service.commit_capture(first, tx)
        snapshot = first.market.to_dict()["snapshot_id"]
        def token(start=command["history_from"], end=command["history_to"]):
            with db.read() as scope:
                return service.review_evidence_token(snapshot, start, end, scope=scope)
        original = token()
        assert original and token(end="2026-10-12T02:03:00Z") is None
        second = service.capture(command, settings)
        assert first.market.to_dict()["snapshot_id"] != second.market.to_dict()["snapshot_id"]
        assert first.review_evidence == second.review_evidence
        with db.write() as tx:
            service.commit_capture(second, tx)
        assert token() == original
        # Ordinary live observations have no historical content index and cannot wake old work.
        with db.write() as tx:
            service.commit_capture(service.capture({}, settings), tx)
        assert token() == original
        request = service._bridge.request
        def changed_history(operation, params):
            response = request(operation, params)
            if operation == "history":
                response["result"]["bars"][0]["high"] = "9999"
            return response
        monkeypatch.setattr(service._bridge, "request", changed_history)
        third = service.capture(command, settings)
        with db.write() as tx:
            service.commit_capture(third, tx)
        assert token() != original
        other = deepcopy(first.market.to_dict()); other["account_scope_id"] = "another-account"
        assert service._review_evidence(other, first.history.to_dict())["key"] != first.review_evidence["key"]
    finally:
        service.close(); db.close()
