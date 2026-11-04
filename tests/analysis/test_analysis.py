import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from xau_next.domains.analysis.public import AnalysisService, market_scope_key
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
MARKET = next(item["value"] for item in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8"))
              if item["schema"] == "MarketSnapshot")


@pytest.fixture
def env(tmp_path):
    clock = ["2026-10-12T02:07:00Z"]
    db = Database(tmp_path / "data", clock=lambda: clock[0])
    db.initialize()
    files = AttachmentStore(db)
    service = AnalysisService(db, files, SCHEMA, clock=lambda: clock[0])
    yield service, db, clock
    db.close()


def create(service, *, market=None, portfolio=None, experience=None, base=None):
    market = market or copy.deepcopy(MARKET)
    status = {"state": "available", "observed_at": market["market_cutoff"],
              "captured_at": market["captured_at"], "reasons": []}
    portfolio = portfolio or {"snapshot_id": "portfolio-demo", "account_scope_id": market["account_scope_id"],
        "account_status": status, "account": None, "orders_status": status, "orders": [], "revision": 1}
    command = {"kind": "update" if base else "new", "market_snapshot_id": market["snapshot_id"],
               "forecast_id": base["forecast_id"] if base else None,
               "base_version_id": base["version_id"] if base else None, "horizon_minutes": [180, 360, 720]}
    return service.create_request(command, market=Snapshot.of(market), portfolio=Snapshot.of(portfolio),
        settings=Snapshot.of({"revision": 1, "values": {"default_volume_lots": None, "per_trade_loss_cap": None,
                                                       "cost_assumption": None}}),
        experience=Snapshot.of(experience) if experience else None).to_dict()


def answer_for(request):
    answer = copy.deepcopy(FIXTURE)
    for field in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
        answer[field] = request[field]
    answer["evidence_refs"] = [request["market_snapshot_id"]]
    answer["horizons"] = [h for h in answer["horizons"] if h["end_at"] > request["market_cutoff"]]
    for h in answer["horizons"]:
        h["phases"][0]["start_at"] = request["market_cutoff"]
    if request["kind"] == "update":
        answer["plans"] = []
    return answer


def prepare(service, request, answer=None, raw=None):
    return service.prepare_import({"request_id": request["request_id"],
        "raw_text": raw if raw is not None else json.dumps(answer if answer is not None else answer_for(request), ensure_ascii=False),
        "source": "manual_paste", "provider": None, "model_id": None})


def commit(service, db, prepared):
    try:
        with db.write() as uow:
            return service.commit_import(prepared, uow=uow)
    except DomainError as error:
        service.record_failed_attempt(prepared, error)
        raise


def test_ac03_04_09_request_import_restart(env):
    service, db, clock = env
    request = create(service)
    assert service.list_forecasts().to_dict()["items"] == []
    assert service.list_requests(state="awaiting_answer").to_dict()["items"][0] == request
    assert "# 全部字段说明" in request["packet_text"]
    assert "proposed_volume_lots" in request["packet_text"]
    assert "budget_and_cost" in request["packet_text"]
    raw = "  " + json.dumps(answer_for(request), ensure_ascii=False) + "\n"
    result = commit(service, db, prepare(service, request, raw=raw))
    version = result.version.to_dict()
    assert version["raw_text"] == raw
    assert version["available_at"] == clock[0]
    assert len(result.plans) == 1
    assert service.get_request(request["request_id"]).to_dict()["state"] == "imported"
    other = Database(db.data_root, clock=lambda: clock[0])
    other.initialize()
    restarted = AnalysisService(other, AttachmentStore(other), SCHEMA, clock=lambda: clock[0])
    assert restarted.get_version(version["version_id"]).to_dict() == version
    assert len(restarted.list_forecasts().to_dict()["items"]) == 1
    other.close()


@pytest.mark.parametrize("variant", json.loads((ROOT / "fixtures/answer-variants.json").read_text(encoding="utf-8")),
                         ids=lambda variant: variant["id"])
def test_ac05_fixture_variants(env, variant):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    for change in variant["mutations"]:
        parent = answer
        for part in change["path"][:-1]:
            parent = parent[part]
        if change["op"] == "remove":
            del parent[change["path"][-1]]
        else:
            parent[change["path"][-1]] = change["value"]
    prepared = prepare(service, request, answer)
    expected = variant["expected_business_result"]
    if expected == "ACCEPT_WITH_EVALUATION_GAP":
        assert commit(service, db, prepared).version.to_dict()["answer"]["plans"][0]["volume_lots"] is None
    else:
        with pytest.raises(DomainError) as caught:
            commit(service, db, prepared)
        assert caught.value.code == expected
        assert caught.value.field_errors
        assert service.list_forecasts().to_dict()["items"] == []
        attempts = service.list_import_attempts(request["request_id"])
        assert len(attempts) == 1
        assert attempts[0].to_dict()["raw_text"] == prepared.raw_text


def test_ac06_permanent_idempotency_and_changed_bytes(env):
    service, db, clock = env
    request = create(service)
    prepared = prepare(service, request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: commit(service, db, prepared), [1, 2]))
    assert first.version == second.version
    assert {first.repeated, second.repeated} == {True, False}
    clock[0] = "2026-10-22T02:07:00Z"
    assert commit(service, db, prepare(service, request)).repeated
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepare(service, request, raw=prepared.raw_text + " "))
    assert caught.value.status == 409
    assert caught.value.code == "REQUEST_CONTENT_CONFLICT"
    assert len(service.list_versions(request["forecast_id"]).to_dict()["items"]) == 1


def test_ac07_08_update_original_end_and_concurrency(env):
    service, db, clock = env
    request = create(service)
    first = commit(service, db, prepare(service, request)).version.to_dict()
    market = copy.deepcopy(MARKET)
    market.update(snapshot_id="snapshot-update", market_cutoff="2026-10-12T06:00:00Z")
    clock[0] = "2026-10-12T06:07:00Z"
    update1, update2 = [create(service, market=market, base=first) for _ in range(2)]
    assert update1["window_start"] == request["window_start"]
    assert update1["window_end"] == request["window_end"]
    prepared1, prepared2 = prepare(service, update1), prepare(service, update2)
    second = commit(service, db, prepared1).version.to_dict()
    assert [h["horizon_minutes"] for h in second["answer"]["horizons"]] == [360, 720]
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepared2)
    assert caught.value.code == "VERSION_CONFLICT"
    assert service.get_request(update2["request_id"]).to_dict()["state"] == "superseded"
    market.update(snapshot_id="snapshot-late", market_cutoff="2026-10-12T13:00:00Z")
    late = create(service, market=market, base=second)
    clock[0] = "2026-10-12T14:00:00Z"
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepare(service, late))
    assert caught.value.code == "EXPIRED_REQUEST"


def test_ac09_atomic_rollback_leaves_no_version(env):
    service, db, _ = env
    request = create(service)
    prepared = prepare(service, request)
    with pytest.raises(RuntimeError), db.write() as uow:
        service.commit_import(prepared, uow=uow)
        raise RuntimeError("simulate plans registration failure")
    assert service.list_forecasts().to_dict()["items"] == []
    assert service.get_request(request["request_id"]).to_dict()["state"] == "awaiting_answer"
    assert not commit(service, db, prepared).repeated


def test_ac15_available_time_and_experience_cutoff(env):
    service, db, _ = env
    request = create(service, experience={"digest_id": "future-digest", "available_at": "2026-10-12T02:10:00Z",
                                          "review_refs": ["future-review"]})
    assert "future-digest" not in request["packet_text"]
    version = commit(service, db, prepare(service, request)).version.to_dict()
    assert service.version_at(request["forecast_id"], "2026-10-12T02:03:00Z") is None
    assert service.version_at(request["forecast_id"], "2026-10-12T02:07:00Z").to_dict() == version
    earlier = create(service, experience={"digest_id": "past-digest", "available_at": "2026-10-12T01:59:00Z",
                                         "review_refs": ["past-review"], "unknown": 2, "total": 5})
    assert "past-digest" in earlier["packet_text"]


@pytest.mark.parametrize(("path", "value", "code"), [
    (("horizons", 0, "phases", 0, "start_at"), "2026-10-12T02:01:00Z", "TIME_WINDOW_INVALID"),
    (("horizons", 0, "phases", 0, "start_at"), "2026-99-99T99:99:99Z", "SCHEMA_INVALID"),
    (("plans", 0, "valid_from"), "2026-10-12T03:00:00Z", "TIME_WINDOW_INVALID"),
    (("plans", 0, "entry_deadline"), "2026-10-12T04:01:00Z", "TIME_WINDOW_INVALID"),
    (("plans", 0, "exit_deadline"), "2026-10-12T14:01:00Z", "TIME_WINDOW_INVALID"),
    (("plans", 0, "volume_lots"), "0.015", "VOLUME_GRID_INVALID"),
    (("plans", 0, "entry", "zone", "high"), "99.00", "PRICE_GEOMETRY_INVALID"),
    (("plans", 0, "activation", "price"), "101.001", "PRICE_GRID_INVALID"),
    (("horizons", 0, "expected_range", "high"), "104.001", "PRICE_GRID_INVALID"),
    (("evidence_refs",), ["fabricated-evidence"], "EVIDENCE_REF_INVALID"),
])
def test_field_level_business_boundaries(env, path, value, code):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    parent = answer
    for item in path[:-1]:
        parent = parent[item]
    parent[path[-1]] = value
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepare(service, request, answer))
    assert caught.value.code == code
    assert caught.value.field_errors


def test_duplicate_role_and_json_keys_rejected(env):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    answer["plans"].append({**copy.deepcopy(answer["plans"][0]), "plan_key": "backup"})
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepare(service, request, answer))
    assert caught.value.code == "PLAN_IDENTITY_INVALID"
    raw = json.dumps(answer_for(request)).replace('"summary":', '"summary":"first", "summary":')
    with pytest.raises(DomainError) as caught:
        commit(service, db, prepare(service, request, raw=raw))
    assert caught.value.code == "SCHEMA_INVALID"


def test_order_coverage_actions_and_positive_stop(env):
    service, db, _ = env
    order_ref = {"account_scope_id": "account-demo", "kind": "position", "broker_id": "ticket-1"}
    order = {"order": order_ref, "symbol": "XAUUSD", "side": "long", "volume_lots": "0.03",
             "entry_price": "100", "stop_loss": "99", "take_profit": "103",
             "event_time": "2026-10-12T01:00:00Z", "captured_at": "2026-10-12T02:00:01Z", "plan_id": None}
    request = create(service, portfolio={"snapshot_id": "orders-1", "account_scope_id": "account-demo",
        "account_status": {}, "account": None, "orders_status": {}, "orders": [order], "revision": 1})
    answer = answer_for(request)
    assert prepare(service, request, answer).error.code == "ORDER_COVERAGE_INVALID"
    advice = {"order": order_ref, "action": "cancel_pending", "reason": "合成测试",
              "proposed_stop_loss": None, "proposed_take_profit": None, "proposed_volume_lots": None,
              "valid_until": None}
    answer["order_advice"] = [advice]
    assert prepare(service, request, answer).error.code == "ORDER_ACTION_INVALID"
    advice.update(action="reduce", proposed_volume_lots="0.03")
    assert prepare(service, request, answer).error.code == "ORDER_ACTION_INVALID"
    advice.update(action="modify_sl_tp", proposed_volume_lots=None, proposed_stop_loss="101.00")
    # A profitable stop on an existing position is independent of new-entry geometry.
    assert commit(service, db, prepare(service, request, answer)).version.to_dict()["answer"]["order_advice"] == [advice]


def test_market_entry_without_precise_price_stays_unknown(env):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    answer["plans"][0]["entry"] = {"mode": "market", "price": None, "zone": None}
    assert commit(service, db, prepare(service, request, answer)).plans[0].to_dict()["definition"]["entry"]["price"] is None


def test_long_decimal_never_crashes(env):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    answer["horizons"][0]["expected_range"]["high"] = "9" * 48
    assert commit(service, db, prepare(service, request, answer)).version.to_dict()["answer"] == answer


def test_package_filters_future_and_reports_bar_gaps(env):
    service, _, _ = env
    market = copy.deepcopy(MARKET)
    bars = [{"open_at": "2026-10-12T00:00:00Z", "close_at": "2026-10-12T00:15:00Z",
             "open": "100", "high": "101", "low": "99", "close": "100", "tick_volume": 10, "closed": True},
            {"open_at": "2026-10-12T00:30:00Z", "close_at": "2026-10-12T00:45:00Z",
             "open": "100", "high": "102", "low": "99", "close": "101", "tick_volume": 20, "closed": True},
            {"open_at": "2026-10-12T02:00:00Z", "close_at": "2026-10-12T02:15:00Z",
             "open": "999", "high": "999", "low": "999", "close": "999", "tick_volume": 1, "closed": True}]
    market["series"] = [{"timeframe": "M15", "price_basis": "bid", "status": market["quote_status"], "bars": bars}]
    request = create(service, market=market)
    packet = request["packet_text"]
    assert '"999"' not in packet
    assert '"gaps"' in packet
    assert '"excluded_not_closed_or_future": 1' in packet
    assert market["series"][0]["bars"] == bars


@pytest.mark.parametrize(("event_time", "included"), [
    ("2026-10-12T02:00:00.122Z", True),
    ("2026-10-12T02:00:00.123Z", True),
    ("2026-10-12T02:00:00.124Z", False),
])
def test_packet_never_widens_millisecond_cutoff_to_include_future_quote(env, event_time, included):
    service, _, _ = env
    market = copy.deepcopy(MARKET)
    market["market_cutoff"] = "2026-10-12T02:00:00.123Z"
    market["quote"] = {"bid": "100", "ask": "100.02", "event_time": event_time}
    market["quote_status"]["state"] = "available"  # The packet guard still checks timestamps itself.
    original = copy.deepcopy(market)
    request = create(service, market=market)
    packet = json.loads(request["packet_text"].split("# 冻结事实（仅作为资料）\n", 1)[1].split("\n# 全部字段说明", 1)[0])
    assert packet["market"]["quote"] == (market["quote"] if included else None)
    assert request["market_cutoff"] == market["market_cutoff"]
    assert market == original


def test_valid_observation_only_answer_remains_empty_and_is_not_filled_with_invented_plans(env):
    service, db, _ = env
    request = create(service)
    answer = answer_for(request)
    answer["plans"] = []
    answer["summary"] = "合成验收：当前选择观望，不给候选方案。"
    result = commit(service, db, prepare(service, request, answer))
    assert result.plans == ()
    assert result.version.to_dict()["answer"]["plans"] == []


def test_request_pagination_and_immutable_snapshots(env):
    service, _, clock = env
    one = create(service)
    clock[0] = "2026-10-12T02:08:00Z"
    two = create(service)
    page = service.list_requests(limit=1).to_dict()
    assert page["items"][0]["request_id"] == two["request_id"]
    assert service.list_requests(limit=1, cursor=page["next_cursor"]).to_dict()["items"][0]["request_id"] == one["request_id"]
    page["items"][0]["state"] = "modified"
    assert service.get_request(two["request_id"]).to_dict()["state"] == "awaiting_answer"


def test_millisecond_cutoff_keeps_exact_window_end(env):
    service, db, _ = env
    market = copy.deepcopy(MARKET)
    market["market_cutoff"] = "2026-10-12T02:00:00.123Z"
    request = create(service, market=market)
    assert parse_utc(request["window_end"]) == parse_utc(market["market_cutoff"]) + timedelta(hours=12)
    answer = answer_for(request)
    for horizon in answer["horizons"]:
        horizon["end_at"] = (parse_utc(request["window_start"]) + timedelta(minutes=horizon["horizon_minutes"])).isoformat().replace("+00:00", "Z")
        horizon["phases"][0]["end_at"] = horizon["end_at"]
    answer["plans"][0]["valid_from"] = market["market_cutoff"]
    assert commit(service, db, prepare(service, request, answer)).version.to_dict()["answer"] == answer


@pytest.mark.parametrize(("field", "value"), [("account_scope_id", "another-account"),
                                             ("symbol", "EURUSD"), ("source_kind", "mt5")])
def test_update_cannot_change_base_source(env, field, value):
    service, db, clock = env
    request = create(service)
    base = commit(service, db, prepare(service, request)).version.to_dict()
    clock[0] = "2026-10-12T03:07:00Z"
    market = {**copy.deepcopy(MARKET), "snapshot_id": "source-changed", "market_cutoff": "2026-10-12T03:00:00Z", field: value}
    with pytest.raises(DomainError) as caught:
        create(service, market=market, base=base)
    assert caught.value.code == "SOURCE_MISMATCH"
    assert caught.value.status == 409


def test_latest_forecast_is_source_indexed_and_wire_clean(env, monkeypatch):
    service, db, clock = env
    request = create(service)
    commit(service, db, prepare(service, request))
    clock[0] = "2026-10-12T02:08:00Z"
    other_market = {**copy.deepcopy(MARKET), "snapshot_id": "other-source", "source_kind": "mt5"}
    other_request = create(service, market=other_market)
    commit(service, db, prepare(service, other_request))
    calls = []
    original_list = service._docs.list

    def indexed_list(scope, kind, **kwargs):
        calls.append((kind, kwargs))
        return original_list(scope, kind, **kwargs)

    monkeypatch.setattr(service._docs, "list", indexed_list)
    latest = service.latest_for_market(Snapshot.of(MARKET)).to_dict()
    assert latest["forecast_id"] == request["forecast_id"]
    assert "account_scope_id" not in latest
    assert "source_kind" not in latest
    assert calls == [("forecast", {"parent_id": market_scope_key(MARKET), "limit": 1})]
    assert service.latest_for_market(other_market).to_dict()["forecast_id"] == other_request["forecast_id"]
    with db.read() as scope:
        internal = db.documents("analysis").get(scope, "forecast", request["forecast_id"])
        assert [internal[key] for key in ("account_scope_id", "symbol", "source_kind")] == ["account-demo", "XAUUSD", "synthetic"]


def test_early_unindexed_forecast_is_retained_and_reindexed_on_update(env):
    service, db, clock = env
    request = create(service)
    base = commit(service, db, prepare(service, request)).version.to_dict()
    docs = db.documents("analysis")
    with db.write() as uow:
        old = docs.get(uow, "forecast", request["forecast_id"])
        payload = {key: value for key, value in old.items()
                   if key not in ("account_scope_id", "symbol", "source_kind", "id", "revision", "recorded_at")}
        docs.update(uow, "forecast", old["id"], payload, old["revision"], parent_id=None)
    assert service.latest_for_market(MARKET).to_dict()["forecast_id"] == request["forecast_id"]
    assert service.latest_for_market({**MARKET, "account_scope_id": "other"}) is None
    clock[0] = "2026-10-12T03:07:00Z"
    market = {**MARKET, "snapshot_id": "updated-source", "market_cutoff": "2026-10-12T03:00:00Z"}
    update = create(service, market=market, base=base)
    new = commit(service, db, prepare(service, update)).version.to_dict()
    assert new["sequence"] == 2
    with db.read() as scope:
        assert docs.list(scope, "forecast", parent_id=market_scope_key(market))[0]["id"] == request["forecast_id"]
        assert docs.list(scope, "forecast", parent_is_null=True) == []


def test_awaiting_query_excludes_large_imported_archive_before_parsing(env, monkeypatch):
    service, db, clock = env
    awaiting = create(service)
    clock[0] = "2026-10-12T02:08:00Z"
    # A synthetic archive projection: large saved packets are intentionally newer
    # than the pending request, reproducing the former all-history pagination scan.
    archive = {**awaiting, "state": "imported", "packet_text": "合成历史包" + "x" * 24000}
    with db.write() as uow:
        for index in range(1000):
            id = f"archived-request-{index:04d}"
            db.documents("analysis").insert(uow, "request", id, {**archive, "request_id": id}, state="imported")
    calls, parsed = [], []
    original_list, original_snapshot = service._docs.list, service._request_snapshot

    def query(scope, kind, **kwargs):
        calls.append(kwargs.copy())
        return original_list(scope, kind, **kwargs)

    def snapshot(scope, row):
        parsed.append(row["request_id"])
        return original_snapshot(scope, row)

    monkeypatch.setattr(service._docs, "list", query)
    monkeypatch.setattr(service, "_request_snapshot", snapshot)
    page = service.list_requests(state="awaiting_answer").to_dict()
    assert [item["request_id"] for item in page["items"]] == [awaiting["request_id"]]
    assert len(calls) == 1
    assert calls[0]["state"] == "awaiting_answer"
    assert parsed == [awaiting["request_id"]]
    assert page["next_cursor"] is None
    first = service.list_requests(state="imported", limit=2).to_dict()
    second = service.list_requests(state="imported", limit=2, cursor=first["next_cursor"]).to_dict()
    assert [item["request_id"] for item in first["items"]] == ["archived-request-0999", "archived-request-0998"]
    assert [item["request_id"] for item in second["items"]] == ["archived-request-0997", "archived-request-0996"]
    assert calls[-1]["state"] == "imported"
    all_page = service.list_requests(limit=2).to_dict()
    assert len(all_page["items"]) == 2
    assert calls[-1]["state"] is None
    clock[0] = "2026-10-12T14:00:00Z"
    expired = service.list_requests(state="expired").to_dict()
    assert [item["request_id"] for item in expired["items"]] == [awaiting["request_id"]]
    assert calls[-1]["state"] == "awaiting_answer"
    assert service.list_requests(state="superseded").to_dict()["items"] == []
    assert calls[-1]["state"] == "awaiting_answer"
