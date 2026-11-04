import copy

from fastapi.testclient import TestClient
from test_flow import TOKEN, Clock, answer, get, post, refresh, request, submit
from xau_next.bootstrap import create_app
from xau_next.shared.snapshots import Snapshot


def test_ac11_13_18_money_margin_association_and_intent(tmp_path):
    clock = Clock()
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", clock=clock, start_worker=False)
    with TestClient(app) as client:
        market = refresh(client, app)
        req = request(client, market)
        clock.value = "2026-10-12T02:07:00Z"
        job, _, _ = submit(client, app, req, answer(req))
        assert job["state"] == "succeeded"
        workspace = get(client, "/v1/workspace").json()
        plan = workspace["plans"][0]
        before = get(client, "/v1/settings").json()
        values = copy.deepcopy(before["values"])
        values.update(
            per_trade_loss_cap={"amount": "2", "currency": "USD"},
            default_volume_lots="0.01",
            cost_assumption={
                "commission_roundtrip_per_lot": "20",
                "slippage_reserve_per_lot": "20",
                "other_cost_per_lot": "0",
                "currency": "USD",
                "valid_from": "2026-10-12T00:00:00Z",
                "valid_until": None,
                "account_scope_id": market["account_scope_id"],
                "symbol": "XAUUSD",
                "source_note": "合成金额验收，明确每手来回费用",
            },
        )
        response = client.put(
            "/v1/settings",
            json={"expected_revision": before["revision"], "values": values},
            headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "cost-settings"},
        )
        assert response.status_code == 200, response.text
        settings = response.json()
        evaluated = post(
            client,
            f"/v1/plans/{plan['plan_id']}/evaluations",
            {
                "market_snapshot_id": market["snapshot_id"],
                "settings_revision": settings["revision"],
                "sizing_mode": "fixed_volume",
                "entry_price_override": None,
                "volume_lots_override": None,
            },
            "evaluate",
        )
        assert evaluated.status_code == 201, evaluated.text
        result = evaluated.json()
        assert result["planned_loss"] == "1.6" and result["planned_net_profit"] == "2.6"
        assert result["margin_required"] == "1" and result["margin_check"]["state"] == "satisfied"
        assert get(client, "/v1/settings?revision=1").json() == before
        # A later read introduces a real-shaped position fixture. Human intention cannot close it.
        portfolio = workspace["portfolio"]
        portfolio["snapshot_id"] = "synthetic-with-position"
        reference = {
            "account_scope_id": market["account_scope_id"],
            "kind": "position",
            "broker_id": "fixture-ticket-1",
        }
        position = {
            "order": reference,
            "symbol": "XAUUSD",
            "side": "long",
            "volume_lots": "0.01",
            "entry_price": "100",
            "stop_loss": "98.8",
            "take_profit": "103",
            "event_time": clock(),
            "captured_at": clock(),
            "plan_id": None,
        }
        portfolio["orders"] = [position]
        app.state.workflows.portfolio.sync_facts(Snapshot.of(portfolio))
        current = get(client, "/v1/portfolio").json()
        linked = post(
            client,
            "/v1/portfolio/associations",
            {
                "order": reference,
                "plan_id": plan["plan_id"],
                "action": "link",
                "reason": "合成样例人工明确关联",
                "expected_portfolio_revision": current["revision"],
            },
            "link",
        )
        assert linked.status_code == 201, linked.text
        decision = post(
            client,
            "/v1/portfolio/decisions",
            {
                "order": reference,
                "action": "close",
                "volume_lots": None,
                "stop_loss": None,
                "take_profit": None,
                "note": "只是人工意图，不是已执行事实",
            },
            "decision",
        )
        assert decision.status_code == 201, decision.text
        current = get(client, "/v1/portfolio").json()
        assert len(current["orders"]) == 1 and current["orders"][0]["volume_lots"] == "0.01"
        unlinked = post(
            client,
            "/v1/portfolio/associations",
            {
                "order": reference,
                "plan_id": None,
                "action": "unlink",
                "reason": "撤销合成关联",
                "expected_portfolio_revision": current["revision"],
            },
            "unlink",
        )
        assert unlinked.status_code == 201, unlinked.text
        assert get(client, "/v1/portfolio").json()["orders"][0]["plan_id"] is None


def test_ac20_runtime_operation_coverage(tmp_path):
    app = create_app(data_root=tmp_path, token=TOKEN, source="synthetic", start_worker=False)
    actual = app.openapi()
    authoritative = app.state.contracts.api
    expected = {
        value["operationId"]: (path, method)
        for path, methods in authoritative["paths"].items()
        for method, value in methods.items()
        if method in {"get", "post", "put"}
    }
    observed = {
        value["operationId"]: (path, method)
        for path, methods in actual["paths"].items()
        for method, value in methods.items()
        if method in {"get", "post", "put"}
    }
    assert observed == expected
    with TestClient(app) as client:
        for route in [
            "/v1/market/snapshots/missing",
            "/v1/forecasts/missing",
            "/v1/plans/missing",
            "/v1/jobs/missing",
            "/v1/reviews/missing",
            "/v1/portfolio/trades/missing",
        ]:
            result = get(client, route)
            assert result.status_code == 404, (route, result.text)
            assert result.headers["content-type"].startswith("application/problem+json")
        assert get(client, "/v1/forecasts?limit=101").status_code == 422
        assert get(client, "/v1/experiments/unavailable").status_code == 501
