"""Deterministic observations + synthetic transport; no MT5 or real provider."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from xau_next.domains.analysis.decision_context import build_decision_context, project_decision_context
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.closed_bars import HISTORY_GAPS, PERIODS
from tests.guidance.test_workflow import Flow
from tests.model_api.test_workflow import ModelFlow


def stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def sample(direction=1):
    cutoff = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
    status = {'state': 'available', 'captured_at': stamp(cutoff), 'observed_at': stamp(cutoff), 'reasons': []}
    market = {'snapshot_id': 'SYNTHETIC-market', 'account_scope_id': 'SYNTHETIC-account',
              'source_kind': 'synthetic', 'symbol': 'XAUUSD', 'market_cutoff': stamp(cutoff),
              'quote': {'bid': '102.5', 'ask': '102.6', 'event_time': stamp(cutoff)},
              'quote_status': deepcopy(status), 'series': []}
    for tf, seconds in PERIODS.items():
        bars = []
        for i in range(3):
            opened = cutoff - timedelta(seconds=seconds * (3-i))
            base = 100 + direction*i
            bars.append({'open_at': stamp(opened), 'close_at': stamp(opened+timedelta(seconds=seconds)),
                         'open': str(base), 'high': str(base+1), 'low': str(base-1),
                         'close': str(base+.5), 'closed': True, 'tick_volume': 1})
        market['series'].append({'timeframe': tf, 'price_basis': 'bid', 'status': deepcopy(status), 'bars': bars})
    request = {'market_cutoff': stamp(cutoff)}
    context = {'market': market, 'settings': {'values': {'quote_max_age_ms': 10000}},
               'timing_reference': {'planning_delay_seconds': 240}, 'previous_analysis': None}
    return request, context


def prior(context):
    context['previous_analysis'] = {'version_id': 'SYNTHETIC-prior', 'available_at': '2026-10-20T13:58:00Z',
        'market_cutoff': '2026-10-20T13:57:00Z', 'horizons': [
            {'horizon_minutes': 60, 'direction': 'short', 'end_at': '2026-10-20T14:57:00Z'}],
        'plans': [{'plan_key': 'old-short', 'trading_style': 'intraday',
                   'invalidation': {'kind': 'bar_close', 'timeframe': 'M1', 'operator': 'gt', 'price': '101'}}],
        'analysis_basis': {'reassessment_conditions': []}}


@pytest.mark.parametrize('direction,label', [(1, 'rising'), (-1, 'falling')])
def test_symmetric_closed_observations_do_not_choose_a_future_direction(direction, label):
    req, ctx = sample(direction); original = deepcopy(ctx)
    result = build_decision_context(req, ctx)
    assert all(r['state'] == 'available' and r['high_relation'] == label
               and r['low_relation'] == label and r['close_relation'] == label for r in result['observations'])
    assert result['previous']['state'] == 'unavailable'
    assert not any(k in result for k in ('direction', 'side', 'signal', 'probability', 'ready'))
    assert ctx == original


@pytest.mark.parametrize('defect', ['missing', 'duplicate', 'ohlc', 'future_close', 'wrong_side',
                                  'unavailable', 'future_capture', 'misaligned', 'repeated_source'])
def test_bad_current_window_stays_unknown_without_falling_back(defect):
    req, ctx = sample(); s = ctx['market']['series'][0]
    # The first PERIODS entry is M1.
    if defect == 'missing': s['bars'].pop()
    if defect == 'duplicate': s['bars'].append(deepcopy(s['bars'][-1]))
    if defect == 'ohlc': s['bars'][-1]['close'] = '999'
    if defect == 'future_close': s['bars'][-1]['close_at'] = '2026-10-20T14:01:00Z'
    if defect == 'wrong_side': s['price_basis'] = 'ask'
    if defect == 'unavailable': s['status']['state'] = 'unavailable'
    if defect == 'future_capture': s['status']['captured_at'] = '2026-10-20T14:01:00Z'
    if defect == 'misaligned': s['bars'][-1]['open_at'] = '2026-10-20T13:59:01Z'
    if defect == 'repeated_source': ctx['market']['series'].append(deepcopy(s))
    result = build_decision_context(req, ctx)['observations'][0]
    assert result['state'] == 'unknown' and result['change'] is None and result['highs'] is None


def test_future_bar_is_ignored_and_historical_gap_status_is_not_relabelled():
    req, ctx = sample(); s = ctx['market']['series'][0]
    s['status'].update(state='incomplete', reasons=[HISTORY_GAPS])
    s['bars'].append({'open_at': '2026-10-20T14:00:00Z', 'close_at': '2026-10-20T14:01:00Z',
                      'closed': True, 'open': '1', 'high': '99999', 'low': '1', 'close': '99999'})
    row = build_decision_context(req, ctx)['observations'][0]
    assert row['state'] == 'available' and row['source_state'] == 'incomplete'
    assert row['highs'] == ['101', '102', '103']


def test_old_conditions_are_current_checks_not_automatic_reversal_or_persistent_events():
    req, ctx = sample(); prior(ctx); original = deepcopy(ctx)
    result = build_decision_context(req, ctx)
    check = result['previous']['checks'][0]
    assert check['state'] == 'met_now' and check['observed_at'] == req['market_cutoff']
    assert result['previous']['horizons'][0]['direction'] == 'short'
    assert 'failure_count' not in result and 'side' not in result
    assert ctx == original


def test_future_or_missing_previous_availability_cannot_enter_the_summary():
    req, ctx = sample(); prior(ctx)
    for available in (None, '2026-10-20T14:00:01Z'):
        ctx['previous_analysis']['available_at'] = available
        row = build_decision_context(req, ctx)['previous']
        assert row['state'] == 'unavailable' and row['checks'] == [] and row['version_id'] is None


def test_unknown_rule_data_does_not_become_old_thesis_held_or_broken():
    req, ctx = sample(); prior(ctx);ctx['market']['series'][0]['bars'].pop()
    assert build_decision_context(req, ctx)['previous']['checks'][0]['state'] == 'unknown'


def test_legacy_plan_without_trading_style_or_supported_rule_stays_unknown():
    req, ctx = sample(); prior(ctx)
    ctx['previous_analysis']['plans'] = [{'plan_key': 'legacy-plan'}]
    row = build_decision_context(req, ctx)['previous']['checks'][0]
    assert row['state'] == 'unknown' and row['trading_style'] is None


def test_current_predicate_not_met_does_not_recover_or_rewrite_an_old_plan():
    req, ctx = sample(); prior(ctx)
    ctx['previous_analysis']['plans'][0]['invalidation']['price'] = '110'
    original = deepcopy(ctx)
    assert build_decision_context(req, ctx)['previous']['checks'][0]['state'] == 'not_met_now'
    assert ctx == original


def test_nested_projection_preserves_values_and_drops_extensions():
    req, ctx = sample(); prior(ctx); value = build_decision_context(req, ctx)
    assert project_decision_context(value) == value
    original = deepcopy(value)
    value['credential'] = 'SYNTHETIC-secret-extension'
    value['observations'][0]['extra'] = {'credential': 'SYNTHETIC-secret-extension'}
    value['previous']['checks'][0]['condition']['credential'] = 'SYNTHETIC-secret-extension'
    assert project_decision_context(value) == original


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def test_new_manual_and_api_packets_share_the_exact_summary_and_keep_output_schema(tmp_path):
    f = Flow(tmp_path)
    try:
        f.configure(); request = f.request(f.refresh()); original = deepcopy(request)
        manual = facts(request['packet_text']); sent = facts(outbound_packet(request['packet_text']))
        assert request['prompt_version'] == 'manual-analysis/16' and request['answer_schema_version'] == '1.7.0'
        assert manual['decision_context'] == sent['decision_context']
        assert '不能仅以180分钟偏空背书' in request['packet_text']
        assert '不因上涨就永久做多' in request['packet_text']
        assert request == original
        answer = f.answer(request); job, raw = f.submit(request, answer)
        assert job['state'] == 'succeeded', job
        assert f.get('/v1/forecasts/'+request['forecast_id'])['current_version']['raw_text'] == raw
        with f.w.db.read() as scope:
            assert not f.w.db.documents('analysis').list(scope, 'model_call', limit=10)
    finally:
        f.close()


def test_mock_provider_receives_new_projection_once_without_real_calls(tmp_path):
    f = ModelFlow(tmp_path)
    try:
        request = f.ready(); before = len(f.sent); start = f.start(request)
        assert f.w.model_api.process_once(f.w)
        record = f.record(start)
        assert record['state'] == 'succeeded', record
        with f.w.db.read() as scope:
            stored = f.w.db.documents('analysis').get(scope, 'model_call', record['call_id'])
        assert stored['configuration']['outbound_transform_version'] == 'api-projection/9'
        assert facts(json.loads(f.sent[-1].content)['input'])['decision_context'] == facts(request['packet_text'])['decision_context']
        assert not f.w.model_api.process_once(f.w)
        assert len(f.sent) == before + 1
    finally:
        f.close()


def test_legacy_experience_experiment_does_not_silently_accept_or_downgrade_latest_prompt(tmp_path):
    from xau_next.shared.errors import DomainError
    from xau_next.workflows.experience_experiment import make_plan, create_plan, freeze_pair
    f = ModelFlow(tmp_path / 'db')
    try:
        request = f.ready()
        with f.w.db.read() as scope:
            source = f.w.db.documents('analysis').get(scope, 'request', request['request_id'])
        plan = make_plan(f.w.model_api.settings.snapshot()['values'], pairs=1,
                         horizons=request['horizon_minutes'], cohort='synthetic', call_budget=2,
                         accept_unknown_cost=True, source_root=f.w.db.data_root, clock=f.clock)
        root = tmp_path / 'legacy-experiment'; create_plan(root, plan)
        before = deepcopy(source); count = len(f.sent)
        with pytest.raises(DomainError) as error:
            freeze_pair(root, 'pair-0001', source, clock=f.clock)
        assert error.value.code == 'EXPERIMENT_REQUEST_VERSION'
        assert source == before and len(f.sent) == count
    finally:
        f.close()
