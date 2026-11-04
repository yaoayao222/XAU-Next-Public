"""Versioned settings. Changes never rewrite historical evaluation inputs."""

import re
from contextlib import nullcontext
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from xau_next.shared.analysis_policy import new_analysis_preferences
from xau_next.shared.scalp import validate_scalp_preferences
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot
from xau_next.shared.values import parse_utc, utc_now

from .model_api import ModelSettingsService

__all__ = ["ModelSettingsService", "SettingsService"]


def defaults():
    return {
        "display_timezone": "Asia/Shanghai",
        "symbol": "XAUUSD",
        "quote_max_age_ms": 10000,
        "default_volume_lots": None,
        "per_trade_loss_cap": None,
        "min_net_rr": None,
        "cost_assumption": None,
        "daily_horizons_minutes": [180, 360, 720],
        "experiment_profile": {
            "initial_equity": {"amount": "40", "currency": "USD"},
            "per_trade_loss_cap": {"amount": "2", "currency": "USD"},
            "cumulative_net_losses_cap": {"amount": "20", "currency": "USD"},
            "max_open_positions": 1,
            "max_holding_minutes": 30,
            "profit_replenishes_budget": False,
        },
        "experiment_enabled": False,
        "connection": {
            "terminal_path": None,
            "account_scope_id": None,
            "broker_timezone": None,
            "history_days": 30,
        },
        "analysis_preferences": new_analysis_preferences({}),
    }


class SettingsService:
    def __init__(self, db, clock=utc_now):
        self.db, self.clock, self.store = db, clock, db.documents("settings")

    def initialize(self):
        with self.db.write() as uow:
            if not self.store.get(uow, "settings", "current"):
                self.store.insert(
                    uow, "settings", "current", {"values": defaults(), "updated_at": self.clock()}
                )

    def get_snapshot(self, revision=None, *, scope=None):
        with nullcontext(scope) if scope is not None else self.db.read() as reader:
            if revision is None:
                value = self.store.get(reader, "settings", "current")
            else:
                value = next(
                    (
                        v
                        for v in self.store.history(reader, "settings", "current")
                        if v["revision"] == revision
                    ),
                    None,
                )
            if value is None:
                raise DomainError("SETTINGS_NOT_FOUND", "没有找到指定版本的设置。", 404)
            result = {k: value[k] for k in ("revision", "updated_at", "values")}
            if revision is None:
                result = {**result, "values": {**result["values"],
                    "analysis_preferences": new_analysis_preferences(result["values"])}}
            return Snapshot.of(result)

    def update_settings(self, command, *, uow=None):
        values = command["values"]

        def invalid(path, message):
            raise DomainError(
                "SETTINGS_INVALID",
                message,
                422,
                [{"path": path, "code": "SETTINGS_INVALID", "message": message}],
            )

        scalp = values.get("analysis_preferences", {}).get("scalp_preferences")
        if scalp is not None:
            errors = validate_scalp_preferences(scalp)
            if errors:
                invalid("/values/analysis_preferences/scalp_preferences", "；".join(errors))

        try:
            ZoneInfo(values["display_timezone"])
            broker_zone = values["connection"]["broker_timezone"]
            if broker_zone and not re.fullmatch(r"UTC[+-](?:0[0-9]|1[0-4]):[0-5][0-9]", broker_zone):
                ZoneInfo(broker_zone)
        except ZoneInfoNotFoundError:
            invalid(
                "/values/display_timezone", "时区名称无法识别，请使用 Asia/Shanghai 或 UTC 等 IANA 时区。"
            )
        for field in ("default_volume_lots", "min_net_rr"):
            if values[field] is not None and Decimal(values[field]) <= 0:
                invalid("/values/" + field, "手数和净收益风险比必须大于零。")
        if len(set(values["daily_horizons_minutes"])) != len(values["daily_horizons_minutes"]):
            invalid("/values/daily_horizons_minutes", "观察窗口不能重复。")
        cap = values["per_trade_loss_cap"]
        if cap is not None and Decimal(cap["amount"]) <= 0:
            invalid("/values/per_trade_loss_cap", "每笔亏损预算必须大于零。")
        cost = values["cost_assumption"]
        if cost:
            for key in ("commission_roundtrip_per_lot", "slippage_reserve_per_lot", "other_cost_per_lot"):
                if cost[key] is not None and Decimal(cost[key]) < 0:
                    invalid("/values/cost_assumption/" + key, "预计费用必须为非负成本，缺失项请保留未知。")
            if cost["valid_until"] and parse_utc(cost["valid_until"]) <= parse_utc(cost["valid_from"]):
                invalid("/values/cost_assumption/valid_until", "费用的结束时间必须晚于开始时间。")
        with nullcontext(uow) if uow is not None else self.db.write() as writer:
            current = self.get_snapshot(scope=writer).to_dict()
            if current["revision"] != command["expected_revision"]:
                raise DomainError("VERSION_CONFLICT", f"设置已更新，当前版本为 {current['revision']}。", 409)
            value = self.store.update(
                writer,
                "settings",
                "current",
                {"values": values, "updated_at": self.clock()},
                command["expected_revision"],
            )
            return Snapshot.of({k: value[k] for k in ("revision", "updated_at", "values")})
