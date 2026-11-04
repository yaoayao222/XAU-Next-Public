from datetime import timedelta
from decimal import Decimal

from .protocol import BridgeFailure, id, now, stamp, status, utc

MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}


def bars(at: str, timeframe: str, count: int) -> list[dict]:
    interval = MINUTES[timeframe]
    instant = utc(at)
    end = instant.replace(second=0, microsecond=0) - timedelta(minutes=instant.minute % interval)
    result = []
    for index in range(count):
        start = end - timedelta(minutes=interval * (count - index))
        result.append(
            {
                "open_at": stamp(start),
                "close_at": stamp(start + timedelta(minutes=interval)),
                "open": "99.95",
                "high": "100.10",
                "low": "99.90",
                "close": "100.00",
                "tick_volume": 10,
                "closed": True,
            }
        )
    return result


class SyntheticSource:
    def identity(self):
        return {
            "source_kind": "synthetic",
            "account_scope_id": "synthetic-account",
            "terminal_path": None,
            "server": "synthetic",
            "sdk_version": None,
            "terminal_version": None,
            "read_only": True,
        }

    def handle(self, operation: str, params: dict) -> dict:
        at = params.get("captured_at") or now()
        symbol = params.get("symbol", "XAUUSD")
        if operation == "health":
            return {"status": "ok", "read_only": True, "source_kind": "synthetic"}
        if operation == "identity":
            return self.identity()
        if operation in {"profit", "margin"}:
            volume, price = Decimal(params["volume_lots"]), Decimal(params["entry_price"])
            if operation == "profit":
                direction = Decimal(1 if params["side"] == "long" else -1)
                value = direction * (Decimal(params["exit_price"]) - price) * Decimal(100) * volume
            else:
                value = price * Decimal(100) * volume / Decimal(100)
            return {
                "amount": str(value),
                "currency": "USD",
                "source_kind": "synthetic",
                "account_scope_id": "synthetic-account",
                "symbol": params.get("symbol", "XAUUSD"),
                "side": params["side"],
                "volume_lots": params["volume_lots"],
                "entry_price": params["entry_price"],
                "captured_at": params.get("captured_at") or now(),
            }
        if operation == "history":
            start, end = utc(params["from"]), utc(params["to"])
            count = int((end - start).total_seconds() / 60)
            if count < 0 or count > 5000:
                raise BridgeFailure("HISTORY_RANGE_INVALID", "合成历史区间应为 0 到 5000 分钟")
            series = bars(stamp(end), "M1", count)
            ticks = [
                {
                    "event_time": row["close_at"],
                    "bid": row["close"],
                    "ask": "100.02",
                    "source_sequence": index,
                }
                for index, row in enumerate(series)
            ]
            ticks.insert(
                0, {"event_time": params["from"], "bid": "99.98", "ask": "100.00", "source_sequence": 0}
            )
            for sequence, tick in enumerate(ticks):
                tick["source_sequence"] = sequence
            return {
                "snapshot_id": id(),
                "source_kind": "synthetic",
                "account_scope_id": "synthetic-account",
                "symbol": symbol,
                "from": params["from"],
                "to": params["to"],
                "bars": series,
                "ticks": ticks,
                "tick_status": "available",
                "bar_status": status(at),
                "coverage": {
                    "from": params["from"],
                    "to": params["to"],
                    "actual_from": params["from"],
                    "actual_to": params["to"],
                    "method": "synthetic-fixed-quotes/1",
                    "complete": True,
                },
                "price_basis": "bid",
                "status": status(at),
                "trades": [],
                "trades_status": status(at),
                "raw": {"synthetic": True},
            }
        if operation == "snapshot":
            scope = "synthetic-account"
            market = {
                "snapshot_id": id(),
                "source_kind": "synthetic",
                "account_scope_id": scope,
                "symbol": symbol,
                "market_cutoff": at,
                "captured_at": at,
                "quote_status": status(at),
                "quote": {"bid": "99.98", "ask": "100", "event_time": at},
                "contract_status": status(at),
                "contract": {
                    "contract_size": "100",
                    "tick_size": "0.01",
                    "volume_min": "0.01",
                    "volume_step": "0.01",
                    "volume_max": "100",
                    "profit_currency": "USD",
                    "margin_currency": "USD",
                    "calculation_mode": "linear_usd",
                    "broker_leverage": "100",
                },
                "series": [
                    {
                        "timeframe": timeframe,
                        "price_basis": "bid",
                        "status": status(at),
                        "bars": bars(at, timeframe, count),
                    }
                    for timeframe, count in (("H1", 240), ("M15", 400), ("M5", 400), ("M1", 1440))
                ],
                "revision": 1,
            }
            portfolio = {
                "snapshot_id": id(),
                "account_scope_id": scope,
                "account_status": status(at),
                "account": {
                    "currency": "USD",
                    "balance": "10000",
                    "equity": "10000",
                    "free_margin": "10000",
                    "used_margin": "0",
                },
                "orders_status": status(at),
                "trades_status": status(at),
                "orders": [],
                "revision": 1,
            }
            history = None
            if params.get("include_history"):
                history = self.handle(
                    "history", {**params, "from": params["history_from"], "to": params["history_to"]}
                )
            return {
                "market": market,
                "portfolio": portfolio,
                "trades": [],
                "history": history,
                "raw": {
                    "data_kind": "synthetic",
                    "method": "fixed-candles/1",
                    "deals": [],
                    "trade_history_read": {
                        "from": stamp(utc(at) - timedelta(days=params.get("history_days", 30))),
                        "to": at,
                        "history_days": params.get("history_days", 30),
                        "status": status(at),
                        "method": "synthetic-empty-trade-history/1",
                    },
                },
            }
        raise BridgeFailure("OPERATION_NOT_ALLOWED", "不支持此只读操作")
