from __future__ import annotations

import hashlib
import importlib
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from .protocol import BridgeFailure, id, now, stamp, utc
from .timebase import Timebase
from .windows import prevent_child_processes, running_terminals

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}


def decimal(value) -> str:
    result = Decimal(str(value))
    if not result.is_finite():
        raise BridgeFailure("VALUE_INVALID", "终端返回了无效金额或价格")
    return format(result, "f")


def epoch(seconds: float) -> str:
    return stamp(datetime.fromtimestamp(seconds, UTC))


def raw(value):
    if hasattr(value, "_asdict"):
        return {key: raw(item) for key, item in value._asdict().items()}
    if hasattr(value, "dtype") and value.dtype.names:
        return {key: raw(value[key]) for key in value.dtype.names}
    if hasattr(value, "item"):
        return raw(value.item())
    if isinstance(value, (tuple, list)):
        return [raw(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def source_status(at: str, *, state="available", observed_at=None, reasons=()):
    return {"state": state, "observed_at": observed_at, "captured_at": at, "reasons": list(reasons)}


class MT5Source:
    """An explicit operation allowlist; never forwards arbitrary SDK method names."""

    def __init__(self, terminal_path: str | None = None):
        self._selected_path, self._sdk, self._identity = terminal_path, None, None
        self._connected_path = None
        self._account_key = None
        self._timebase = Timebase(None)

    def identity(self):
        return self._identity

    def _ensure(self, params: dict):
        self._timebase = Timebase(params.get("broker_timezone"))
        requested = params.get("terminal_path") or self._selected_path
        processes = running_terminals()
        if requested:
            expected = str(Path(requested).resolve()).casefold()
            matches = [p for p in processes if p["path"].casefold() == expected]
        else:
            matches = processes
        if not matches:
            raise BridgeFailure("TERMINAL_NOT_RUNNING", "所选 MT5 终端未运行，请先手动打开并登录", True)
        if len(matches) != 1:
            raise BridgeFailure("TERMINAL_SELECTION_REQUIRED", "检测到多个终端，请在设置中选择已运行终端路径")
        selected = matches[0]
        prevent_child_processes()
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise BridgeFailure("SDK_UNAVAILABLE", "只读桥环境未安装 MetaTrader5 SDK") from exc
        if self._connected_path != selected["path"]:
            if self._connected_path:
                self._sdk.shutdown()
            if not self._sdk.initialize(selected["path"], timeout=10000, portable=False):
                raise BridgeFailure("TERMINAL_ATTACH_FAILED", "无法连接已运行终端的只读接口", True)
            self._connected_path = selected["path"]
        terminal, account = self._sdk.terminal_info(), self._sdk.account_info()
        if terminal is None or account is None:
            raise BridgeFailure("ACCOUNT_UNAVAILABLE", "终端或已登录账户身份不可读取", True)
        actual_path = str((Path(terminal.path) / "terminal64.exe").resolve())
        if actual_path.casefold() != selected["path"].casefold():
            self.close()
            raise BridgeFailure("TERMINAL_IDENTITY_MISMATCH", "SDK 返回终端与所选进程路径不一致")
        current = running_terminals()
        if not any(p["pid"] == selected["pid"] and p["path"] == selected["path"] for p in current):
            self.close()
            raise BridgeFailure("TERMINAL_CHANGED", "终端在读取期间发生变化", True)
        self._account_key = (str(account.login), account.server)
        scope = hashlib.sha256(
            (selected["path"].casefold() + "|" + account.server + "|" + str(account.login)).encode()
        ).hexdigest()[:32]
        self._identity = {
            "source_kind": "mt5",
            "account_scope_id": scope,
            "terminal_path": selected["path"],
            "terminal_pid": selected["pid"],
            "server": account.server,
            "broker": account.company,
            "sdk_version": str(self._sdk.__version__),
            "terminal_version": int(terminal.build),
            "read_only": True,
            "child_processes_blocked": True,
            "broker_timezone": self._timebase.configured,
        }
        return terminal, account

    def _check_end(self):
        account = self._sdk.account_info()
        if account is None or (str(account.login), account.server) != self._account_key:
            raise BridgeFailure("ACCOUNT_CHANGED", "账户在采集过程中变化，本批结果未接纳", True)

    def handle(self, operation: str, params: dict) -> dict:
        terminal, account = self._ensure(params)
        if operation == "identity":
            return self.identity()
        if operation == "health":
            return {
                "status": "ok" if terminal.connected else "disconnected",
                "read_only": True,
                "source_kind": "mt5",
            }
        if operation == "snapshot":
            result = self._snapshot(params, terminal, account)
        elif operation == "history":
            result = self._history(params, account)
        elif operation in {"profit", "margin"}:
            result = self._calculation(operation, params, account)
        else:
            raise BridgeFailure("OPERATION_NOT_ALLOWED", "此桥仅允许明确的只读操作")
        self._check_end()
        return result

    def _snapshot(self, params, terminal, account):
        at, symbol = now(), params.get("symbol", "XAUUSD")
        scope = self._identity["account_scope_id"]
        info, tick = self._sdk.symbol_info(symbol), self._sdk.symbol_info_tick(symbol)
        quote, quote_selection = None, None
        if tick is not None and tick.bid > 0 and tick.ask >= tick.bid:
            quote_at = self._timebase.event_time((tick.time_msc or tick.time * 1000) / 1000)
            quote = {"bid": decimal(tick.bid), "ask": decimal(tick.ask), "event_time": quote_at}
            age_ms = (utc(at) - utc(quote_at)).total_seconds() * 1000
            quote_state = (
                "incomplete"
                if age_ms < -1000
                else "stale"
                if age_ms > params.get("quote_max_age_ms", 30000)
                else "available"
            )
            quote_reasons = (
                ["报价时间晚于采集时间"]
                if age_ms < -1000
                else ["报价超过允许新鲜度"]
                if quote_state == "stale"
                else []
            )
            if not terminal.connected:
                quote_state, quote_reasons = "stale", ["终端未连接服务器，保留最后报价"]
            quote_status = source_status(at, state=quote_state, observed_at=quote_at, reasons=quote_reasons)
            if terminal.connected and -1000 <= age_ms < 0:
                # A tick arriving during capture (or modest broker clock lead) must
                # not be called available and then disappear from the frozen packet.
                # Keep the cutoff and source timestamps; select an actual earlier tick.
                quote, quote_selection, reason = self._quote_at_cutoff(symbol, at, params)
                quote_status = source_status(
                    at,
                    state="available" if quote else "unavailable",
                    observed_at=quote["event_time"] if quote else None,
                    reasons=[] if quote else [reason],
                )
        else:
            quote_status = source_status(at, state="unavailable", reasons=["未取得有效 Bid/Ask 报价"])
        contract = None
        if info is not None and info.trade_contract_size > 0 and info.trade_tick_size > 0:
            linear = self._linear_mode(info, account)
            contract = {
                "contract_size": decimal(info.trade_contract_size),
                "tick_size": decimal(info.trade_tick_size),
                "volume_min": decimal(info.volume_min),
                "volume_step": decimal(info.volume_step),
                "volume_max": decimal(info.volume_max),
                "profit_currency": info.currency_profit,
                "margin_currency": info.currency_margin,
                "calculation_mode": "linear_usd" if linear else f"mt5_mode_{info.trade_calc_mode}",
                "broker_leverage": decimal(account.leverage) if account.leverage > 0 else None,
            }
        contract_status = (
            source_status(at, observed_at=at)
            if contract
            else source_status(at, state="unavailable", reasons=["合约规格不可读取"])
        )
        all_series, rates_raw = [], {}
        price_basis = "bid" if info is not None and getattr(info, "chart_mode", None) == 0 else "unknown"
        for name, count in (("H1", 240), ("M15", 400), ("M5", 400), ("M1", 1440)):
            rates = self._sdk.copy_rates_from_pos(symbol, getattr(self._sdk, "TIMEFRAME_" + name), 0, count)
            series = self._series(name, rates, at, price_basis)
            if quote_status["state"] == "incomplete":
                series["status"]["state"] = "incomplete"
                series["status"]["reasons"].append("TIME_BASIS_UNKNOWN：终端时间基准待确认")
                for bar in series["bars"]:
                    bar["closed"] = False
            all_series.append(series)
            rates_raw[name] = [raw(row) for row in rates] if rates is not None else None
        market = {
            "snapshot_id": id(),
            "source_kind": "mt5",
            "account_scope_id": scope,
            "symbol": symbol,
            "market_cutoff": at,
            "captured_at": at,
            "quote_status": quote_status,
            "quote": quote,
            "contract_status": contract_status,
            "contract": contract,
            "series": all_series,
            "revision": 1,
        }
        positions, orders = self._sdk.positions_get(), self._sdk.orders_get()
        facts = []
        for kind, records in (("position", positions), ("pending_order", orders)):
            if records is not None:
                facts.extend(self._order(row, kind, scope, at) for row in records)
        failures = [name for name, data in (("持仓", positions), ("挂单", orders)) if data is None]
        orders_status = source_status(
            at,
            state="incomplete" if failures and facts else "unavailable" if failures else "available",
            observed_at=at if not failures else None,
            reasons=[name + "读取失败" for name in failures],
        )
        portfolio = {
            "snapshot_id": id(),
            "account_scope_id": scope,
            "account_status": source_status(
                at,
                state="available" if terminal.connected else "stale",
                observed_at=at,
                reasons=[] if terminal.connected else ["账户来自终端缓存，服务器未连接"],
            ),
            "account": {
                "currency": account.currency,
                "balance": decimal(account.balance),
                "equity": decimal(account.equity),
                "free_margin": decimal(account.margin_free),
                "used_margin": decimal(account.margin),
            },
            "orders_status": orders_status,
            "orders": facts,
            "revision": 1,
        }
        trade_history = self._daily_trades(params, account, at=at, connected=terminal.connected)
        portfolio["trades_status"] = trade_history["status"]
        history = (
            self._history({**params, "from": params["history_from"], "to": params["history_to"]}, account)
            if params.get("include_history")
            else None
        )
        return {
            "market": market,
            "portfolio": portfolio,
            "trades": trade_history["trades"],
            "history": history,
            "raw": {
                "terminal": raw(terminal),
                "capture_settings": params,
                "time_normalization": {
                    "broker_timezone": self._timebase.configured,
                    "source": "explicit_user_configuration"
                    if self._timebase.configured
                    else "unverified_sdk_utc",
                },
                "account": raw(account),
                "symbol": raw(info),
                "tick": raw(tick),
                "snapshot_quote_selection": quote_selection,
                "positions": raw(positions),
                "orders": raw(orders),
                "rates": rates_raw,
                "deals": trade_history["raw_deals"],
                "trade_history_read": {
                    key: value for key, value in trade_history.items() if key not in {"trades", "raw_deals"}
                },
            },
        }

    def _quote_at_cutoff(self, symbol, at, params):
        """Resolve a small future-tick race without widening the frozen boundary."""
        cutoff = utc(at)
        # A bounded read avoids copying long tick histories in a five-second refresh.
        lookback_ms = min(params.get("quote_max_age_ms", 30000), 30000)
        start = cutoff - timedelta(milliseconds=lookback_ms)
        # SDK queries can have second precision. Include that last second, then apply
        # the exact millisecond cutoff locally; never accept its future ticks.
        end = cutoff.replace(microsecond=0) + timedelta(seconds=1)
        selection = {
            "method": "last-valid-pre-cutoff-tick/1",
            "cutoff": at,
            "lookback_ms": lookback_ms,
            "query_from": stamp(start),
            "query_to": stamp(end),
            "row_count": None,
            "excluded_future": 0,
            "excluded_invalid_price": 0,
            "selected_tick": None,
        }
        try:
            rows = self._sdk.copy_ticks_range(
                symbol, self._timebase.query_time(start), self._timebase.query_time(end),
                self._sdk.COPY_TICKS_ALL,
            )
        except Exception as exc:  # noqa: BLE001 -- SDK read failure must not hide other successful facts.
            selection["read_error"] = type(exc).__name__
            return None, selection, "最新报价晚于冻结截点，截点前逐笔报价读取失败。"
        if rows is None:
            return None, selection, "最新报价晚于冻结截点，截点前逐笔报价读取失败。"
        selection["row_count"] = len(rows)
        if len(rows) > 10000:
            return None, selection, "截点前短窗口报价过多，未截断或猜测最后报价。"
        previous, chosen, chosen_at, chosen_raw, ambiguous = None, None, None, None, False
        for row in rows:
            item = raw(row)
            try:
                event = self._timebase.event_time(int(item["time_msc"]) / 1000)
                event_at = utc(event)
            except (KeyError, TypeError, ValueError, OverflowError, BridgeFailure):
                return None, selection, "截点前逐笔报价时间无效，无法确认最后报价。"
            if previous is not None and event_at < previous:
                return None, selection, "截点前逐笔报价顺序异常，未重排或猜测最后报价。"
            previous = event_at
            if event_at > cutoff:
                selection["excluded_future"] += 1
                continue
            if event_at < start:
                continue
            try:
                bid, ask = decimal(item["bid"]), decimal(item["ask"])
                if Decimal(bid) <= 0 or Decimal(ask) < Decimal(bid):
                    raise ValueError("invalid bid/ask")
            except (KeyError, TypeError, ValueError, ArithmeticError, BridgeFailure):
                selection["excluded_invalid_price"] += 1
                continue
            quote = {"event_time": event, "bid": bid, "ask": ask}
            if event_at == chosen_at:
                ambiguous = ambiguous or (Decimal(bid), Decimal(ask)) != (
                    Decimal(chosen["bid"]), Decimal(chosen["ask"])
                )
            else:
                chosen_at, chosen, ambiguous = event_at, quote, False
                chosen_raw = item
        if ambiguous:
            selection["selected_tick"] = None
            return None, selection, "截点前最后时刻存在不同双侧报价，无法确认唯一报价。"
        if chosen is None:
            return None, selection, "最新报价晚于冻结截点，未取得新鲜且有效的截点前双侧报价。"
        # Retain the exact used source row and the original latest tick, not a new
        # copy of every overlapping lookback window on each background refresh.
        selection["selected_tick"] = chosen_raw
        return chosen, selection, None

    def _daily_trades(self, params, account, *, at, connected):
        """Account deal collection has its own configured range, independent of chart replay."""
        days = params.get("history_days", 30)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            return {
                "trades": [],
                "raw_deals": None,
                "history_days": days,
                "from": None,
                "to": at,
                "status": source_status(
                    at, state="unavailable", reasons=["成交历史读取天数必须为 1 到 3650 的整数"]
                ),
                "error": {"code": "HISTORY_DAYS_INVALID"},
            }
        end = utc(at)
        result = self._read_deals(end - timedelta(days=days), end, account, at=at, connected=connected)
        result["history_days"] = days
        return result

    def _read_deals(self, start, end, account, *, at, connected=True):
        """Only history_deals_get is called; failures remain separate from other facts."""
        result = {
            "trades": [],
            "raw_deals": None,
            "from": stamp(start),
            "to": stamp(end),
            "broker_timezone": self._timebase.configured,
            "method": "mt5-history-deals-get/1",
        }
        try:
            query_start = self._timebase.query_time(start)
            query_end = self._timebase.query_time(end)
            result.update(query_from_encoded=stamp(query_start), query_to_encoded=stamp(query_end))
            deals = self._sdk.history_deals_get(query_start, query_end)
            if deals is None:
                if hasattr(self._sdk, "last_error"):
                    result["sdk_error"] = raw(self._sdk.last_error())
                raise BridgeFailure("DEALS_UNAVAILABLE", "成交历史读取失败，当前历史情况未知", True)
            result["raw_deals"] = raw(deals)
            result["trades"] = self._trades(deals, account)
            observed = [deal["event_time"] for trade in result["trades"] for deal in trade["deals"]]
            result["status"] = source_status(
                at,
                state="available" if connected else "stale",
                observed_at=max(observed, key=utc) if observed else stamp(end),
                reasons=[] if connected else ["成交历史来自终端缓存，服务器未连接"],
            )
        except Exception as error:  # noqa: BLE001 -- isolate SDK failures into explicit status, retain raw evidence.
            # SDK read/conversion failure must not erase successfully captured quote,
            # contract, account or orders. Preserve raw rows when conversion failed.
            result["trades"] = []
            result["status"] = source_status(
                at, state="unavailable", reasons=["成交历史读取或解析失败，未将缺失记录当作空历史"]
            )
            result["error"] = {
                "code": getattr(error, "code", "DEALS_READ_FAILED"),
                "detail": str(error),
            }
        return result

    def _linear_mode(self, info, account):
        names = (
            "SYMBOL_CALC_MODE_FOREX",
            "SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE",
            "SYMBOL_CALC_MODE_CFD",
            "SYMBOL_CALC_MODE_CFDINDEX",
            "SYMBOL_CALC_MODE_CFDLEVERAGE",
        )
        supported = {getattr(self._sdk, name, None) for name in names} - {None}
        # Constants absent from a SDK build do not imply support for a numeric mode.
        return (
            info.trade_calc_mode in supported and info.currency_profit == "USD" and account.currency == "USD"
        )

    def _series(self, timeframe, rates, at, price_basis):
        bars = []
        interval = TIMEFRAMES[timeframe] * 60
        for row in rates if rates is not None else []:
            close_time = self._timebase.event_time(int(row["time"]) + interval)
            bars.append(
                {
                    "open_at": self._timebase.event_time(int(row["time"])),
                    "close_at": close_time,
                    "open": decimal(row["open"]),
                    "high": decimal(row["high"]),
                    "low": decimal(row["low"]),
                    "close": decimal(row["close"]),
                    "tick_volume": int(row["tick_volume"]),
                    "closed": utc(close_time) <= utc(at),
                }
            )
        closed = [row for row in bars if row["closed"]]
        reasons = []
        if not closed:
            state, observed = "unavailable", None
            reasons.append("未取得已收盘 K 线")
        else:
            observed = closed[-1]["close_at"]
            state = "stale" if (utc(at) - utc(observed)).total_seconds() > interval * 2 else "available"
            if state == "stale":
                reasons.append("最近闭柱时间过旧")
            if any(utc(b["open_at"]) != utc(a["close_at"]) for a, b in pairwise(closed)):
                state = "incomplete"
                reasons.append("区间存在缺柱或非交易时段，未补造 K 线")
            if price_basis == "unknown":
                state = "incomplete"
                reasons.append("无法确认 K 线报价侧")
        return {
            "timeframe": timeframe,
            "price_basis": price_basis,
            "status": source_status(at, state=state, observed_at=observed, reasons=reasons),
            "bars": bars,
        }

    def _order(self, row, kind, scope, at):
        long_types = {
            self._sdk.ORDER_TYPE_BUY,
            self._sdk.ORDER_TYPE_BUY_LIMIT,
            self._sdk.ORDER_TYPE_BUY_STOP,
            self._sdk.ORDER_TYPE_BUY_STOP_LIMIT,
        }
        entry_time = getattr(row, "time_msc", None) or getattr(row, "time_setup_msc", None)
        if not entry_time:
            entry_time = (getattr(row, "time", None) or getattr(row, "time_setup", 0)) * 1000
        return {
            "order": {"account_scope_id": scope, "kind": kind, "broker_id": str(row.ticket)},
            "symbol": row.symbol,
            "side": "long" if row.type in long_types else "short",
            "volume_lots": decimal(row.volume if kind == "position" else row.volume_current),
            "entry_price": decimal(row.price_open),
            "stop_loss": decimal(row.sl) if row.sl else None,
            "take_profit": decimal(row.tp) if row.tp else None,
            "event_time": self._timebase.event_time(entry_time / 1000),
            "captured_at": at,
            "plan_id": None,
        }

    def _history(self, params, account):
        at, symbol = now(), params.get("symbol", "XAUUSD")
        start, end = utc(params["from"]), utc(params["to"])
        if start >= end or end - start > timedelta(days=3):
            raise BridgeFailure("HISTORY_RANGE_INVALID", "单次行情历史读取区间应在 0 到 3 天内，请分段读取")
        query_start, query_end = self._timebase.query_time(start), self._timebase.query_time(end)
        rates = self._sdk.copy_rates_range(symbol, self._sdk.TIMEFRAME_M1, query_start, query_end)
        ticks = self._sdk.copy_ticks_range(symbol, query_start, query_end, self._sdk.COPY_TICKS_ALL)
        trade_history = self._read_deals(start, end, account, at=at)
        if ticks is not None and len(ticks) > 150000:
            raise BridgeFailure("HISTORY_TOO_LARGE", "本区间报价过多，请缩短区间；数据未被静默截断")
        info = self._sdk.symbol_info(symbol)
        basis = "bid" if info is not None and getattr(info, "chart_mode", None) == 0 else "unknown"
        series = self._series("M1", rates, at, basis)
        series["bars"] = [
            bar for bar in series["bars"] if utc(bar["open_at"]) >= start and utc(bar["close_at"]) <= end
        ]
        tick_values = []
        for row in ticks if ticks is not None else []:
            tick_values.append(
                {
                    "event_time": self._timebase.event_time(int(row["time_msc"]) / 1000),
                    "bid": decimal(row["bid"]),
                    "ask": decimal(row["ask"]),
                    "source_sequence": None,
                    "flags": int(row["flags"]),
                }
            )
        bar_complete = bool(
            series["bars"]
            and utc(series["bars"][0]["open_at"]) == start
            and utc(series["bars"][-1]["close_at"]) == end
            and all(utc(a["close_at"]) == utc(b["open_at"]) for a, b in pairwise(series["bars"]))
        )
        bar_complete = bar_complete and basis == "bid"
        reasons = [] if bar_complete else ["请求历史区间未取得完整连续的 Bid M1 柱"]
        series["status"] = source_status(
            at,
            state="available" if bar_complete else "incomplete",
            observed_at=series["bars"][-1]["close_at"] if series["bars"] else None,
            reasons=reasons,
        )
        current_tick = self._sdk.symbol_info_tick(symbol)
        tick_checked_at = now()
        time_unverified = (
            current_tick is None
            or (utc(self._timebase.event_time(current_tick.time_msc / 1000)) - utc(tick_checked_at)).total_seconds() > 1
        )
        if time_unverified:
            reasons.append("TIME_BASIS_UNKNOWN：终端时间基准待确认")
            series["status"]["state"] = "incomplete"
            series["status"]["reasons"].append("TIME_BASIS_UNKNOWN：终端时间基准待确认")
            for bar in series["bars"]:
                bar["closed"] = False
        if ticks is None:
            reasons.append("双侧报价历史读取失败")
        if trade_history["status"]["state"] != "available":
            reasons.append("成交历史读取失败")
        valid_quotes = all(
            Decimal(tick["bid"]) > 0 and Decimal(tick["ask"]) >= Decimal(tick["bid"]) for tick in tick_values
        )
        ordered_quotes = all(utc(a["event_time"]) <= utc(b["event_time"]) for a, b in pairwise(tick_values))
        if not valid_quotes:
            reasons.append("历史报价包含无效 Bid/Ask，原始记录已保留")
        if not ordered_quotes:
            reasons.append("历史报价时间顺序异常，未重新猜测顺序")
        tick_complete = bool(
            tick_values
            and not time_unverified
            and valid_quotes
            and ordered_quotes
            and utc(tick_values[0]["event_time"]) <= start + timedelta(seconds=5)
            and utc(tick_values[-1]["event_time"]) >= end - timedelta(seconds=5)
        )
        tick_status = "available" if tick_complete else "incomplete" if tick_values else "unavailable"
        if not tick_complete:
            reasons.append("双侧报价未证明覆盖请求区间")
        result_status = source_status(
            at, state="incomplete" if reasons else "available", observed_at=stamp(end), reasons=reasons
        )
        return {
            "snapshot_id": id(),
            "source_kind": "mt5",
            "account_scope_id": self._identity["account_scope_id"],
            "symbol": symbol,
            "from": params["from"],
            "to": params["to"],
            "bars": series["bars"],
            "ticks": tick_values,
            "tick_status": tick_status,
            "bar_status": series["status"],
            "coverage": {
                "from": params["from"],
                "to": params["to"],
                "actual_from": tick_values[0]["event_time"] if tick_values else None,
                "actual_to": tick_values[-1]["event_time"] if tick_values else None,
                "method": "mt5-copy-ticks-range/1",
                "complete": tick_complete,
            },
            "price_basis": basis,
            "status": result_status,
            "trades": trade_history["trades"],
            "trades_status": trade_history["status"],
            "raw": {
                "deals": trade_history["raw_deals"],
                "trade_history_read": {
                    key: value for key, value in trade_history.items() if key not in {"trades", "raw_deals"}
                },
                "history_timing": {"history_started_at": at, "tick_checked_at": tick_checked_at, "current_tick_event_time": self._timebase.event_time(current_tick.time_msc / 1000) if current_tick is not None else None},
                "time_normalization": {
                    "broker_timezone": self._timebase.configured,
                    "source": "explicit_user_configuration"
                    if self._timebase.configured
                    else "unverified_sdk_utc",
                    "query_from_encoded": stamp(query_start),
                    "query_to_encoded": stamp(query_end),
                },
                "rates": [raw(r) for r in rates] if rates is not None else None,
                "ticks": [raw(t) for t in ticks] if ticks is not None else None,
            },
        }

    def _trades(self, deals, account):
        grouped = {}
        for deal in deals:
            if deal.position_id and deal.type in {self._sdk.DEAL_TYPE_BUY, self._sdk.DEAL_TYPE_SELL}:
                grouped.setdefault(str(deal.position_id), []).append(deal)
        result = []
        entry_names = {
            self._sdk.DEAL_ENTRY_IN: "in",
            self._sdk.DEAL_ENTRY_OUT: "out",
            self._sdk.DEAL_ENTRY_INOUT: "inout",
            self._sdk.DEAL_ENTRY_OUT_BY: "out_by",
        }
        for position, rows in grouped.items():
            rows.sort(key=lambda r: (r.time_msc, r.ticket))
            incoming = next((r for r in rows if entry_names.get(r.entry) == "in"), None)
            original_incoming = incoming if incoming is not None and str(incoming.order) == position else None

            def money(value):
                return {"amount": decimal(value), "currency": account.currency}

            scope = self._identity["account_scope_id"]
            result.append(
                {
                    "trade_id": hashlib.sha256((scope + ":" + position).encode()).hexdigest()[:32],
                    "account_scope_id": scope,
                    "broker_position_id": position,
                    "symbol": rows[0].symbol,
                    "side": ("long" if original_incoming.type == self._sdk.DEAL_TYPE_BUY else "short")
                    if original_incoming
                    else None,
                    "opened_at": self._timebase.event_time(original_incoming.time_msc / 1000)
                    if original_incoming
                    else None,
                    "opening_complete": original_incoming is not None,
                    "captured_at": now(),
                    "currency": account.currency,
                    "deals": [
                        {
                            "deal_id": str(r.ticket),
                            "event_time": self._timebase.event_time(r.time_msc / 1000),
                            "volume_lots": decimal(r.volume),
                            "price": decimal(r.price),
                            "realized_profit": money(r.profit),
                            "commission": money(r.commission),
                            "swap": money(r.swap),
                            "other_fee": money(r.fee),
                            "entry_kind": entry_names.get(r.entry, "unknown"),
                        }
                        for r in rows
                    ],
                }
            )
        return result

    def _calculation(self, kind, params, account):
        if params["side"] not in {"long", "short"}:
            raise BridgeFailure("SIDE_INVALID", "方向必须为 long 或 short")
        order_type = self._sdk.ORDER_TYPE_BUY if params["side"] == "long" else self._sdk.ORDER_TYPE_SELL
        volume, entry = Decimal(params["volume_lots"]), Decimal(params["entry_price"])
        if volume <= 0 or entry <= 0:
            raise BridgeFailure("VALUE_INVALID", "手数和入场价必须为正")
        if kind == "profit":
            result = self._sdk.order_calc_profit(
                order_type,
                params["symbol"],
                float(volume),
                float(entry),
                float(Decimal(params["exit_price"])),
            )
        else:
            result = self._sdk.order_calc_margin(order_type, params["symbol"], float(volume), float(entry))
        if result is None:
            raise BridgeFailure("CALCULATION_UNAVAILABLE", "终端未返回本次只读试算结果", True)
        return {
            "amount": decimal(result),
            "currency": account.currency,
            "source_kind": "mt5",
            "account_scope_id": self._identity["account_scope_id"],
            "symbol": params["symbol"],
            "side": params["side"],
            "volume_lots": params["volume_lots"],
            "entry_price": params["entry_price"],
            "captured_at": now(),
        }

    def close(self):
        if self._sdk is not None:
            self._sdk.shutdown()
        self._connected_path = None
