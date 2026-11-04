"""AC-13: ordinary snapshots collect independent, explicitly sourced deal history."""

import json
from collections import namedtuple
from datetime import timedelta

import pytest
from xau_mt5_bridge import native
from xau_mt5_bridge.native import MT5Source
from xau_mt5_bridge.protocol import BridgeFailure, utc
from xau_mt5_bridge.synthetic import SyntheticSource
from xau_mt5_bridge.timebase import Timebase

AT = "2026-10-12T08:00:00.000Z"


def record(**values):
    return namedtuple("SdkRecord", values)(**values)


def encoded_time(at):
    return int(Timebase("UTC+03:00").query_time(utc(at)).timestamp() * 1000)


def deal(ticket, entry, at, *, position=101, order=None, symbol="XAUUSD"):
    return record(
        ticket=ticket,
        position_id=position,
        order=order or position,
        type=0 if entry == 0 else 1,
        entry=entry,
        symbol=symbol,
        time_msc=encoded_time(at),
        time=encoded_time(at) // 1000,
        volume=0.01,
        price=100 if entry == 0 else 101,
        profit=0 if entry == 0 else 1,
        commission=-0.10,
        swap=0,
        fee=0,
    )


class FakeSdk:
    TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1 = 1, 5, 15, 60
    COPY_TICKS_ALL = 0
    SYMBOL_CALC_MODE_FOREX = 0
    DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
    ORDER_TYPE_BUY, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_BUY_STOP, ORDER_TYPE_BUY_STOP_LIMIT = 0, 2, 4, 6

    def __init__(self, failure=None):
        self.deal_calls = []
        self.failure = failure
        self.rows = (
            deal(201, 0, "2026-10-10T08:00:00.000Z"),
            deal(202, 1, "2026-10-12T07:59:00.000Z", order=202),
        )

    def symbol_info(self, symbol):
        return record(
            trade_contract_size=100,
            trade_tick_size=0.01,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100,
            currency_profit="USD",
            currency_margin="USD",
            trade_calc_mode=0,
            chart_mode=0,
        )

    def symbol_info_tick(self, symbol):
        return record(time=encoded_time(AT) // 1000, time_msc=encoded_time(AT), bid=100, ask=100.02)

    def positions_get(self):
        return ()

    def orders_get(self):
        return ()

    def copy_rates_from_pos(self, symbol, timeframe, position, count):
        return [
            {
                "time": encoded_time(AT) // 1000 - timeframe * 60,
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "tick_volume": 1,
            }
        ]

    def copy_rates_range(self, symbol, timeframe, start, end):
        return [
            {
                "time": int((start + timedelta(minutes=index)).timestamp()),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "tick_volume": 1,
            }
            for index in range(int((end - start).total_seconds() // 60))
        ]

    def copy_ticks_range(self, symbol, start, end, flags):
        return [
            {"time_msc": int(at.timestamp() * 1000), "bid": 100, "ask": 100.02, "flags": 0}
            for at in (start, end)
        ]

    def history_deals_get(self, start, end):
        self.deal_calls.append((start, end))
        if self.failure == "none":
            return None
        if self.failure == "exception":
            raise RuntimeError("synthetic SDK read failure")
        return tuple(
            row for row in self.rows if start.timestamp() * 1000 <= row.time_msc <= end.timestamp() * 1000
        )

    def last_error(self):
        return (-1, "synthetic SDK history unavailable")


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(native, "now", lambda: AT)
    value = MT5Source()
    value._sdk = FakeSdk()
    value._identity = {"account_scope_id": "synthetic-native-account"}
    value._timebase = Timebase("UTC+03:00")
    terminal = record(connected=True)
    account = record(currency="USD", leverage=100, balance=100, equity=101, margin_free=100, margin=1)
    return value, terminal, account


def quote_row(at, *, bid=100, ask=100.02):
    return {"time": encoded_time(at) // 1000, "time_msc": encoded_time(at),
            "bid": bid, "ask": ask, "flags": 6}


def test_snapshot_resolves_226ms_later_tick_to_last_real_pre_cutoff_quote(source, monkeypatch):
    value, terminal, account = source
    cutoff = "2026-10-13T05:43:01.308Z"
    future = record(**quote_row("2026-10-13T05:43:01.534Z", bid=4191.81, ask=4191.91))
    prior = quote_row("2026-10-13T05:43:01.244Z", bid=4191.80, ask=4191.90)
    monkeypatch.setattr(native, "now", lambda: cutoff)
    monkeypatch.setattr(value._sdk, "symbol_info_tick", lambda symbol: future)
    calls = []

    def ticks(symbol, start, end, flags):
        calls.append((symbol, start, end, flags))
        # The SDK can return the whole final second; exact filtering belongs to us.
        return [quote_row("2026-10-13T05:43:00.999Z"), prior, future._asdict()]

    monkeypatch.setattr(value._sdk, "copy_ticks_range", ticks)
    result = value._snapshot({"symbol": "XAUUSD", "quote_max_age_ms": 10000}, terminal, account)
    market = result["market"]
    assert market["quote"] == {"bid": "4191.8", "ask": "4191.9", "event_time": "2026-10-13T05:43:01.244Z"}
    assert market["market_cutoff"] == cutoff and market["quote_status"]["state"] == "available"
    assert market["quote_status"]["observed_at"] == market["quote"]["event_time"]
    assert len(calls) == 1 and calls[0][1] == Timebase("UTC+03:00").query_time(utc(cutoff) - timedelta(seconds=10))
    assert result["raw"]["tick"] == future._asdict()
    assert result["raw"]["snapshot_quote_selection"]["selected_tick"] == prior
    assert result["raw"]["snapshot_quote_selection"]["cutoff"] == cutoff


@pytest.mark.parametrize("history_case", ["none", "exception", "empty", "future_only", "stale", "invalid", "unordered", "ambiguous"])
def test_pre_cutoff_quote_failure_is_explicit_and_does_not_invent_available_quote(source, monkeypatch, history_case):
    value, terminal, account = source
    monkeypatch.setattr(value._sdk, "symbol_info_tick", lambda symbol: record(**quote_row("2026-10-12T08:00:00.226Z")))

    def ticks(*args):
        if history_case == "exception":
            raise RuntimeError("synthetic tick history failure")
        return {
            "none": None, "empty": [],
            "future_only": [quote_row("2026-10-12T08:00:00.001Z")],
            "stale": [quote_row("2026-10-12T07:59:49.999Z")],
            "invalid": [quote_row("2026-10-12T07:59:59.999Z", bid=101, ask=100)],
            "unordered": [quote_row(AT), quote_row("2026-10-12T07:59:59.999Z")],
            "ambiguous": [quote_row(AT), quote_row(AT, bid=101, ask=101.02)],
        }[history_case]

    monkeypatch.setattr(value._sdk, "copy_ticks_range", ticks)
    result = value._snapshot({"symbol": "XAUUSD", "quote_max_age_ms": 10000}, terminal, account)
    assert result["market"]["quote"] is None
    assert result["market"]["quote_status"]["state"] == "unavailable"
    assert result["market"]["quote_status"]["reasons"]
    assert result["market"]["contract_status"]["state"] == "available"
    assert result["portfolio"]["account_status"]["state"] == "available"
    assert result["raw"]["tick"]["time_msc"] == encoded_time("2026-10-12T08:00:00.226Z")


def test_pre_cutoff_quote_accepts_exact_boundary_and_skips_invalid_price(source, monkeypatch):
    value, terminal, account = source
    monkeypatch.setattr(value._sdk, "symbol_info_tick", lambda symbol: record(**quote_row("2026-10-12T08:00:00.226Z")))
    monkeypatch.setattr(value._sdk, "copy_ticks_range", lambda *args: [
        quote_row("2026-10-12T07:59:59.999Z", bid=0, ask=0), quote_row(AT),
        quote_row("2026-10-12T08:00:00.001Z", bid=999, ask=999.01),
    ])
    result = value._snapshot({"symbol": "XAUUSD"}, terminal, account)
    assert result["market"]["quote"] == {"event_time": AT, "bid": "100", "ask": "100.02"}
    assert result["market"]["quote_status"]["state"] == "available"


@pytest.mark.parametrize(("tick_time", "state"), [(AT, "available"), ("2026-10-12T08:00:01.001Z", "incomplete")])
def test_quote_selection_does_not_mask_large_clock_error_or_query_for_normal_tick(source, monkeypatch, tick_time, state):
    value, terminal, account = source
    monkeypatch.setattr(value._sdk, "symbol_info_tick", lambda symbol: record(**quote_row(tick_time)))

    def unexpected(*args):
        pytest.fail("Normal ticks and large clock errors must not query a replacement quote")

    monkeypatch.setattr(value._sdk, "copy_ticks_range", unexpected)
    result = value._snapshot({"symbol": "XAUUSD"}, terminal, account)
    assert result["market"]["quote"]["event_time"] == tick_time
    assert result["market"]["quote_status"]["state"] == state
    if state == "incomplete":
        assert all(not bar["closed"] for series in result["market"]["series"] for bar in series["bars"])


def test_ordinary_snapshot_reads_deals_and_preserves_raw_with_default_30_days(source):
    value, terminal, account = source
    result = value._snapshot({"symbol": "XAUUSD", "include_history": False}, terminal, account)
    assert len(value._sdk.deal_calls) == 1
    start, end = value._sdk.deal_calls[0]
    expected_end = Timebase("UTC+03:00").query_time(utc(AT))
    assert end == expected_end and start == expected_end - timedelta(days=30)
    assert result["history"] is None
    assert result["portfolio"]["trades_status"]["state"] == "available"
    assert len(result["trades"]) == 1 and len(result["trades"][0]["deals"]) == 2
    trade = result["trades"][0]
    assert trade["account_scope_id"] == "synthetic-native-account"
    assert trade["opened_at"] == "2026-10-10T08:00:00.000Z"
    assert trade["deals"][1]["event_time"] == "2026-10-12T07:59:00.000Z"
    assert len(result["raw"]["deals"]) == 2
    assert result["raw"]["trade_history_read"]["history_days"] == 30
    assert result["raw"]["trade_history_read"]["query_to_encoded"] == "2026-10-12T11:00:00.000Z"
    json.dumps(result)  # The real bridge writes exactly this JSON-lines payload.


def test_configured_history_days_does_not_invent_missing_opening(source):
    value, terminal, account = source
    result = value._snapshot({"symbol": "XAUUSD", "history_days": 1}, terminal, account)
    start, end = value._sdk.deal_calls[0]
    assert end - start == timedelta(days=1)
    assert result["portfolio"]["trades_status"]["state"] == "available"
    assert len(result["trades"][0]["deals"]) == 1
    assert result["trades"][0]["opened_at"] is None and result["trades"][0]["opening_complete"] is False


def test_short_market_replay_window_does_not_truncate_normal_trade_collection(source):
    value, terminal, account = source
    result = value._snapshot(
        {
            "symbol": "XAUUSD",
            "history_days": 7,
            "include_history": True,
            "history_from": "2026-10-12T07:55:00.000Z",
            "history_to": AT,
        },
        terminal,
        account,
    )
    assert [end - start for start, end in value._sdk.deal_calls] == [timedelta(days=7), timedelta(minutes=5)]
    assert len(result["trades"][0]["deals"]) == 2 and result["trades"][0]["opening_complete"] is True
    assert len(result["history"]["trades"][0]["deals"]) == 1
    assert result["history"]["trades"][0]["opening_complete"] is False
    assert result["history"]["from"] == "2026-10-12T07:55:00.000Z"


@pytest.mark.parametrize("failure", ["none", "exception"])
@pytest.mark.parametrize("include_history", [False, True])
def test_deal_failure_preserves_other_successful_facts(source, failure, include_history):
    value, terminal, account = source
    value._sdk.failure = failure
    result = value._snapshot(
        {
            "symbol": "XAUUSD",
            "include_history": include_history,
            "history_from": "2026-10-12T07:55:00.000Z",
            "history_to": AT,
        },
        terminal,
        account,
    )
    assert result["market"]["quote_status"]["state"] == "available"
    assert result["market"]["contract_status"]["state"] == "available"
    assert result["portfolio"]["account_status"]["state"] == "available"
    assert result["portfolio"]["orders_status"]["state"] == "available"
    assert result["portfolio"]["trades_status"]["state"] == "unavailable"
    assert result["portfolio"]["trades_status"]["observed_at"] is None
    assert result["trades"] == [] and result["raw"]["deals"] is None
    assert result["raw"]["trade_history_read"]["error"]


def test_empty_success_is_distinct_from_failed_history(source):
    value, terminal, account = source
    value._sdk.rows = ()
    result = value._snapshot({"symbol": "XAUUSD"}, terminal, account)
    assert result["trades"] == [] and result["raw"]["deals"] == []
    assert result["portfolio"]["trades_status"]["state"] == "available"


def test_trade_collection_is_account_wide_not_filtered_by_chart_symbol(source):
    value, terminal, account = source
    value._sdk.rows += (deal(301, 0, "2026-10-12T06:00:00.000Z", position=302, symbol="EURUSD"),)
    result = value._snapshot({"symbol": "XAUUSD"}, terminal, account)
    assert {item["symbol"] for item in result["trades"]} == {"XAUUSD", "EURUSD"}
    assert {item["account_scope_id"] for item in result["trades"]} == {"synthetic-native-account"}


def test_account_change_during_snapshot_is_rejected_before_result_is_accepted(source, monkeypatch):
    value, terminal, account = source
    value._account_key = ("123", "synthetic-server")
    monkeypatch.setattr(value, "_ensure", lambda params: (terminal, account))
    value._sdk.account_info = lambda: record(login=456, server="synthetic-server")
    with pytest.raises(BridgeFailure) as error:
        value.handle("snapshot", {"symbol": "XAUUSD"})
    assert error.value.code == "ACCOUNT_CHANGED"


def test_synthetic_snapshot_explicitly_marks_successful_empty_trade_history():
    result = SyntheticSource().handle("snapshot", {"captured_at": AT, "history_days": 7})
    assert result["market"]["source_kind"] == "synthetic"
    assert result["portfolio"]["trades_status"]["state"] == "available"
    assert result["trades"] == [] and result["raw"]["deals"] == []
    assert result["raw"]["trade_history_read"]["method"] == "synthetic-empty-trade-history/1"
    assert utc(result["raw"]["trade_history_read"]["to"]) - utc(
        result["raw"]["trade_history_read"]["from"]
    ) == timedelta(days=7)
