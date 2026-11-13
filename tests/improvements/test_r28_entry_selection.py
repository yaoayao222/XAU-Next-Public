"""Frozen inputs and SYNTHETIC mocked answers, not a model-quality experiment."""
from copy import deepcopy
import json

import pytest

from tests.guidance.test_workflow import Flow
from tests.model_api.test_workflow import ModelFlow
from tests.improvements.prompt_cases import selected_prompt
from tests.improvements.test_r27_decision_context import sample
from xau_next.domains.analysis.entry_selection import build_entry_selection_context, project_entry_selection_context
from xau_next.domains.analysis.package import build_packet, frozen_answer_schema
from xau_next.domains.analysis.public import outbound_packet


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def timing_sample():
    request, context = sample()
    request.update(market_cutoff='2026-10-20T12:54:57.374Z', created_at='2026-10-20T12:55:08.529Z',
                   window_end='2026-10-21T00:54:57.374Z', answer_schema_version='1.7.0')
    # Known frozen latency from the reported failing original, with synthetic identity.
    context['timing_reference'] = {'policy_version': 'entry-observation/2', 'planning_delay_seconds': 306}
    context['market']['quote'].update(event_time=request['market_cutoff'])
    context['market']['quote_status'].update(captured_at=request['market_cutoff'])
    return request, context


def test_baseline_table_exposes_the_reported_m5_one_minute_shortfall_without_changing_any_plan():
    request, context = timing_sample(); original = deepcopy((request, context))
    summary = build_entry_selection_context(request, context)
    assert summary['estimated_available_at'] == '2026-10-20T13:00:14.529000Z'
    rows = {r['entry_mode']: r for r in summary['minimum_timing'] if r['timeframe'] == 'M5'}
    assert rows['limit_retest']['minimum_entry_deadline'] == '2026-10-20T13:15:00Z'
    assert rows['market']['minimum_entry_deadline'] == '2026-10-20T13:10:10Z'
    assert rows['stop_breakout']['minimum_entry_deadline'] == '2026-10-20T13:11:00Z'
    assert rows['limit_retest']['required_activation_closes'] == 2
    assert summary['style_windows'][0]['analysis_fresh_until'] == '2026-10-20T13:24:57.374000Z'
    assert (request, context) == original
    assert not any(k in summary for k in ('selected_mode', 'direction', 'recommended_price', 'ready', 'probability'))


@pytest.mark.parametrize('defect', ['stale', 'future_quote', 'future_capture', 'missing_capture',
                                  'reversed', 'invalid_price', 'unavailable'])
def test_comparison_cannot_use_invalid_or_future_bid_ask(defect):
    request, context = timing_sample(); q = context['market']['quote']; s = context['market']['quote_status']
    if defect == 'stale': q['event_time'] = '2026-10-20T12:54:46Z'
    if defect == 'future_quote': q['event_time'] = '2026-10-20T12:54:58Z'
    if defect == 'future_capture': s['captured_at'] = '2026-10-20T12:54:58Z'
    if defect == 'missing_capture': s.pop('captured_at')
    if defect == 'reversed': q['ask'] = '1'
    if defect == 'invalid_price': q['bid'] = 'NaN'
    if defect == 'unavailable': s['state'] = 'unavailable'
    quote = build_entry_selection_context(request, context)['quote']
    assert quote['state'] == 'unknown' and quote['bid'] is None and quote['ask'] is None


def test_nested_projection_drops_unapproved_extensions_at_every_level():
    request, context = timing_sample(); value = build_entry_selection_context(request, context)
    expected = deepcopy(value)
    for item in [value, value['quote'], *value['style_windows'], *value['minimum_timing']]:
        item['unexpected_extension'] = 'SYNTHETIC-should-not-be-sent'
    assert project_entry_selection_context(value) == expected


def test_later_starts_and_checks_are_not_promised_by_the_empty_check_baseline():
    from xau_next.shared.entry_timing import planned_timing
    request, context = timing_sample(); base = build_entry_selection_context(request, context)
    earliest = next(r['minimum_entry_deadline'] for r in base['minimum_timing']
                    if r['timeframe'] == 'M5' and r['entry_mode'] == 'limit_retest')
    plan = {'trading_style': 'scalp', 'valid_from': '2026-10-20T13:11:00Z',
            'entry_deadline': '2026-10-20T13:24:00Z', 'activation': {'kind': 'bar_close', 'timeframe': 'M5'},
            'entry': {'mode': 'limit_retest'}, 'entry_checks': []}
    timing = planned_timing(plan, request, context)
    assert earliest == '2026-10-20T13:15:00Z'
    assert timing['minimum_entry_deadline'] == '2026-10-20T13:25:00Z'
    assert timing['state'] == 'INSUFFICIENT'


def test_new_input_fact_diff_is_only_entry_context_and_prompt_identity(tmp_path):
    f = Flow(tmp_path)
    try:
        f.configure(); request = f.request(f.refresh())
        with f.w.db.read() as scope:
            stored = f.w.db.documents('analysis').get(scope, 'request', request['request_id'])
        r28_request = deepcopy(facts(request['packet_text'])['request'])
        r28_request['prompt_version'] = 'manual-analysis/15'
        r28_packet = build_packet(r28_request, stored['context'], frozen_answer_schema(stored))
        new = facts(r28_packet); sent = facts(outbound_packet(r28_packet))
        assert new['request']['prompt_version'] == 'manual-analysis/15'
        assert new['entry_selection_context'] == sent['entry_selection_context']
        old_request = deepcopy(new['request'])
        old_request['prompt_version'] = 'manual-analysis/14'
        old = facts(build_packet(old_request, stored['context'], frozen_answer_schema(stored)))
        clean = deepcopy(new); clean.pop('entry_selection_context'); clean['request']['prompt_version'] = 'manual-analysis/14'
        assert clean == old
        assert '入场比较' in r28_packet
        assert '回踩不是默认答案' in r28_packet
    finally:
        f.close()


@pytest.mark.parametrize('scenario,mode', [('延续方式可表达','stop_breakout'), ('区间内方式可表达','market'),
                                         ('回踩方式仍可表达','limit_retest'), ('三种均无依据',None)])
def test_supported_choices_and_no_candidate_import_without_mode_rewriting(tmp_path, scenario, mode):
    f = Flow(tmp_path)
    try:
        f.configure(); request = f.request(f.refresh()); answer = f.answer(request)
        if mode is None:
            answer['plans'] = []
            answer['summary'] = 'SYNTHETIC: all modes lack evidence; no candidate, not a real model decision.'
            for row in answer['candidate_assessments']:
                row.update(outcome='no_candidate', plan_key=None, missing_conditions=['SYNTHETIC: no independent structure'],
                           rationale='SYNTHETIC: market/stop_breakout/limit_retest all unsupported.')
        else:
            answer['plans'][0]['entry']['mode'] = mode
            if mode == 'market': answer['plans'][0]['entry']['price'] = None
            answer['plans'][0]['rationale'] = 'SYNTHETIC mode fixture; not evidence that a real model selects this mode.'
        job, raw = f.submit(request, answer)
        assert job['state'] == 'succeeded', (scenario,job)
        version = f.get('/v1/forecasts/'+request['forecast_id'])['current_version']
        assert version['raw_text'] == raw
        if mode:
            assert version['answer']['plans'][0]['entry']['mode'] == mode
        else:
            assert version['answer']['plans'] == []
    finally:
        f.close()


@pytest.mark.parametrize('prompt,projection', [
    ('manual-analysis/15', 'api-projection/8'),
    ('manual-analysis/16', 'api-projection/9'),
])
def test_latest_projection_reaches_mock_transport_once_and_keeps_original_gates(tmp_path, prompt, projection):
    f = ModelFlow(tmp_path)
    try:
        with selected_prompt(f, prompt):
            request = f.ready()
        original = deepcopy(request)
        before = len(f.sent); started = f.start(request)
        assert f.w.model_api.process_once(f.w)
        record = f.record(started); assert record['state'] == 'succeeded',record
        assert f.get('/v1/analysis/requests/' + request['request_id'])['packet_text'] == original['packet_text']
        sent = facts(json.loads(f.sent[-1].content)['input'])
        assert sent['entry_selection_context'] == facts(request['packet_text'])['entry_selection_context']
        with f.w.db.read() as scope:
            stored = f.w.db.documents('analysis').get(scope,'model_call',record['call_id'])
        assert stored['configuration']['outbound_transform_version'] == projection
        assert len(f.sent) == before+1 and not f.w.model_api.process_once(f.w)
        assert len(f.sent) == before+1
    finally:
        f.close()


@pytest.mark.parametrize('mode', ['market','stop_breakout'])
def test_independently_defined_continuation_candidate_has_a_bounded_entry_and_never_rewrites_retest(mode):
    from tests.guidance.test_advice import case, decide, NOW
    from xau_next.domains.plans.calculation import calculate
    old = case(); old[0]['definition']['take_profit'] = '105'
    old[1]['quote'].update(bid='100.18',ask='100.20')
    snapshot = deepcopy(old[0])
    assert decide(old)['state'] == 'WAITING'
    new = list(deepcopy(old))
    new[0]['definition']['entry'] = {'mode':mode,'price':None if mode=='market' else '100.10',
                                    'zone':{'low':'100.10','high':'100.30'}}
    command = {'entry_price_override':'100.20','volume_lots_override':None,'sizing_mode':'fixed_volume'}
    new[4] = {**new[4],**calculate(*new[:4],command,evaluated_at=NOW,margin_fact={'amount':'1','currency':'USD'})}
    assert decide(new)['state'] == 'READY'
    assert old[0] == snapshot and old[0]['definition']['entry']['mode']=='limit_retest'
    new[1]['quote'].update(bid='100.38',ask='100.40')
    assert decide(new)['state'] == 'MISSED'
