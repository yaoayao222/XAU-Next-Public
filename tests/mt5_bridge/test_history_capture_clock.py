from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from xau_mt5_bridge import native
from xau_mt5_bridge.native import MT5Source
from xau_mt5_bridge.timebase import Timebase


@pytest.mark.parametrize('tick_delta,missing,valid', [(4, False, True), (10804, False, False), (4, True, False)])
def test_history_latency_does_not_mask_real_future_clock(monkeypatch, tick_delta, missing, valid):
    encoded = datetime(2026, 10, 23, 15, tzinfo=UTC).timestamp()
    source = MT5Source()
    source._timebase = Timebase('UTC+03:00')
    source._identity = {'account_scope_id': 'synthetic-account'}
    times = iter(['2026-10-23T12:00:00.000Z', '2026-10-23T12:00:05.000Z'])
    monkeypatch.setattr(native, 'now', lambda: next(times))
    start = datetime(2026, 10, 23, 14, tzinfo=UTC).timestamp()
    source._sdk = SimpleNamespace(
        TIMEFRAME_M1=1, COPY_TICKS_ALL=0,
        copy_rates_range=lambda *a: [{'time': start, 'open':100, 'high':101, 'low':99, 'close':100, 'tick_volume':2}],
        copy_ticks_range=lambda *a: [{'time_msc': int((start+.05)*1000), 'bid':100,'ask':100.1,'flags':6}, {'time_msc':int((start+59.95)*1000),'bid':100,'ask':100.1,'flags':6}],
        symbol_info=lambda *a: SimpleNamespace(chart_mode=0),
        symbol_info_tick=lambda *a: None if missing else SimpleNamespace(time_msc=int((encoded+tick_delta)*1000)),
    )
    source._read_deals = lambda *a, **kw: {'trades': [], 'raw_deals': [], 'status': {'state':'available'}}
    result = source._history({'symbol':'XAUUSD','from':'2026-10-23T11:00:00.000Z','to':'2026-10-23T11:01:00.000Z'}, SimpleNamespace())
    assert result['coverage']['complete'] is valid
    assert result['bars'][0]['closed'] is valid
    assert any('TIME_BASIS_UNKNOWN' in x for x in result['status']['reasons']) is (not valid)
    assert result['raw']['history_timing']['history_started_at'] != result['raw']['history_timing']['tick_checked_at']
