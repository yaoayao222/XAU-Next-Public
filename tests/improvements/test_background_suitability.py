import copy

import pytest

from tests.guidance.test_workflow import Flow
from tests.scalp.test_ready_flow import create_and_import, install_market_path, local_candidate
from tests.scalp.test_suitability import market, plan, NOW
from xau_next.domains.plans.advice import rule_check
from xau_next.shared.scalp import assess_scalp, DEFAULT_SCALP_PREFERENCES
from xau_next.shared.values import parse_utc


def background_facts():
    snapshot = market()
    candidate = plan()
    candidate['entry_checks'] = [{'kind': 'bar_close', 'timeframe': 'M5', 'operator': 'lt',
        'price': '3997.90', 'observation': 'background', 'purpose': '合成：持续保持的M5背景，不是新穿越事件。'}]
    snapshot['series'][1].update(status={'state': 'available', 'captured_at': NOW, 'reasons': []}, bars=[
        {'open_at': '2026-10-14T23:50:00Z', 'close_at': '2026-10-14T23:55:00Z', 'closed': True, 'close': '3997.80'},
        {'open_at': '2026-10-14T23:55:00Z', 'close_at': NOW, 'closed': True, 'close': '3997.80'}])
    return candidate, snapshot


@pytest.mark.parametrize('side', ['long', 'short'])
def test_satisfied_background_has_no_new_trigger_distance(side):
    candidate, snapshot = background_facts()
    candidate.update(side=side, stop_loss='3999.00' if side == 'long' else '4001.40',
                     take_profit='4003.20' if side == 'long' else '3997.20')
    original = copy.deepcopy((candidate, snapshot))
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    assert result['state'] == 'compatible'
    trigger = result['trigger_distances'][0]
    assert trigger['state'] == 'background_satisfied' and trigger['distance'] is None
    assert '等待合法的新M5' not in result['required_path']
    assert (candidate, snapshot) == original


@pytest.mark.parametrize('defect', ['missing', 'stale', 'duplicate', 'unclosed', 'future', 'unavailable'])
def test_missing_background_evidence_stays_unknown(defect):
    candidate, snapshot = background_facts()
    series = snapshot['series'][1]
    if defect == 'missing':
        series['bars'] = []
    elif defect == 'stale':
        series['status']['captured_at'] = '2026-10-14T23:58:00Z'
    elif defect == 'duplicate':
        series['bars'].append(copy.deepcopy(series['bars'][-1]))
    elif defect == 'unclosed':
        series['bars'] = [{**series['bars'][-1], 'closed': False}]
    elif defect == 'future':
        series['bars'] = [{**series['bars'][-1], 'open_at': NOW, 'close_at': '2026-10-15T00:05:00Z'}]
    else:
        series['status']['state'] = 'unavailable'
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    assert result['state'] == 'unknown'
    assert result['trigger_distances'][0]['state'] == 'unknown'
    assert result['trigger_distances'][0]['distance'] is None
    assert rule_check(candidate['entry_checks'][0], snapshot, NOW, 10000)['matched'] is None


def test_background_is_rechecked_not_latched_or_quote_substituted():
    candidate, snapshot = background_facts()
    snapshot['series'][1]['bars'][-1]['close'] = '3998.00'
    snapshot['quote'].update(bid='3997.70', ask='3997.90')
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    trigger = result['trigger_distances'][0]
    assert trigger['state'] == 'background_waiting' and trigger['distance'] is None
    assert rule_check(candidate['entry_checks'][0], snapshot, NOW, 10000)['matched'] is False


@pytest.mark.parametrize('observation', [None, 'new_since_available', 'new_since_activation'])
def test_old_and_new_event_distance_limits_remain(observation):
    candidate, snapshot = background_facts()
    if observation is None:
        candidate['entry_checks'][0].pop('observation')
    else:
        candidate['entry_checks'][0]['observation'] = observation
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    assert 'TRIGGER_DISTANCE_EXCEEDED' in result['codes']
    assert result['trigger_distances'][0]['distance'] == '2.11'
    assert '等待合法的新M5' in result['required_path']


def test_quote_background_uses_requested_side():
    candidate, snapshot = background_facts()
    candidate['entry_checks'][0].update(kind='quote_compare', quote_side='ask', price='4000.10')
    candidate['entry_checks'][0].pop('timeframe')
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    assert result['trigger_distances'][0]['state'] == 'background_waiting'
    candidate['entry_checks'][0]['quote_side'] = 'bid'
    result = assess_scalp(candidate, snapshot, DEFAULT_SCALP_PREFERENCES)
    assert result['trigger_distances'][0]['state'] == 'background_satisfied'


def test_ready_then_background_loss_waits_without_risk_relaxation(tmp_path, monkeypatch):
    flow = Flow(tmp_path)
    try:
        path = install_market_path(flow, monkeypatch)
        original = flow.w.market._bridge.request
        background = {'close': '97.80'}

        def source(operation, params):
            response = original(operation, params)
            if operation == 'snapshot':
                for series in response['result']['market']['series']:
                    if series['timeframe'] == 'M5':
                        for bar in series['bars']:
                            bar.update(open=background['close'], high=background['close'],
                                       low=background['close'], close=background['close'])
            return response

        monkeypatch.setattr(flow.w.market._bridge, 'request', source)

        def candidate(answer):
            definition = local_candidate(answer)
            definition['entry_checks'] = [{'kind': 'bar_close', 'timeframe': 'M5', 'operator': 'lt',
                'price': '97.90', 'observation': 'background', 'purpose': '合成：持续M5背景，无新闭柱要求。'}]

        request, raw = create_and_import(flow, candidate)
        path['m1_closes'][parse_utc('2026-10-12T02:01:00Z')] = '100.30'
        flow.clock.value = '2026-10-12T02:01:02Z'
        flow.refresh()
        workspace = flow.get('/v1/workspace')
        assert workspace['plans'][0]['lifecycle'] == 'ACTIVE'
        assert all(workspace['evaluations'][0][name]['state'] == 'satisfied'
            for name in ['quote_check', 'cost_check', 'budget_check', 'margin_check'])
        advice = workspace['action_advice'][0]
        assert advice['state'] == 'READY', advice
        assert advice['scalp_suitability']['range_net_check']['state'] == 'satisfied'
        assert flow.get('/v1/settings')['values']['min_net_rr'] == '2'
        background['close'] = '98.00'
        flow.clock.value = '2026-10-12T02:05:02Z'
        flow.refresh()
        advice = flow.get('/v1/workspace')['action_advice'][0]
        assert advice['state'] == 'WAITING', advice
        assert advice['scalp_suitability']['trigger_distances'][-1]['state'] == 'background_waiting'
        assert flow.get('/v1/forecasts/' + request['forecast_id'])['current_version']['raw_text'] == raw
    finally:
        flow.close()
