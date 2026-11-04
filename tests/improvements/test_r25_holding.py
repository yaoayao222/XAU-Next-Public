"""SYNTHETIC acceptance of holding management. No model or broker calls."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from tests.guidance.test_advice import case, NOW
from tests.guidance.test_workflow import Flow, associate
from tests.portfolio.test_portfolio import envelope, deal
from xau_next.domains.plans.advice import position_advice
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.values import parse_utc
from xau_next.shared.errors import DomainError


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def direct():
    p, m, _, s, *_ = case()
    o = {'order': {'account_scope_id': m['account_scope_id'], 'kind': 'position', 'broker_id': 'SYNTHETIC'},
         'plan_id': p['plan_id'], 'side': 'long', 'stop_loss': '98.80', 'take_profit': '104',
         'entry_price': '100', 'volume_lots': '0.01', 'symbol': 'XAUUSD'}
    return o, p, m, s


def rule(price='99.50'):
    return {'kind': 'quote_compare', 'quote_side': 'bid', 'operator': 'lte', 'price': price}


def test_structure_exit_before_stop_is_latched_and_does_not_wait_for_new_model():
    o, p, m, s = direct()
    p['definition']['holding_exit'] = {'condition': rule(), 'reason': 'SYNTHETIC恢复结构破坏'}
    opened = '2026-10-12T02:01:00Z'
    m['quote'].update(bid='99.40', ask='99.42')
    a = position_advice(o, p, m, s, now=NOW, opened_at=opened)
    assert a['state'] == 'EXIT' and a['reason_code'] == 'THESIS_INVALIDATED'
    assert a['management']['original_stop'] == p['definition']['stop_loss']
    assert a['_holding_events'][0]['observed_at'] == NOW
    m['quote'].update(bid='100', ask='100.02')
    b = position_advice(o, p, m, s, now=NOW, opened_at=opened, observed_events=a['_holding_events'])
    assert b['state'] == 'EXIT' and not b['_holding_events']
    m['quote_status']['state'] = 'unavailable'
    assert position_advice(o, p, m, s, now=NOW, opened_at=opened, observed_events=a['_holding_events'])['state'] == 'EXIT'


def test_old_reassessment_condition_is_only_an_alert_and_not_a_new_exit():
    o, p, m, s = direct()
    original = deepcopy(p)
    m['quote'].update(bid='99.40', ask='99.42')
    a = position_advice(o, p, m, s, now=NOW, opened_at='2026-10-12T02:01:00Z', reassessment_rules=[rule()])
    assert a['state'] == 'HOLD'
    assert a['management']['reassessment_state'] == 'required'
    assert p == original and 'holding_exit' not in p['definition']


def test_pre_entry_observation_does_not_trigger_new_holding_structure_exit():
    o, p, m, s = direct()
    p['definition']['holding_exit'] = {'condition': rule(), 'reason': 'SYNTHETIC'}
    m['quote'].update(bid='99.40', ask='99.42')
    a = position_advice(o, p, m, s, now=NOW, opened_at=NOW)
    assert a['state'] == 'HOLD' and not a['_holding_events']


def test_missing_structure_data_is_unknown_but_time_exit_is_independent_of_quote():
    o, p, m, s = direct()
    p['definition']['holding_exit'] = {'condition': {'kind': 'bar_close', 'timeframe': 'M5', 'operator': 'lte', 'price': '99.50'}, 'reason': 'SYNTHETIC'}
    m['series'] = []
    a = position_advice(o, p, m, s, now=NOW, opened_at='2026-10-12T02:01:00Z')
    assert a['state'] == 'UNKNOWN' and a['management']['structure_status'] == 'unknown'
    p['definition']['max_holding_minutes'] = 5
    m['quote'] = None
    assert position_advice(o, p, m, s, now=NOW, opened_at='2026-10-12T02:01:00Z')['reason_code'] == 'HOLDING_LIMIT'


def test_source_switch_without_current_market_is_an_explicit_blocker(tmp_path):
    f = Flow(tmp_path)
    try:
        with f.w.db.read() as scope, pytest.raises(DomainError) as error:
            f.w._holding_context({'account_scope_id': 'synthetic-account', 'kind': 'position', 'broker_id': 'r25'}, None, {}, scope=scope)
        assert error.value.code == 'HOLDING_FACTS_UNAVAILABLE'
    finally:
        f.close()


class HoldingFlow:
    def __init__(self, tmp_path, monkeypatch, mutate=None):
        self.f = Flow(tmp_path)
        self.old_request, self.original = self.f.initial('scalp', mutate=mutate)
        self.plan_id = self.f.get('/v1/workspace')['plans'][0]['plan_id']
        self.order = {'account_scope_id': 'synthetic-account', 'kind': 'position', 'broker_id': 'r25-synthetic'}
        self.volume = '0.02'
        self.bid = '100.00'
        original = self.f.w.market._bridge.request
        def source(operation, params):
            response = original(operation, params)
            if operation == 'snapshot':
                result = response['result']
                result['market']['quote'].update(bid=self.bid, ask=str(float(self.bid) + .02))
                result['portfolio']['orders'] = [{'order': self.order, 'symbol': 'XAUUSD', 'side': 'long',
                    'volume_lots': self.volume, 'entry_price': '100', 'stop_loss': '99', 'take_profit': '102.90',
                    'event_time': self.f.clock(), 'captured_at': self.f.clock(), 'plan_id': None}]
                trade = envelope([deal('r25-in', 'in', '0.02', '100', '0', '0', event_time='2026-10-12T02:00:02Z')],
                                 id='r25-trade', account='synthetic-account', broker_id='r25-synthetic')
                trade['opened_at'] = '2026-10-12T02:00:02Z'
                result['trades'] = [trade]
            return response
        monkeypatch.setattr(self.f.w.market._bridge, 'request', source)
        self.f.clock.value = '2026-10-12T02:00:03Z'
        self.market = self.f.refresh()
        associate(self.f, self.order, self.plan_id)

    def create(self):
        return self.f.post('/v1/analysis/requests', {'kind': 'new', 'forecast_id': None, 'base_version_id': None,
            'market_snapshot_id': self.market['snapshot_id'], 'holding_review_order': self.order})

    def answer(self, request):
        a = self.f.answer(request)
        a['plans'] = []
        for c in a['candidate_assessments']:
            c.update(outcome='no_candidate', plan_key=None, rationale='SYNTHETIC持仓专项，无新入场候选', missing_conditions=['只重评已有持仓'])
        h = deepcopy(a['horizons'][0])
        h.update(horizon_minutes=request['horizon_minutes'][0], end_at=request['window_end'], direction='uncertain', confidence=None,
            phases=[{'start_at': request['market_cutoff'], 'end_at': request['window_end'], 'direction': 'uncertain', 'note': 'SYNTHETIC'}], turning_points=[])
        a['horizons'] = [h]
        a['order_advice'] = [{'order': self.order, 'action': 'hold', 'reason': 'SYNTHETIC当前依据仍在，保留原退出规则。',
            'proposed_stop_loss': None, 'proposed_take_profit': None, 'proposed_volume_lots': None,
            'valid_until': '2026-10-12T02:05:00Z', 'target_assessment': {'target_price': '103', 'state': 'supported',
                'supporting_evidence': ['SYNTHETIC结构保持'], 'opposing_evidence': ['SYNTHETIC仍可能回落'], 'obstacles': ['SYNTHETIC途中阻力'],
                'time_basis': 'SYNTHETIC原剩余持仓期限内重新判断，不保证到达。', 'change_since_previous': '首次持仓重评', 'limitations': ['仅合成数据，不证明效果']}}]
        return a


def test_manual_holding_request_projection_import_and_originals_are_separate(tmp_path, monkeypatch):
    h = HoldingFlow(tmp_path, monkeypatch); f = h.f
    try:
        request = h.create()
        assert h.create()['request_id'] == request['request_id']
        assert request['holding_review_order'] == h.order
        ctx = facts(request['packet_text'])['holding_review']
        assert ctx['original_plan']['take_profit'] == '103'
        assert ctx['deadline'] == '2026-10-12T02:20:02Z'
        assert parse_utc(request['window_end']) <= parse_utc(ctx['deadline'])
        sent = facts(outbound_packet(request['packet_text']))['holding_review']
        assert sent['original_plan'] == {k: v for k, v in ctx['original_plan'].items() if k in sent['original_plan']}
        assert 'fingerprint' not in sent and 'holding_key' not in sent
        old_forecast = f.get('/v1/workspace')['current_forecast']
        job, raw = f.submit(request, h.answer(request))
        assert job['state'] == 'succeeded', job
        ws = f.get('/v1/workspace')
        assert ws['current_forecast'] == old_forecast
        assert ws['plans'][0]['plan_id'] == h.plan_id and ws['action_advice'][0]['state'] == 'USED'
        m = ws['position_advice'][0]['management']
        assert m['opened_at'] == '2026-10-12T02:00:02Z' and m['deadline'] == ctx['deadline']
        assert m['original_target'] == '103' and m['broker_target'] == '102.90'
        assert m['latest_review']['current'] is True
        saved = f.get('/v1/analysis/requests/' + request['request_id'])
        assert saved['holding_result']['raw_text'] == raw
        assert f.get('/v1/forecasts/' + h.old_request['forecast_id'])['current_version']['raw_text'] == h.original
        # A real size change invalidates the old opinion; polling alone does not.
        h.volume = '0.01'; f.clock.value = '2026-10-12T02:00:04Z'; h.market = f.refresh()
        assert f.get('/v1/workspace')['position_advice'][0]['management']['latest_review']['current'] is False
        with f.w.db.read() as scope:
            assert not f.w.db.documents('analysis').list(scope, 'model_call', limit=10)
    finally:
        f.close()


def test_confirmed_structure_and_notification_survive_rebound_and_restart(tmp_path, monkeypatch):
    def policy(a):
        a['plans'][0]['holding_exit'] = {'condition': rule(), 'reason': 'SYNTHETIC恢复结构失效'}
    h = HoldingFlow(tmp_path, monkeypatch, mutate=policy); f = h.f
    try:
        h.bid = '99.40'; f.clock.value = '2026-10-12T02:00:04Z'; h.market = f.refresh()
        assert f.get('/v1/workspace')['position_advice'][0]['state'] == 'EXIT'
        f.w.guidance.tick(); f.w.guidance.tick()
        notices = [n for n in f.get('/v1/workspace')['notifications'] if n['kind'] == 'position']
        assert len(notices) == 1
        h.bid = '100'; f.clock.value = '2026-10-12T02:00:05Z'; h.market = f.refresh()
        assert f.get('/v1/workspace')['position_advice'][0]['state'] == 'EXIT'
        f.close()
        restarted = Flow(tmp_path, f.clock, f.sink)
        try:
            assert restarted.get('/v1/workspace')['position_advice'][0]['state'] == 'EXIT'
            with restarted.w.db.read() as scope:
                events = restarted.w.portfolio.holding_events(restarted.w.portfolio.get_portfolio(scope=scope).to_dict()['orders'][0],
                    '2026-10-12T02:00:02Z', scope=scope)
            assert len(events) == 1 and events[0]['observed_at'] == '2026-10-12T02:00:04Z'
        finally:
            restarted.close()
    finally:
        f.close()


def test_changed_holding_cannot_start_a_model_call_from_old_packet(tmp_path, monkeypatch):
    h = HoldingFlow(tmp_path, monkeypatch); f = h.f
    try:
        request = h.create()
        h.volume = '0.01'; f.clock.value = '2026-10-12T02:00:04Z'; h.market = f.refresh()
        error = f.post('/v1/analysis/requests/' + request['request_id'] + '/model-call',
                       {'retry_of': None, 'acknowledge_possible_charge': True}, 409)
        assert error['code'] == 'HOLDING_FACTS_CHANGED'
        with f.w.db.read() as scope:
            assert not f.w.db.documents('analysis').list(scope, 'model_call', limit=10)
    finally:
        f.close()


@pytest.mark.parametrize('defect,code', [('target', 'HOLDING_TARGET_MISMATCH'), ('expiry', 'HOLDING_VALIDITY_REQUIRED'),
    ('evidence', 'HOLDING_EVIDENCE_REQUIRED'), ('unknown', 'HOLDING_UNKNOWN_REQUIRED'), ('plans', 'HOLDING_ENTRY_FORBIDDEN')])
def test_bad_reassessment_answers_preserve_original_and_fail(tmp_path, monkeypatch, defect, code):
    h = HoldingFlow(tmp_path, monkeypatch); f = h.f
    try:
        request = h.create(); a = h.answer(request)
        if defect == 'target': a['order_advice'][0]['target_assessment']['target_price'] = '104'
        if defect == 'expiry': a['order_advice'][0]['valid_until'] = None
        if defect == 'evidence': a['order_advice'][0]['target_assessment']['supporting_evidence'] = []
        if defect == 'unknown': a['order_advice'][0]['target_assessment']['state'] = 'unknown'
        if defect == 'plans': a['plans'] = f.answer(request)['plans']
        result, raw = f.submit(request, a)
        assert result['state'] == 'failed', result
        assert result['error']['code'] == code, result
        assert f.get('/v1/forecasts/' + h.old_request['forecast_id'])['current_version']['raw_text'] == h.original
    finally:
        f.close()
