"""SYNTHETIC holding-exit regressions. Mock provider only; no MT5 or paid calls."""
from copy import deepcopy
import json

import pytest

from tests.guidance.test_workflow import Flow
from tests.model_api.test_workflow import ModelFlow
from xau_next.domains.analysis.public import outbound_packet


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def short_answer(flow, request):
    answer = flow.answer(request)
    plan = answer['plans'][0]
    plan.update(side='short', stop_loss='101', take_profit='97', entry_checks=[],
        invalidation={'kind': 'quote_compare', 'quote_side': 'ask', 'operator': 'gte', 'price': '101'})
    return answer


def test_new_prompt_reaches_manual_packet_and_real_outbound_projection_without_losing_facts(tmp_path):
    f = Flow(tmp_path)
    try:
        f.configure()
        request = f.request(f.refresh())
        original = deepcopy(request)
        manual = request['packet_text']
        outgoing = outbound_packet(manual)
        assert request['prompt_version'] == 'manual-analysis/14'
        for packet in (manual, outgoing):
            assert '多头price<entry.zone.low；空头price>entry.zone.high' in packet
            assert '等于边界也不接受；闭柱条件同样适用' in packet
            assert '先盈利到某价再启用回撤退出' in packet
            assert 'condition=null并解释' in packet
            assert '不能删掉必要保护来凑候选' in packet
        before, after = facts(manual), facts(outgoing)
        assert after['request']['prompt_version'] == request['prompt_version']
        for key in ('prediction_policy', 'condition_policy', 'forecast_evidence', 'snapshot_alignment'):
            assert after[key] == before[key]
        assert after['bar_encoding'].startswith('ohlc-rows/1')
        assert after['experience_encoding'].startswith('cases/metrics')
        assert request == original
        with f.w.db.read() as scope:
            assert not f.w.db.documents('analysis').list(scope, 'model_call', limit=10)
    finally:
        f.close()


@pytest.mark.parametrize('side,threshold,allowed', [
    ('long', '99.89', True), ('long', '99.90', False), ('long', '100.00', False),
    ('short', '100.11', True), ('short', '100.10', False), ('short', '99.89', False),
])
@pytest.mark.parametrize('kind', ['bar_close', 'quote_compare'])
def test_boundaries_stay_strict_for_both_sides_and_observations(tmp_path, side, threshold, allowed, kind):
    f = Flow(tmp_path)
    try:
        f.configure()
        request = f.request(f.refresh())
        answer = f.answer(request) if side == 'long' else short_answer(f, request)
        condition = {'kind': kind, 'operator': 'lte' if side == 'long' else 'gte', 'price': threshold}
        condition.update({'timeframe': 'M5'} if kind == 'bar_close' else
                         {'quote_side': 'bid' if side == 'long' else 'ask'})
        answer['plans'][0]['holding_exit'] = {'condition': condition, 'reason': 'SYNTHETIC independent structure'}
        original = deepcopy(answer)
        job, raw = f.submit(request, answer)
        assert answer == original
        if allowed:
            assert job['state'] == 'succeeded', job
            saved = f.get('/v1/forecasts/' + request['forecast_id'])['current_version']
            assert saved['raw_text'] == raw
        else:
            assert job['state'] == 'failed' and job['error']['code'] == 'HOLDING_RULE_INVALID', job
            message = job['error']['detail']
            assert threshold in message and '99.90—100.10' in message
            assert '原回答未修改' in message and 'condition填null' in message
            assert job['error']['field_errors'][0]['path'] == '/plans/0/holding_exit'
    finally:
        f.close()


def test_reported_gold_example_stays_rejected_and_raw_answer_is_preserved_without_retry(tmp_path):
    f = ModelFlow(tmp_path)
    try:
        request = f.ready()
        answer = short_answer(f, request)
        plan = answer['plans'][0]
        plan.update(entry={'mode': 'limit_retest', 'price': '4178.95',
                           'zone': {'low': '4178.95', 'high': '4179.05'}},
                    activation={'kind': 'bar_close', 'timeframe': 'M1', 'operator': 'lt', 'price': '4176.55'},
                    invalidation={'kind': 'quote_compare', 'quote_side': 'ask', 'operator': 'gte', 'price': '4180.20'},
                    stop_loss='4180.20', take_profit='4175.10',
                    holding_exit={'condition': {'kind': 'bar_close', 'timeframe': 'M5',
                                                'operator': 'gt', 'price': '4178.90'},
                                  'reason': 'SYNTHETIC reproduces the reported unsupported threshold'})
        f.answer_text = json.dumps(answer, ensure_ascii=False)
        started = f.start(request)
        assert f.w.model_api.process_once(f.w)
        record = f.record(started)
        assert record['state'] == 'failed' and record['transport_succeeded'] and record['answer_valid'] is False
        problem = f.get('/v1/jobs/' + record['job_id'])['error']
        assert record['error_code'] == problem['code'] == 'HOLDING_RULE_INVALID'
        assert all(value in problem['detail'] for value in ('4178.95—4179.05', '4178.90', '严格高于4179.05'))
        assert record['answer'] and record['raw_response']
        assert f.w.model_api.calls.read(record['answer']).decode('utf-8') == f.answer_text
        with f.w.db.read() as scope:
            stored = f.w.db.documents('analysis').get(scope, 'model_call', record['call_id'])
            assert stored['configuration']['outbound_transform_version'] == 'api-projection/7'
        sent = len(f.sent)
        assert not f.w.model_api.process_once(f.w)
        assert len(f.sent) == sent  # No automatic retry after business validation fails.
    finally:
        f.close()


def test_null_exit_with_reason_imports_and_keeps_original_stop_target_and_deadline(tmp_path):
    f = Flow(tmp_path)
    try:
        f.configure()
        request = f.request(f.refresh())
        answer = f.answer(request)
        answer['plans'][0]['holding_exit'] = {
            'condition': None, 'reason': 'SYNTHETIC no additional structure; original stop, target and deadline remain.'}
        original = deepcopy(answer['plans'][0])
        job, raw = f.submit(request, answer)
        assert job['state'] == 'succeeded', job
        saved = f.get('/v1/forecasts/' + request['forecast_id'])['current_version']
        assert saved['raw_text'] == raw
        for key in ('stop_loss', 'take_profit', 'exit_deadline', 'max_holding_minutes', 'holding_exit'):
            assert saved['answer']['plans'][0][key] == original[key]
    finally:
        f.close()
