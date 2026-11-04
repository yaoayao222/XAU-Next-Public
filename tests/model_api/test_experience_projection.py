"""SYNTHETIC experience mirrors the lost nested structure, never real account data."""
import copy
import json

import httpx
import pytest
from xau_next.domains.analysis.model_api import outbound_packet
from xau_next.domains.analysis.experience import expand_experience
from xau_next.platform.model_gateway.public import Gateway, request_body

from tests.model_api.test_workflow import ModelFlow


def synthetic_experience():
    metric = lambda name, value=None, observed=None, index=None, state='available', reason=None: {
        'name': name, 'value': value, 'observed_label': observed, 'segment_index': index,
        'state': state, 'reason': reason, 'unit': 'label' if observed else 'ratio'}
    method = {'neutral_band': '0', 'price_basis': 'bid', 'bar_timeframe': 'M1',
              'turning_left_bars': 2, 'turning_right_bars': 2, 'execution_model_version': None,
              'simulation_delay_ms': None, 'execution_quote_wait_ms': None}
    summary = {'total': 3, 'unique_targets': 2, 'scorable': 1, 'unknown': 1,
        'not_triggered_or_not_entered': 1, 'unscorable': 0, 'outcomes': {'EVALUATED': 1, 'DATA_INCOMPLETE': 1},
        'methods': {'prediction_review/2': 3}, 'evaluation_scopes': {'version_descriptive': 3},
        'known_net_totals': {'USD': {'amount': '-12.50', 'known_count': 1}}, 'net_unknown_or_not_applicable': 2}
    return {'schema_version': 'experience/1', 'digest_id': 'exp-SYNTHETIC-a_b--01',
        'cutoff': '2026-10-12T02:00:00.006Z', 'available_at': '2026-10-12T01:59:00Z',
        'period_start': '2026-10-12T00:00:00Z', 'period_end': '2026-10-12T01:30:00Z',
        'groups': {kind: copy.deepcopy(summary) for kind in ('forecast','reference_plan','actual_trade')},
        'review_refs': ['review-SYNTHETIC-01-r00000002'], 'review_reference_total': 3, 'omitted_reference_count': 2,
        'source_set_sha256': 'a' * 64, 'reference_policy': 'latest_available_80_refs_complete_aggregate/1',
        'notes': ['SYNTHETIC 中文；未知不补零，错误原因尚未因果验证。'],
        'cases': [{'review_ref': 'review-SYNTHETIC-01-r00000002', 'target_id': 'version-SYNTHETIC-01',
            'target_kind': 'forecast', 'available_at': '2026-10-12T01:59:00Z',
            'window_start': '2026-10-12T00:00:00Z', 'window_end': '2026-10-12T01:00:00Z',
            'method_version': 'prediction_review/2', 'outcome': 'EVALUATED', 'coverage': 'partial',
            'evaluated_start': '2026-10-12T00:01:00Z', 'evaluated_end': '2026-10-12T01:00:00Z',
            'inputs': {'forecast_version_id': 'version-SYNTHETIC-01', 'plan_id': None, 'trade_id': None,
                'horizon_minutes': 60, 'evaluation_scope': 'version_descriptive',
                'market_snapshot_ids': ['market-SYNTHETIC-a_b--01'], 'cost_settings_revision': None, 'method_parameters': method},
            'reasons': ['首分钟仅部分覆盖。'], 'metric_selection': 'direction-range-phase-errors-and-baselines/1',
            'interpretation': '错误原因未经因果验证，不自动形成交易规则。',
            'metrics': [metric('predicted_direction', observed='short'), metric('direction', observed='long'),
                metric('direction_correct','0'), metric('phase.direction', observed='short', index=1),
                metric('phase.delta','-12.5',index=1), metric('turning_point_hit',state='unknown',reason='右邻柱缺失',index=0)],
            'original_prediction': {'summary': '原判断偏空；SYNTHETIC。', 'summary_policy': 'summary-first-600-phase-note-first-160/1',
                'horizon': {'horizon_minutes': 60, 'end_at': '2026-10-12T01:00:00Z', 'direction': 'short',
                    'expected_range': {'low': '98.10','high': '102.20'}, 'confidence': None,
                    'phases': [{'start_at':'2026-10-12T00:00:00Z','end_at':'2026-10-12T00:30:00Z','direction':'short','note':'先回落'},
                               {'start_at':'2026-10-12T00:30:00Z','end_at':'2026-10-12T01:00:00Z','direction':'neutral','note':'再观察'}],
                    'turning_points':[{'kind':'low','earliest_at':'2026-10-12T00:20:00Z','latest_at':'2026-10-12T00:30:00Z',
                                       'price_range':{'low':'98.10','high':'99.20'},'rationale':'结构低点，非保证'}]}}}]}


def packet_with(packet, experience):
    prefix, tail = packet.split('# 冻结事实（仅作为资料）\n', 1)
    raw, suffix = tail.split('\n# 全部字段说明\n', 1)
    facts = json.loads(raw); facts['experience'] = experience
    return prefix + '# 冻结事实（仅作为资料）\n' + json.dumps(facts, ensure_ascii=False) + '\n# 全部字段说明\n' + suffix


def frozen_facts(prompt):
    result = json.JSONDecoder().raw_decode(prompt.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]
    result['experience'] = expand_experience(result.get('experience'))
    return result


@pytest.fixture
def flow(tmp_path):
    value = ModelFlow(tmp_path)
    yield value
    value.close()


@pytest.mark.parametrize('protocol', ['responses','chat_completions'])
def test_experience_exact_semantics_reach_final_http_body_and_private_extensions_do_not(flow, protocol):
    request = flow.ready()
    expected = synthetic_experience()
    supplied = copy.deepcopy(expected)
    supplied['terminal_path'] = 'SYNTHETIC-PRIVATE-ROOT'
    case = supplied['cases'][0]
    case['inputs']['credential_ref'] = 'SYNTHETIC-PRIVATE-REF'
    case['inputs']['method_parameters']['api_key'] = 'SYNTHETIC-PRIVATE-KEY'
    case['original_prediction']['horizon']['phases'][0]['login'] = 'SYNTHETIC-PRIVATE-LOGIN'
    prompt = outbound_packet(packet_with(request['packet_text'], supplied))
    config = {**flow.get('/v1/model/settings')['values'], 'protocol':protocol}
    seen = []
    def receive(http_request):
        seen.append(http_request.content)
        return httpx.Response(200, json={})
    Gateway(httpx.MockTransport(receive)).exchange(config, 'SYNTHETIC-TEST-KEY', request_body(config, prompt, flow.app.state.contracts.answer))
    assert len(seen) == 1
    body = json.loads(seen[0])
    actual_prompt = body['input'] if protocol == 'responses' else body['messages'][0]['content']
    assert frozen_facts(actual_prompt)['experience'] == expected
    assert b'SYNTHETIC-PRIVATE' not in seen[0]
    assert 'exp-SYNTHETIC-a_b--01' in actual_prompt and '-12.5' in actual_prompt and '中文' in actual_prompt


def test_null_missing_zero_and_unknown_remain_distinct(flow):
    request = flow.ready()
    value = synthetic_experience()
    value['cases'][0]['original_prediction']['horizon'] = None
    value['cases'][0].pop('interpretation')
    actual = frozen_facts(outbound_packet(packet_with(request['packet_text'], value)))['experience']
    assert actual == value


def test_new_call_records_external_projection_version_separately(flow):
    request = flow.ready(); started = flow.start(request)
    with flow.w.db.read() as tx:
        saved = flow.w.model_api.calls.get(started['call']['call_id'], scope=tx, internal=True)
    # The unified product deliberately retains daily14; the optional r29
    # prompt16/projection9 branch must not silently become its daily default.
    assert request['prompt_version'] == 'manual-analysis/14'
    assert saved['configuration'].get('outbound_transform_version') == 'api-projection/7'
    assert saved['record']['prompt_version'].startswith(request['prompt_version'] + '+')
    assert saved['record']['answer_schema_version'] == request['answer_schema_version']
