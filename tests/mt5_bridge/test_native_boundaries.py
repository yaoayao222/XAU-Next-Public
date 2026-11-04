import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from xau_mt5_bridge.native import MT5Source
from xau_mt5_bridge.protocol import BridgeFailure, utc
from xau_mt5_bridge.timebase import Timebase


@pytest.mark.skipif(os.name != "nt", reason="Windows process discovery")
def test_repeated_process_discovery_does_not_retain_per_call_allocations():
    import gc
    import tracemalloc

    from xau_mt5_bridge.windows import running_terminals

    tracemalloc.start()
    try:
        running_terminals()
        gc.collect()
        before = tracemalloc.take_snapshot()
        for _ in range(80):
            running_terminals()
        gc.collect()
        after = tracemalloc.take_snapshot()
        retained = sum(
            s.size_diff
            for s in after.compare_to(before, "lineno")
            if str(s.traceback[0].filename).endswith("xau_mt5_bridge\\windows.py")
        )
        assert retained < 64 * 1024, f"80 read-only process scans retained {retained} bytes"
    finally:
        tracemalloc.stop()


def test_fixed_user_timezone_normalizes_events_and_reverses_history_queries():
    timebase = Timebase("UTC+03:00")
    encoded = datetime(2026, 10, 12, 9, 31, tzinfo=UTC).timestamp()
    assert timebase.event_time(encoded) == "2026-10-12T06:31:00.000Z"
    assert timebase.query_time(utc("2026-10-12T06:31:00Z")).hour == 9
    assert Timebase(None).event_time(encoded) == "2026-10-12T09:31:00.000Z"  # No inferred correction.


def test_closed_candles_use_normalized_time_and_orders_keep_broker_identity():
    source = MT5Source()
    source._timebase = Timebase("UTC+03:00")
    encoded = int(datetime(2026, 10, 12, 9, 30, tzinfo=UTC).timestamp())
    rates = [
        {"time": encoded + offset, "open": 100, "high": 101, "low": 99, "close": 100, "tick_volume": 1}
        for offset in (0, 60)
    ]
    result = source._series("M1", rates, "2026-10-12T06:31:10Z", "bid")
    assert [bar["closed"] for bar in result["bars"]] == [True, False]
    assert result["bars"][0]["open_at"] == "2026-10-12T06:30:00.000Z"
    source._sdk = SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_BUY_LIMIT=2, ORDER_TYPE_BUY_STOP=4, ORDER_TYPE_BUY_STOP_LIMIT=6
    )
    row = SimpleNamespace(
        ticket=123,
        symbol="XAUUSD",
        type=0,
        volume=0.01,
        price_open=100,
        sl=99,
        tp=101,
        time_msc=encoded * 1000,
    )
    order = source._order(row, "position", "account-id", "2026-10-12T06:31:10Z")
    assert order["order"] == {"account_scope_id": "account-id", "kind": "position", "broker_id": "123"}
    assert order["event_time"] == "2026-10-12T06:30:00.000Z"


def test_no_running_terminal_fails_before_sdk_initialize(monkeypatch):
    from xau_mt5_bridge import native

    monkeypatch.setattr(native, "running_terminals", list)
    source = MT5Source()
    with pytest.raises(BridgeFailure) as failure:
        source.handle("snapshot", {})
    assert failure.value.code == "TERMINAL_NOT_RUNNING"
    assert source._sdk is None


@pytest.mark.skipif(os.name != "nt", reason="Windows process guard")
def test_native_guard_prevents_any_child_process():
    root = Path(__file__).resolve().parents[2]
    source = root / "apps/mt5_bridge/src"
    if not source.exists():
        source = root / "bridge/src"  # Actual packaged layout.
    code = """from xau_mt5_bridge.windows import prevent_child_processes
import subprocess,sys
prevent_child_processes()
try:
    p=subprocess.Popen([sys.executable,'-c','pass'])
except OSError:
    print('blocked')
else:
    p.wait()
    raise AssertionError('child process was allowed')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "blocked"
