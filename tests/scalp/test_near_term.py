import copy
from datetime import timedelta
from decimal import Decimal

import pytest
from xau_next.shared.closed_bars import HISTORY_GAPS
from xau_next.shared.near_term import build_near_term, project_near_term
from xau_next.shared.values import parse_utc


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def market():
    cutoff = parse_utc("2026-10-15T02:10:27Z")
    end = cutoff.replace(second=0)
    source = {"state": "available", "observed_at": stamp(end),
              "captured_at": stamp(cutoff + timedelta(seconds=1)), "reasons": []}
    bars = []
    for index in range(15):
        start = end - timedelta(minutes=15 - index)
        price = Decimal(4000) + Decimal(index) / 10
        bars.append({"open_at": stamp(start), "close_at": stamp(start + timedelta(minutes=1)),
                     "open": str(price), "high": str(price + Decimal(".3")),
                     "low": str(price - Decimal(".2")), "close": str(price + Decimal(".1")),
                     "closed": True, "tick_volume": 20})
    five = []
    for index in range(3):
        chunk = bars[index * 5:(index + 1) * 5]
        five.append({"open_at": chunk[0]["open_at"], "close_at": chunk[-1]["close_at"],
                     "open": chunk[0]["open"], "close": chunk[-1]["close"],
                     "high": str(max(Decimal(bar["high"]) for bar in chunk)),
                     "low": str(min(Decimal(bar["low"]) for bar in chunk)),
                     "closed": True, "tick_volume": 100})
    return {"snapshot_id": "synthetic-near-term", "source_kind": "synthetic",
            "market_cutoff": stamp(cutoff), "captured_at": source["captured_at"],
            "quote": {"bid": "4001.50", "ask": "4001.70", "event_time": stamp(cutoff)},
            "quote_status": copy.deepcopy(source),
            "series": [{"timeframe": "M1", "price_basis": "bid", "status": copy.deepcopy(source), "bars": bars},
                       {"timeframe": "M5", "price_basis": "bid", "status": copy.deepcopy(source), "bars": five}]}


def windows(result):
    return {item["window_minutes"]: item for item in result["m1_windows"]}


def test_price_changes_are_decimal_price_dollars_and_fixed_closed_windows():
    source = market()
    original = copy.deepcopy(source)
    result = build_near_term(source)
    assert source == original
    assert result["version"] == "near-term/1"
    assert result["snapshot_id"] == source["snapshot_id"]
    assert result["source_kind"] == "synthetic"
    assert result["unit"] == "XAUUSD_price_dollars"
    assert result["quote"]["spread"] == "0.20"
    assert result["quote"]["age_seconds"] == 0
    for duration, window in windows(result).items():
        assert window["state"] == "available"
        assert window["sample_count"] == duration
        assert window["continuity_complete"] is True
        assert window["window_end"] == "2026-10-15T02:10:00Z"
        assert Decimal(window["change"]) == Decimal(duration) / 10
        assert Decimal(window["range"]) == Decimal(duration) / 10 + Decimal(".4")


def test_local_extrema_identify_bar_times_not_invented_tick_time():
    result = build_near_term(market())
    high = result["local_extrema"]["high"]
    low = result["local_extrema"]["low"]
    assert high["price"] == "4001.7"
    assert high["bar_open_at"] == "2026-10-15T02:09:00Z"
    assert high["bar_close_at"] == "2026-10-15T02:10:00Z"
    assert high["age_seconds"] == 27
    assert low["price"] == "4000.3"
    assert low["age_seconds"] == 567
    assert result["local_extrema"]["age_basis"] == "bar_close_at"
    assert high["quote_distance"]["quote_basis"] == "bid"
    assert high["quote_distance"]["signed_distance"] == "0.20"
    assert low["quote_distance"]["absolute_distance"] == "1.20"


def test_mean_true_range_has_ten_samples_and_previous_close():
    result = build_near_term(market())["m1_true_range"]
    assert result["state"] == "available"
    assert result["sample_count"] == 10
    assert result["input_bar_count"] == 11
    assert result["previous_close_at"] == "2026-10-15T02:00:00Z"
    assert result["value"] == "0.5"
    assert "previous_close" in result["algorithm"]


def test_true_range_includes_gap_from_previous_close():
    source = market()
    source["series"][0]["bars"][-11].update(open="3990", high="3991", low="3989", close="3990")
    result = build_near_term(source)["m1_true_range"]
    assert result["state"] == "available"
    assert result["value"] == "1.53"


def test_tied_extrema_use_latest_matching_bar_and_report_the_rule():
    source = market()
    source["series"][0]["bars"][-2]["high"] = "4001.7"
    result = build_near_term(source)["local_extrema"]
    assert result["high"]["bar_close_at"] == "2026-10-15T02:10:00Z"
    assert "ties=latest_bar" in result["algorithm"]


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unclosed", "future", "duration", "ohlc", "nan"])
def test_invalid_latest_window_never_falls_back_to_older_complete_bars(damage):
    source = market()
    bars = source["series"][0]["bars"]
    if damage == "missing":
        bars.pop()
    elif damage == "duplicate":
        bars.append(copy.deepcopy(bars[-1]))
    elif damage == "unclosed":
        bars[-1]["closed"] = False
    elif damage == "future":
        bars[-1]["close_at"] = "2026-10-15T02:11:00Z"
    elif damage == "duration":
        bars[-1]["open_at"] = "2026-10-15T02:08:59Z"
    elif damage == "ohlc":
        bars[-1]["high"] = "3990"
    else:
        bars[-1]["high"] = "NaN"
    result = build_near_term(source)
    for window in result["m1_windows"]:
        assert window["state"] == "unknown"
        assert window["change"] is None
        assert window["range"] is None
        assert window["continuity_complete"] is False
        assert window["reasons"]
    assert result["local_extrema"]["high"] is None
    assert result["m1_true_range"]["value"] is None


def test_future_and_current_unclosed_extra_bars_do_not_enter_statistics():
    source = market()
    before = build_near_term(source)
    source["series"][0]["bars"].append({**source["series"][0]["bars"][-1],
                                       "open_at": "2026-10-15T02:10:00Z", "close_at": "2026-10-15T02:11:00Z",
                                       "closed": False, "high": "99999"})
    after = build_near_term(source)
    assert windows(after)[10]["high"] == windows(before)[10]["high"]
    assert after["local_extrema"] == before["local_extrema"]


def test_short_windows_remain_available_when_ten_minute_window_is_incomplete():
    source = market()
    del source["series"][0]["bars"][-8]
    result = windows(build_near_term(source))
    assert all(result[duration]["state"] == "available" for duration in (1, 3, 5))
    assert result[10]["state"] == "unknown"
    assert result[10]["sample_count"] == 9


def test_only_explicit_historical_gap_status_can_be_locally_revalidated():
    source = market()
    source["series"][0]["bars"].pop(0)
    source["series"][0]["status"].update(state="incomplete", reasons=[HISTORY_GAPS])
    result = windows(build_near_term(source))[10]
    assert result["state"] == "available"
    assert result["source_state"] == "incomplete"
    assert result["source_reasons"] == [HISTORY_GAPS]
    assert result["reasons"]
    source["series"][0]["status"]["reasons"].append("时间来源未知")
    blocked = windows(build_near_term(source))[10]
    assert blocked["state"] == "unknown"
    assert blocked["change"] is None


def test_history_gap_exception_requires_adjacent_closed_evidence_for_one_minute():
    source = market()
    source["series"][0]["status"].update(state="incomplete", reasons=[HISTORY_GAPS])
    assert windows(build_near_term(source))[1]["state"] == "available"
    del source["series"][0]["bars"][-2]
    result = windows(build_near_term(source))[1]
    assert result["state"] == "unknown"
    assert result["change"] is None


@pytest.mark.parametrize("state", ["stale", "unavailable", "incomplete"])
def test_untrusted_series_state_is_not_erased_by_good_prices(state):
    source = market()
    source["series"][0]["status"].update(state=state, reasons=["来源不能确认"])
    result = build_near_term(source)
    assert windows(result)[1]["state"] == "unknown"
    assert result["m1_true_range"]["value"] is None


@pytest.mark.parametrize("basis,expected", [("bid", "0.20"), ("ask", "0.00"), ("mid", "0.10")])
def test_structure_distance_uses_matching_quote_basis(basis, expected):
    source = market()
    source["series"][0]["price_basis"] = basis
    distance = build_near_term(source)["local_extrema"]["high"]["quote_distance"]
    assert distance["quote_basis"] == basis
    assert distance["signed_distance"] == expected
    assert distance["spread_conversion"] is None


@pytest.mark.parametrize("damage", ["missing", "future", "stale", "negative_spread"])
def test_quote_failure_keeps_statistics_but_distance_unknown(damage):
    source = market()
    if damage == "missing":
        source["quote"] = None
    elif damage == "future":
        source["quote"]["event_time"] = "2026-10-15T02:10:28Z"
    elif damage == "stale":
        source["quote_status"]["state"] = "stale"
    else:
        source["quote"]["ask"] = "4000"
    result = build_near_term(source)
    assert result["quote"]["state"] == "unknown"
    assert result["quote"]["spread"] is None
    assert windows(result)[10]["state"] == "available"
    distance = result["local_extrema"]["high"]["quote_distance"]
    assert distance["state"] == "unknown"
    assert distance["absolute_distance"] is None


def test_unknown_basis_duplicate_series_and_missing_previous_close_stay_unknown():
    source = market()
    source["series"][0]["price_basis"] = "unknown"
    assert windows(build_near_term(source))[10]["state"] == "unknown"
    source = market()
    source["series"].append(copy.deepcopy(source["series"][0]))
    assert windows(build_near_term(source))[1]["state"] == "unknown"
    source = market()
    del source["series"][0]["bars"][-11]
    result = build_near_term(source)
    assert windows(result)[10]["state"] == "available"
    assert result["m1_true_range"]["value"] is None


def test_m5_structure_uses_last_two_complete_aligned_bars():
    source = market()
    result = build_near_term(source)["m5_structure"]
    assert result["state"] == "available"
    assert result["sample_count"] == 2
    assert result["window_start"] == "2026-10-15T02:00:00Z"
    assert result["window_end"] == "2026-10-15T02:10:00Z"
    assert result["latest_close"] == "4001.5"
    source["series"][1]["bars"].pop()
    assert build_near_term(source)["m5_structure"]["state"] == "unknown"


def test_explicit_earlier_cutoff_excludes_later_quote_and_bars():
    result = build_near_term(market(), "2026-10-15T02:09:30Z")
    assert result["cutoff"] == "2026-10-15T02:09:30Z"
    assert result["quote"]["state"] == "unknown"
    assert windows(result)[1]["window_end"] == "2026-10-15T02:09:00Z"
    assert windows(result)[1]["high"] == "4001.6"


def test_cannot_expand_snapshot_cutoff_or_accept_invalid_time_metadata():
    source = market()
    result = build_near_term(source, "2026-10-15T02:11:00Z")
    assert windows(result)[1]["state"] == "unknown"
    source["series"][0]["status"]["captured_at"] = None
    assert windows(build_near_term(source))[1]["state"] == "unknown"


@pytest.mark.parametrize("complete", [True, False])
def test_api_projection_preserves_every_known_nested_path_and_null_or_zero(complete):
    source = market()
    if not complete:
        source["series"][0]["bars"].pop()
        source["quote"] = None
    raw = build_near_term(source)
    original = copy.deepcopy(raw)
    projected = project_near_term(raw)
    assert projected == raw
    assert raw == original
    assert projected["quote"]["sample_count"] == (1 if complete else 0)
    assert projected["quote"]["age_seconds"] == (0 if complete else None)
    projected["quote"]["reasons"].append("仅修改返回副本")
    assert raw == original


def test_api_projection_rejects_arbitrary_extensions_at_every_object_level():
    original = build_near_term(market())
    tampered = copy.deepcopy(original)

    def inject(value):
        if isinstance(value, dict):
            for nested in list(value.values()):
                inject(nested)
            value.update(private="private-field", login=123, api_key="synthetic-not-a-secret")
        elif isinstance(value, list):
            for nested in value:
                inject(nested)

    inject(tampered)
    assert project_near_term(tampered) == original


def test_api_projection_does_not_invent_missing_optional_content():
    assert project_near_term(None) is None
    assert project_near_term({}) == {}
    raw = {"version": "near-term/1", "quote": {"spread": None, "age_seconds": 0},
           "m1_windows": [{"sample_count": 0, "change": None}],
           "local_extrema": {"high": None}, "m5_structure": {"local_extrema": None}}
    assert project_near_term(raw) == raw
