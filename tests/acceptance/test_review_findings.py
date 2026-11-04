"""Focused regression reproductions from final integration review; all data synthetic."""

import copy
import json
from pathlib import Path

import pytest
from xau_next.bootstrap import create_app
from xau_next.shared.snapshots import Snapshot
from xau_next.workflows.service import Workflows

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workflow(tmp_path):
    clock = ["2026-10-12T02:00:00Z"]
    app = create_app(
        data_root=tmp_path,
        token="review-only-synthetic-token",
        source="synthetic",
        clock=lambda: clock[0],
        start_worker=False,
    )
    value = app.state.workflows
    yield value, clock
    value.stop()
    value.db.close()


def test_margin_calculation_is_bound_to_plan_market_symbol(workflow, monkeypatch):
    workflows, _ = workflow
    market = next(
        item["value"]
        for item in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8"))
        if item["schema"] == "MarketSnapshot"
    )
    settings = workflows.settings.get_snapshot().to_dict()
    settings["values"]["symbol"] = "EURUSD"
    saved = workflows.settings.update_settings(
        {"expected_revision": settings["revision"], "values": settings["values"]}
    ).to_dict()
    status = {
        "state": "available",
        "observed_at": "2026-10-12T02:00:00Z",
        "captured_at": "2026-10-12T02:00:00Z",
        "reasons": [],
    }
    workflows.portfolio.sync_facts(
        Snapshot.of(
            {
                "snapshot_id": "portfolio-review",
                "account_scope_id": market["account_scope_id"],
                "account_status": status,
                "account": {
                    "currency": "USD",
                    "balance": "100",
                    "equity": "100",
                    "free_margin": "100",
                    "used_margin": "0",
                },
                "orders_status": status,
                "orders": [],
                "revision": 1,
            }
        )
    )
    definition = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))[
        "plans"
    ][0]
    with workflows.db.write() as tx:
        workflows.plans.register(
            [
                Snapshot.of(
                    {
                        "plan_id": "plan-review",
                        "version_id": "version-review",
                        "available_at": "2026-10-12T02:00:00Z",
                        "definition": definition,
                    }
                )
            ],
            uow=tx,
            market_identity=market,
        )
    monkeypatch.setattr(workflows.market, "get_snapshot", lambda *args, **kwargs: Snapshot.of(market))
    calls = []

    def calculation(kind, command, selected_settings):
        symbol = command.get("symbol", selected_settings.to_dict()["values"]["symbol"])
        calls.append(symbol)
        return Snapshot.of(
            {
                "amount": "0.01",
                "currency": "USD",
                "source_kind": "synthetic",
                "account_scope_id": market["account_scope_id"],
                "symbol": symbol,
                "side": command["side"],
                "volume_lots": command["volume_lots"],
                "entry_price": command["entry_price"],
                "captured_at": "2026-10-12T02:00:00Z",
            }
        )

    monkeypatch.setattr(workflows.market, "calculation", calculation)
    _, result = workflows.dispatch(
        "evaluatePlan",
        {
            "market_snapshot_id": market["snapshot_id"],
            "settings_revision": saved["revision"],
            "entry_price_override": None,
            "volume_lots_override": None,
            "sizing_mode": "fixed_volume",
        },
        {"plan_id": "plan-review"},
        "review-symbol-test",
        "/v1/plans/plan-review/evaluations",
    )
    assert calls == ["XAUUSD"], "Margin must use the selected plan/market symbol, not mutable settings.symbol"
    assert result["margin_required"] == "0.01"


def configure_due_forecast(workflows, monkeypatch):
    forecast = {"forecast_id": "forecast-review", "current_version_id": "version-review"}
    version = {"version_id": "version-review", "forecast_id": "forecast-review", "answer": {"market_cutoff": "2026-10-12T00:00:00Z", "horizons": [{"horizon_minutes": 60, "end_at": "2026-10-12T01:00:00Z"}]}}
    monkeypatch.setattr(
        workflows.analysis,
        "list_forecasts",
        lambda **kwargs: Snapshot.of({"items": [forecast], "next_cursor": None}),
    )
    monkeypatch.setattr(workflows.analysis, "get_version", lambda *args, **kwargs: Snapshot.of(version))
    monkeypatch.setattr(workflows.plans, "list_plans", lambda *args, **kwargs: ())


def test_retryable_due_review_is_requeued_after_restart(workflow, monkeypatch):
    workflows, clock = workflow
    configure_due_forecast(workflows, monkeypatch)
    workflows.enqueue_due_reviews()
    lease = workflows.jobs.claim("review-test")
    assert lease is not None
    workflows.jobs.fail(
        lease, {"code": "BRIDGE_TEMPORARILY_UNAVAILABLE", "detail": "合成瞬时读取失败", "retryable": True}
    )
    clock[0] = "2026-10-12T03:00:00Z"
    restarted = Workflows(
        db=workflows.db,
        settings=workflows.settings,
        analysis=workflows.analysis,
        plans=workflows.plans,
        market=workflows.market,
        portfolio=workflows.portfolio,
        review=workflows.review,
        jobs=workflows.jobs,
        events=workflows.events,
        idempotency=workflows.idempotency,
        contract_version=workflows.contract_version,
        clock=workflows.clock,
    )
    restarted.enqueue_due_reviews()
    assert workflows.jobs.claim("recovered-review-test") is not None, (
        "A retryable failure is not completed due work"
    )


def test_unassociated_closed_trade_gets_automatic_review_without_forecasts(workflow, monkeypatch):
    workflows, clock = workflow
    status = {
        "state": "available",
        "observed_at": "2026-10-12T02:00:00Z",
        "captured_at": "2026-10-12T02:00:00Z",
        "reasons": [],
    }
    money = lambda value: {"amount": value, "currency": "USD"}
    trade = {
        "trade_id": "trade-review",
        "account_scope_id": "account-demo",
        "broker_position_id": "position-review",
        "symbol": "XAUUSD",
        "side": "long",
        "opened_at": "2026-10-12T01:00:00Z",
        "currency": "USD",
        "deals": [
            {
                "deal_id": "deal-in",
                "entry_kind": "in",
                "event_time": "2026-10-12T01:00:00Z",
                "volume_lots": "0.01",
                "price": "100",
                "realized_profit": money("0"),
                "commission": money("0"),
                "swap": money("0"),
                "other_fee": money("0"),
            },
            {
                "deal_id": "deal-out",
                "entry_kind": "out",
                "event_time": "2026-10-12T01:30:00Z",
                "volume_lots": "0.01",
                "price": "101",
                "realized_profit": money("1"),
                "commission": money("0"),
                "swap": money("0"),
                "other_fee": money("0"),
            },
        ],
    }
    workflows.portfolio.sync_facts(
        Snapshot.of(
            {
                "snapshot_id": "portfolio-review",
                "account_scope_id": "account-demo",
                "account_status": status,
                "account": {
                    "currency": "USD",
                    "balance": "101",
                    "equity": "101",
                    "free_margin": "101",
                    "used_margin": "0",
                },
                "orders_status": status,
                "orders": [],
                "revision": 1,
            }
        ),
        trades=(Snapshot.of(trade),),
    )
    assert workflows.portfolio.get_trade("trade-review").to_dict()["trade"]["state"] == "closed"
    workflows.enqueue_due_reviews()
    lease = workflows.jobs.claim("automatic-trade-review")
    assert lease is not None, (
        "Actual trades require review even without forecast/plan association"
    )
    workflows.run_reviews(lease)
    assert workflows.enqueue_due_reviews() == 0
    clock[0] = "2026-10-12T02:10:00Z"
    before = workflows.portfolio.get_trade_metrics("trade-review").to_dict()["revision"]
    refreshed_portfolio = workflows.portfolio.get_portfolio().to_dict()
    refreshed_portfolio["snapshot_id"] = "portfolio-sample-2"
    workflows.portfolio.sync_facts(Snapshot.of(refreshed_portfolio),
                                  trades=(Snapshot.of({**trade, "captured_at": clock[0]}),))
    assert workflows.portfolio.get_trade_metrics("trade-review").to_dict()["revision"] == before
    metrics = workflows.portfolio.get_trade_metrics
    def resampled_metrics(*args, **kwargs):
        value = metrics(*args, **kwargs).to_dict()
        value.update(revision=value["revision"] + 100, available_at=clock[0])
        return Snapshot.of(value)
    monkeypatch.setattr(workflows.portfolio, "get_trade_metrics", resampled_metrics)
    assert workflows.enqueue_due_reviews() == 0, "sampling revisions are not changed trading facts"
    calls = []
    compute = workflows._compute
    def observe(function, *args, **kwargs):
        calls.append(function.__name__)
        return compute(function, *args, **kwargs)
    monkeypatch.setattr(workflows, "_compute", observe)
    with workflows.db.write() as tx:
        workflows.jobs.enqueue(tx, "review_refresh", {"forecast_id": None, "through": clock[0]})
    assert workflows.process_once()
    assert calls == [], "manual full scan must skip unchanged trades too"
    # A real fee correction is evaluated once, including a later return to the earlier facts.
    for minute, fee in ((11, "-1"), (12, "0")):
        clock[0] = f"2026-10-12T02:{minute}:00Z"
        corrected = copy.deepcopy(trade)
        corrected["captured_at"] = clock[0]
        corrected["deals"][1]["commission"] = money(fee)
        refreshed_portfolio["snapshot_id"] = f"portfolio-fee-{minute}"
        workflows.portfolio.sync_facts(Snapshot.of(refreshed_portfolio), trades=(Snapshot.of(corrected),))
        assert workflows.enqueue_due_reviews() == 1
        assert workflows.process_once()
        assert workflows.enqueue_due_reviews() == 0
    assert calls == ["compute_actual", "compute_actual"]


def test_workspace_does_not_present_previous_account_evaluation_as_current(workflow, monkeypatch):
    workflows, _ = workflow
    original = next(
        item["value"]
        for item in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8"))
        if item["schema"] == "MarketSnapshot"
    )
    current = {**copy.deepcopy(original), "snapshot_id": "market-account-b", "account_scope_id": "account-b"}
    forecast = {
        "forecast_id": "forecast-a",
        "current_version_id": "version-a",
        "window_start": "2026-10-12T02:00:00Z",
        "window_end": "2026-10-12T14:00:00Z",
        "summary": "账户 A 的历史分析",
        "available_at": "2026-10-12T02:00:00Z",
        "revision": 1,
    }
    answer = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    answer["market_snapshot_id"] = original["snapshot_id"]
    version = {"version_id": "version-a", "forecast_id": "forecast-a", "answer": answer}
    old_plan = {
        "plan_id": "plan-a",
        "version_id": "version-a",
        "lifecycle": "ACTIVE",
        "definition": answer["plans"][0],
    }
    old_evaluation = {
        "evaluation_id": "evaluation-a",
        "plan_id": "plan-a",
        "market_snapshot_id": original["snapshot_id"],
        "budget_check": {"state": "satisfied", "reasons": []},
        "margin_check": {"state": "satisfied", "reasons": []},
    }
    monkeypatch.setattr(workflows.market, "latest", lambda **kwargs: Snapshot.of(current))
    monkeypatch.setattr(workflows.market, "get_snapshot", lambda *args, **kwargs: Snapshot.of(original))
    monkeypatch.setattr(
        workflows.analysis,
        "list_forecasts",
        lambda **kwargs: Snapshot.of({"items": [forecast], "next_cursor": None}),
    )
    monkeypatch.setattr(workflows.analysis, "get_version", lambda *args, **kwargs: Snapshot.of(version))
    monkeypatch.setattr(
        workflows.analysis,
        "get_forecast",
        lambda *args, **kwargs: Snapshot.of({"forecast": forecast, "current_version": version}),
    )
    monkeypatch.setattr(workflows.plans, "list_plans", lambda *args, **kwargs: (Snapshot.of(old_plan),))
    monkeypatch.setattr(
        workflows.plans,
        "list_evaluations",
        lambda *args, **kwargs: Snapshot.of({"items": [old_evaluation], "next_cursor": None}),
    )
    result = workflows.workspace()
    assert result["market"]["account_scope_id"] == "account-b"
    assert result["evaluations"] == [], (
        "Account A budget/margin green states must not become Account B current evaluations"
    )
