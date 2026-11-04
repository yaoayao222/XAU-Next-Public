"""Synthetic order-level guards: refresh is harmless; real conflicts never overwrite."""
import copy

import pytest
from xau_next.shared.errors import DomainError
from test_portfolio import env, portfolio, sync, link_plan


def command(state):
    return {"order": state["orders"][0]["order"], "plan_id": "plan-1", "action": "link",
            "reason": "SYNTHETIC explicit association", "expected_portfolio_revision": state["revision"],
            "expected_order_token": state["orders"][0]["association_token"]}


def save(service, cmd):
    return service.create_association(cmd, plan_snapshot={"plan_id": "plan-1",
        "account_scope_id": "account-demo", "symbol": "XAUUSD"}).to_dict()


def test_background_reads_account_amounts_and_other_orders_do_not_conflict(env):
    service, _, _ = env
    first = sync(service)
    next_facts = portfolio("next-read")
    next_facts["account"]["equity"] = "99.25"
    next_facts["orders"][0]["captured_at"] = "2026-10-12T02:00:05Z"
    next_facts["orders_status"]["captured_at"] = "2026-10-12T02:00:05Z"
    other = copy.deepcopy(next_facts["orders"][0])
    other["order"]["broker_id"] = "unrelated-position"
    next_facts["orders"].append(other)
    latest = sync(service, next_facts)
    assert latest["revision"] > first["revision"]
    assert latest["orders"][0]["association_token"] == first["orders"][0]["association_token"]
    assert save(service, command(first))["input"]["expected_order_token"] == command(first)["expected_order_token"]
    assert service.get_portfolio().to_dict()["orders"][0]["plan_id"] == "plan-1"


@pytest.mark.parametrize("field,value", [("volume_lots", "0.005"), ("stop_loss", "99"),
    ("take_profit", "104"), ("entry_price", "101"), ("side", "short"),
    ("symbol", "EURUSD"), ("event_time", "2026-10-12T01:01:00Z")])
def test_changed_target_rejected_without_any_association_write(env, field, value):
    service, _, _ = env
    first = sync(service)
    next_facts = portfolio("changed-target")
    next_facts["orders"][0][field] = value
    sync(service, next_facts)
    with pytest.raises(DomainError, match="关键资料") as error:
        save(service, command(first))
    assert error.value.code == "VERSION_CONFLICT"
    assert service.list_associations(command(first)["order"]) == ()


@pytest.mark.parametrize("state", ["unavailable", "incomplete"])
def test_read_status_changed_rejected(env, state):
    service, _, _ = env
    first = sync(service)
    sync(service, portfolio("failed-read", orders_status=state))
    with pytest.raises(DomainError, match="读取状态"):
        save(service, command(first))


def test_disappeared_order_and_account_switch_rejected(env):
    service, _, _ = env
    first = sync(service)
    sync(service, portfolio("flat", order=False))
    with pytest.raises(DomainError):
        save(service, command(first))
    sync(service, portfolio("other-account", account_scope="other-account"))
    with pytest.raises(DomainError):
        save(service, command(first))


def test_concurrent_link_and_link_unlink_back_to_none_reject_old_token(env):
    service, _, _ = env
    first = sync(service)
    cmd = command(first)
    save(service, cmd)
    with pytest.raises(DomainError):
        save(service, cmd)
    link_plan(service, cmd["order"], action="unlink")
    assert service.get_portfolio().to_dict()["orders"][0]["plan_id"] is None
    with pytest.raises(DomainError):
        save(service, cmd)
    assert len(service.list_associations(cmd["order"])) == 2
    save(service, command(service.get_portfolio().to_dict()))
    assert len(service.list_associations(cmd["order"])) == 3


def test_old_client_keeps_strict_global_guard(env):
    service, _, _ = env
    first = sync(service)
    cmd = command(first)
    cmd.pop("expected_order_token")
    sync(service, portfolio("plain-refresh"))
    with pytest.raises(DomainError):
        save(service, cmd)
