import copy
from decimal import Decimal

import pytest
from xau_next.domains.portfolio.public import PortfolioService
from xau_next.platform.db.public import Database
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot


def usage(service, db, plan_id="plan-1", account="account-demo", symbol="XAUUSD"):
    with db.read() as scope:
        return service.entry_usage(plan_id, available_at="2026-10-12T00:00:00Z",
            account_scope_id=account, symbol=symbol, scope=scope).to_dict()


def link_plan(service, reference, plan_id="plan-1", action="link"):
    return service.create_association({"order": reference, "plan_id": plan_id, "action": action,
        "reason": "合成核验", "expected_portfolio_revision": service.get_portfolio().to_dict()["revision"]},
        plan_snapshot={"plan_id": plan_id, "account_scope_id": reference["account_scope_id"], "symbol": "XAUUSD"})


@pytest.mark.parametrize("history_kind", ["closed", "partial", "exit_only"])
def test_execution_between_reads_requires_attribution_and_consumes_once(env, history_kind):
    service, db, _ = env
    facts = [deal("in", "in", "0.01", "100", "0", None),
             deal("out", "out", "0.005" if history_kind == "partial" else "0.01", "101", "1", None)]
    if history_kind == "exit_only":
        facts = facts[1:]
    sync(service, portfolio(order=False), [envelope(facts)])
    assert usage(service, db)["state"] == "UNKNOWN"
    assert usage(service, db, account="other-account")["state"] == "UNUSED"
    assert usage(service, db, symbol="EURUSD")["state"] == "UNUSED"
    ref = {"account_scope_id": "account-demo", "kind": "position", "broker_id": "position-1"}
    association = link_plan(service, ref).to_dict()
    first = usage(service, db)
    assert first["state"] == "USED"
    assert first["evidence"]["association_id"] == association["association_id"]
    assert usage(service, db, plan_id="unrelated-plan")["state"] == "UNUSED"
    link_plan(service, ref, action="unlink")
    sync(service, portfolio("flat-next", order=False), [envelope(facts)])
    assert usage(service, db) == first


@pytest.mark.parametrize("with_history,expected", [(True, "USED"), (False, "UNKNOWN")])
def test_legacy_usage_backfill_appends_without_rewriting_originals(tmp_path, monkeypatch, with_history, expected):
    from xau_next.domains.portfolio.usage import ExecutionUsage
    db = Database(tmp_path / "legacy")
    db.initialize()
    with monkeypatch.context() as patch:
        for method in ("backfill", "execution", "association"):
            patch.setattr(ExecutionUsage, method, lambda *a, **kw: None)
        old = PortfolioService(db)
        pf = sync(old)
        ref = pf["orders"][0]["order"]
        link_plan(old, ref)
        link_plan(old, ref, action="unlink")
        trades = [envelope([deal("in", "in", "0.01", "100", "0", "0"),
                            deal("out", "out", "0.01", "101", "1", "0")])] if with_history else []
        sync(old, portfolio("flat", order=False), trades)
    docs = db.documents("portfolio")
    kinds = ["snapshot", "current", "trade", "deal", "link", "association", "order_trade"]
    with db.read() as scope:
        before = {kind: docs.list(scope, kind, limit=1000) for kind in kinds}
    upgraded = PortfolioService(db)
    assert usage(upgraded, db)["state"] == expected
    with db.read() as scope:
        assert before == {kind: docs.list(scope, kind, limit=1000) for kind in kinds}
    assert usage(PortfolioService(db), db)["state"] == expected
    db.close()


@pytest.fixture
def env(tmp_path):
    clock = ["2026-10-12T02:00:00Z"]
    db = Database(tmp_path / "portfolio-data", clock=lambda: clock[0])
    db.initialize()
    yield PortfolioService(db, clock=lambda: clock[0]), db, clock
    db.close()


def portfolio(id="portfolio-1", *, orders_status="available", order=True, account_scope="account-demo"):
    status = {"state": "available", "observed_at": "2026-10-12T02:00:00Z", "captured_at": "2026-10-12T02:00:00Z",
              "reasons": []}
    return {"snapshot_id": id, "account_scope_id": account_scope, "account_status": status,
        "account": {"currency": "USD", "balance": "100", "equity": "101", "free_margin": "80", "used_margin": "20"},
        "orders_status": {**status, "state": orders_status, "reasons": ["终端读取失败"] if orders_status == "unavailable" else []},
        "orders": [{"order": {"account_scope_id": account_scope, "kind": "position", "broker_id": "position-1"},
                    "symbol": "XAUUSD", "side": "long", "volume_lots": "0.01", "entry_price": "100",
                    "stop_loss": "98.8", "take_profit": "103", "event_time": "2026-10-12T01:00:00Z",
                    "captured_at": "2026-10-12T02:00:00Z", "plan_id": None}] if order else [], "revision": 0}


def deal(id, kind, volume, price, profit, commission, *, event_time=None):
    def money(amount):
        return {"amount": amount, "currency": "USD"} if amount is not None else None
    return {"deal_id": id, "entry_kind": kind,
        "event_time": event_time or ("2026-10-12T01:00:00Z" if kind == "in" else "2026-10-12T01:30:00Z"),
        "volume_lots": volume, "price": price, "realized_profit": money(profit),
        "commission": money(commission), "swap": money("0"), "other_fee": money("0")}


def envelope(deals, id="trade-1", account="account-demo", broker_id="position-1"):
    return {"trade_id": id, "account_scope_id": account, "broker_position_id": broker_id,
            "symbol": "XAUUSD", "side": "long", "opened_at": "2026-10-12T01:00:00Z", "currency": "USD", "deals": deals}


def sync(service, fact=None, trades=()):
    return service.sync_facts(Snapshot.of(fact or portfolio()), trades=tuple(Snapshot.of(t) for t in trades)).to_dict()


def test_ac13_failed_read_retains_known_facts_and_intent_does_not_close(env):
    service, _, _ = env
    first = sync(service)
    broken = portfolio("portfolio-failed", orders_status="unavailable", order=False)
    broken["account_status"] = {**broken["account_status"], "state": "unavailable", "reasons": ["账户读取失败"]}
    broken["account_status"]["observed_at"] = None
    broken["orders_status"]["observed_at"] = None
    broken["account"] = None
    current = sync(service, broken)
    assert current["orders_status"]["state"] == "unavailable"
    assert [{k: v for k, v in x.items() if k != "association_token"} for x in current["orders"]] == [{k: v for k, v in x.items() if k != "association_token"} for x in first["orders"]]
    assert current["orders"][0]["association_token"] != first["orders"][0]["association_token"]
    assert current["account"] == first["account"]
    assert current["account_status"]["observed_at"] == first["account_status"]["observed_at"]
    assert service.get_snapshot("portfolio-failed").to_dict()["account"] is None
    assert service.get_snapshot("portfolio-failed").to_dict()["orders"] == []
    command = {"order": first["orders"][0]["order"], "action": "close", "volume_lots": None,
               "stop_loss": None, "take_profit": None, "note": "计划人工退出，尚未操作。"}
    decision = service.record_decision(command).to_dict()
    assert decision["state"] == "recorded_pending_broker_fact"
    assert service.get_portfolio().to_dict() == current
    # No initial facts also keeps a distinct unavailable state instead of claiming flat.
    assert service.list_trades().to_dict()["items"] == []


def test_ac13_partial_closes_signed_costs_and_weighted_exit(env):
    service, _, _ = env
    entry = deal("entry", "in", "0.01", "100", "0", "0")
    exit1 = deal("exit-1", "out", "0.006", "101", "0.6", "-0.12")
    exit2 = deal("exit-2", "out", "0.004", "102", "0.8", "-0.08", event_time="2026-10-12T01:40:00Z")
    sync(service, trades=[envelope([entry, exit1])])
    partial = service.get_trade("trade-1").to_dict()
    assert partial["trade"]["state"] == "partially_closed"
    assert partial["trade"]["closed_at"] is None
    assert Decimal(partial["trade"]["complete_realized_net"]["amount"]) == Decimal("0.48")
    # History ranges may overlap and omit earlier deals. Merge identities, never add twice.
    sync(service, portfolio("portfolio-2", order=False), [envelope([exit1, exit2])])
    current = service.get_trade("trade-1").to_dict()
    metrics = service.get_trade_metrics("trade-1").to_dict()
    assert current["trade"]["state"] == "closed"
    assert Decimal(current["trade"]["complete_realized_net"]["amount"]) == Decimal("1.2")
    assert current["trade"]["plan_id"] is None
    assert len(current["deals"]) == 3
    assert Decimal(metrics["weighted_exit_price"]) == Decimal("101.4")
    assert metrics["remaining_volume_lots"] == "0.000"
    assert len(service.list_trades().to_dict()["items"]) == 1


def test_ac16_fee_later_completed_keeps_previous_revision(env):
    service, _, clock = env
    entry = deal("entry", "in", "0.01", "100", "0", "0")
    exit1 = deal("exit", "out", "0.01", "101", "1", None)
    sync(service, portfolio(order=False), [envelope([entry, exit1])])
    first = service.get_trade("trade-1").to_dict()["trade"]
    assert first["complete_realized_net"] is None
    assert first["known_realized_subtotal"]["amount"] == "1"
    clock[0] = "2026-10-12T02:10:00Z"
    exit1["commission"] = {"amount": "-0.2", "currency": "USD"}
    sync(service, portfolio("portfolio-2", order=False), [envelope([exit1])])
    current = service.get_trade("trade-1").to_dict()["trade"]
    assert current["fees_complete"] is True
    assert Decimal(current["complete_realized_net"]["amount"]) == Decimal("0.8")
    versions = service.trade_history("trade-1")
    assert len(versions) == 2
    assert versions[0].to_dict()["trade"] == first
    assert versions[0].to_dict()["trade"]["complete_realized_net"] is None


def test_rebates_keep_sign_and_foreign_currency_stays_missing(env):
    service, _, _ = env
    entry = deal("entry", "in", "0.01", "100", "0", "0.05")
    exit1 = deal("exit", "out", "0.01", "101", "1", "-0.2")
    exit1["other_fee"] = {"amount": "-0.1", "currency": "EUR"}
    sync(service, trades=[envelope([entry, exit1])])
    trade = service.get_trade("trade-1").to_dict()["trade"]
    assert Decimal(trade["known_realized_subtotal"]["amount"]) == Decimal("0.85")
    assert trade["complete_realized_net"] is None
    assert "exit.other_fee.currency" in service.get_trade_metrics("trade-1").to_dict()["missing"]


@pytest.mark.parametrize("facts", [
    [deal("exit", "out", "0.01", "101", "1", "0")],
    [deal("entry", "in", "0.01", "100", "0", "0"), deal("reverse", "inout", "0.02", "101", "1", "0")],
    [deal("entry", "in", "0.01", "100", "0", "0"), deal("exit", "out", "0.02", "101", "1", "0")],
])
def test_unexplained_history_and_reversal_remain_unknown(env, facts):
    service, _, _ = env
    sync(service, trades=[envelope(facts)])
    trade = service.get_trade("trade-1").to_dict()["trade"]
    metrics = service.get_trade_metrics("trade-1").to_dict()
    assert trade["state"] == "unknown"
    assert trade["closed_at"] is None
    assert trade["complete_realized_net"] is None
    assert metrics["lifecycle_complete"] is False
    assert metrics["missing"]


def test_deal_identity_dedup_and_original_fact_preserved(env):
    service, _, _ = env
    trades = [envelope([deal("entry", "in", "0.01", "100", "0", "0")])]
    sync(service, trades=trades)
    sync(service, trades=trades)
    assert len(service.trade_history("trade-1")) == 1
    assert len(service.get_trade("trade-1").to_dict()["deals"]) == 1
    collision = envelope(trades[0]["deals"], id="trade-2", broker_id="position-2")
    with pytest.raises(DomainError) as caught:
        sync(service, portfolio("portfolio-2"), [collision])
    assert caught.value.code == "DEAL_IDENTITY_CONFLICT"
    assert len(service.list_trades().to_dict()["items"]) == 1


def test_association_explicit_scope_revision_unlink_and_time(env):
    service, _, clock = env
    original = sync(service)
    reference = original["orders"][0]["order"]
    command = {"order": reference, "plan_id": "plan-1", "action": "link", "reason": "人工核对唯一方案",
               "expected_portfolio_revision": original["revision"]}
    plan = Snapshot.of({"plan": {"plan_id": "plan-1"}, "account_scope_id": "other-account", "symbol": "XAUUSD"})
    with pytest.raises(DomainError) as caught:
        service.create_association(command, plan_snapshot=plan)
    assert caught.value.code == "ACCOUNT_MISMATCH"
    plan = Snapshot.of({"plan": {"plan_id": "plan-1"}, "account_scope_id": "account-demo", "symbol": "XAUUSD"})
    link = service.create_association(command, plan_snapshot=plan).to_dict()
    linked = service.get_portfolio().to_dict()
    assert linked["orders"][0]["plan_id"] == "plan-1"
    assert service.association_at(reference, "2026-10-12T01:59:59Z") is None
    with pytest.raises(DomainError) as caught:
        service.create_association(command, plan_snapshot=plan)
    assert caught.value.code == "VERSION_CONFLICT"
    clock[0] = "2026-10-12T02:05:00Z"
    service.create_association({**command, "action": "unlink", "plan_id": None,
                               "expected_portfolio_revision": linked["revision"]})
    assert service.get_portfolio().to_dict()["orders"][0]["plan_id"] is None
    assert service.association_at(reference, "2026-10-12T02:03:00Z").to_dict()["association_id"] == link["association_id"]
    assert service.association_at(reference, "2026-10-12T02:05:00Z").to_dict()["plan_id"] is None
    assert len(service.list_associations(reference)) == 2


def test_association_available_for_closed_trade_without_current_order(env):
    service, _, _ = env
    facts = [deal("entry", "in", "0.01", "100", "0", "0"), deal("exit", "out", "0.01", "101", "1", "-0.2")]
    state = sync(service, portfolio(order=False), [envelope(facts)])
    command = {"order": {"account_scope_id": "account-demo", "kind": "position", "broker_id": "position-1"},
               "plan_id": "plan-1", "action": "link", "reason": "人工核对历史成交",
               "expected_portfolio_revision": state["revision"]}
    service.create_association(command, plan_snapshot=Snapshot.of({"plan": {"plan_id": "plan-1"},
                               "account_scope_id": "account-demo", "symbol": "XAUUSD"}))
    assert service.get_trade("trade-1").to_dict()["trade"]["plan_id"] == "plan-1"


def test_restart_and_atomic_failure(env):
    service, db, clock = env
    sync(service)
    broken = envelope([deal("entry", "in", "0.01", "100", "0", "0")])
    broken["deals"][0].pop("entry_kind")
    with pytest.raises(DomainError):
        sync(service, portfolio("portfolio-2", order=False), [broken])
    assert service.get_portfolio().to_dict()["snapshot_id"] == "portfolio-1"
    second_db = Database(db.data_root, clock=lambda: clock[0])
    second_db.initialize()
    restarted = PortfolioService(second_db, clock=lambda: clock[0])
    assert restarted.get_portfolio().to_dict() == service.get_portfolio().to_dict()
    second_db.close()


def test_late_snapshot_does_not_replace_newer_facts(env):
    service, _, _ = env
    first = portfolio()
    sync(service, first)
    old = copy.deepcopy(first)
    old["snapshot_id"] = "older-snapshot"
    old["orders"] = []
    for field in ("account_status", "orders_status"):
        old[field]["captured_at"] = "2026-10-12T01:59:00Z"
    assert sync(service, old)["snapshot_id"] == first["snapshot_id"]


def test_account_switch_does_not_reuse_previous_orders(env):
    service, _, _ = env
    sync(service)
    switched = sync(service, portfolio("account-switch", orders_status="unavailable", order=False,
                                      account_scope="account-other"))
    assert switched["orders"] == []
    assert switched["orders_status"]["state"] == "unavailable"


def test_incomplete_read_does_not_remove_missing_objects(env):
    service, _, _ = env
    initial = sync(service)
    partial = sync(service, portfolio("partial", orders_status="incomplete", order=False))
    assert [{k: v for k, v in x.items() if k != "association_token"} for x in partial["orders"]] == [{k: v for k, v in x.items() if k != "association_token"} for x in initial["orders"]]
    assert partial["orders"][0]["association_token"] != initial["orders"][0]["association_token"]
    assert partial["orders_status"]["state"] == "incomplete"
    complete = sync(service, portfolio("complete", order=False))
    assert complete["orders"] == []
    assert complete["orders_status"]["state"] == "available"


def test_associations_same_timestamp_use_commit_sequence(env):
    service, _, clock = env
    current = sync(service)
    reference = current["orders"][0]["order"]
    command = {"order": reference, "plan_id": "plan-1", "action": "link", "reason": "人工核对",
               "expected_portfolio_revision": current["revision"]}
    service.create_association(command, plan_snapshot=Snapshot.of({"plan": {"plan_id": "plan-1"},
                               "account_scope_id": "account-demo", "symbol": "XAUUSD"}))
    current = service.get_portfolio().to_dict()
    service.create_association({**command, "action": "unlink", "plan_id": None,
                               "expected_portfolio_revision": current["revision"]})
    assert service.association_at(reference, clock[0]).to_dict()["plan_id"] is None


def test_same_batch_duplicate_deal_conflict_rolls_back(env):
    service, _, _ = env
    entry = deal("entry", "in", "0.01", "100", "0", "0")
    with pytest.raises(DomainError) as caught:
        sync(service, trades=[envelope([entry, {**entry, "volume_lots": "0.02"}])])
    assert caught.value.code == "DEAL_IDENTITY_CONFLICT"
    assert service.list_trades().to_dict()["items"] == []


def test_truncated_history_does_not_invent_open_time_or_side(env):
    service, _, _ = env
    incomplete = envelope([deal("exit", "out", "0.01", "101", "1", "-0.2")])
    incomplete.update(side=None, opened_at=None, opening_complete=False)
    sync(service, portfolio(order=False), [incomplete])
    result = service.get_trade("trade-1").to_dict()["trade"]
    assert result["side"] is None
    assert result["opened_at"] is None
    assert result["state"] == "unknown"
    assert result["complete_realized_net"] is None


def test_stored_complete_opening_survives_partial_later_read(env):
    service, _, _ = env
    complete = envelope([deal("entry", "in", "0.01", "100", "0", "0")])
    complete["opening_complete"] = True
    sync(service, trades=[complete])
    partial = envelope([deal("exit", "out", "0.01", "101", "1", "-0.2")])
    partial.update(side=None, opened_at=None, opening_complete=False)
    sync(service, portfolio("portfolio-2", order=False), [partial])
    result = service.get_trade("trade-1").to_dict()["trade"]
    assert result["side"] == "long"
    assert result["opened_at"] == "2026-10-12T01:00:00Z"
    assert result["state"] == "closed"
    assert Decimal(result["complete_realized_net"]["amount"]) == Decimal("0.8")


def test_late_old_capture_cannot_remove_later_real_fees(env):
    service, _, _ = env
    entry = deal("entry", "in", "0.01", "100", "0", "0")
    exit1 = deal("exit", "out", "0.01", "101", "1", "-0.2")
    fresh = {**envelope([entry, exit1]), "captured_at": "2026-10-12T02:10:00Z"}
    sync(service, portfolio(order=False), [fresh])
    old = {**envelope([{**exit1, "commission": None}]), "captured_at": "2026-10-12T02:01:00Z"}
    sync(service, portfolio("portfolio-2", order=False), [old])
    assert Decimal(service.get_trade("trade-1").to_dict()["trade"]["complete_realized_net"]["amount"]) == Decimal("0.8")
    assert len(service.trade_history("trade-1")) == 1


def test_review_metadata_binds_explicit_source_and_independent_association(env):
    service, _, clock = env
    facts = [deal("entry", "in", "0.01", "100", "0", "0"), deal("exit", "out", "0.01", "101", "1", "-0.2")]
    known = {**envelope(facts), "source_kind": "synthetic", "market_snapshot_id": "market-1"}
    current = sync(service, portfolio(order=False), [known])
    metrics = service.get_trade_metrics("trade-1").to_dict()
    assert metrics["source_kind"] == "synthetic"
    assert metrics["market_snapshot_id"] == "market-1"
    assert metrics["association_id"] is None
    assert metrics["association_available_at"] is None
    assert metrics["association_revision"] == 0
    assert metrics["order_ref"] == {"account_scope_id": "account-demo", "kind": "position", "broker_id": "position-1"}
    clock[0] = "2026-10-12T02:10:00Z"
    command = {"order": metrics["order_ref"], "action": "link", "plan_id": "plan-1", "reason": "人工核对",
               "expected_portfolio_revision": current["revision"]}
    association = service.create_association(command, plan_snapshot=Snapshot.of({"plan": {"plan_id": "plan-1"},
        "account_scope_id": "account-demo", "symbol": "XAUUSD"})).to_dict()
    linked = service.get_trade_metrics("trade-1").to_dict()
    assert linked["revision"] == metrics["revision"]
    assert linked["available_at"] == metrics["available_at"]
    assert linked["association_id"] == association["association_id"]
    assert linked["association_available_at"] == clock[0]
    assert linked["association_revision"] == 1


def test_missing_trade_source_is_not_inferred_from_account_identity(env):
    service, _, clock = env
    facts = [deal("entry", "in", "0.01", "100", "0", "0"), deal("exit", "out", "0.01", "101", "1", "-0.2")]
    sync(service, portfolio(order=False), [envelope(facts)])
    assert service.get_trade_metrics("trade-1").to_dict()["source_kind"] == "unknown"
    clock[0] = "2026-10-12T02:10:00Z"
    # One freshly confirmed deal cannot relabel the other historical unknown-source deal.
    sync(service, portfolio("portfolio-2", order=False), [{**envelope([facts[1]]), "source_kind": "mt5", "market_snapshot_id": "market-2"}])
    assert service.get_trade_metrics("trade-1").to_dict()["source_kind"] == "unknown"
    with pytest.raises(DomainError) as caught:
        sync(service, portfolio("portfolio-3", order=False), [{**envelope([facts[1]]), "source_kind": "synthetic"}])
    assert caught.value.code == "SOURCE_MISMATCH"
    with pytest.raises(DomainError) as caught:
        sync(service, portfolio("portfolio-4", order=False), [{**envelope([{**facts[1], "deal_id": "new-foreign-source"}]),
                                                               "source_kind": "synthetic"}])
    assert caught.value.code == "SOURCE_MISMATCH"
