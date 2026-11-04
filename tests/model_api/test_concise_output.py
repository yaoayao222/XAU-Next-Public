"""SYNTHETIC: compact expression must not reduce facts, output contract or import gates."""
import copy
import json

import pytest
from xau_next.domains.analysis import model_api
from xau_next.platform.model_gateway.public import request_body

from tests.model_api.test_workflow import ModelFlow


@pytest.fixture
def flow(tmp_path):
    value = ModelFlow(tmp_path)
    yield value
    value.close()


def test_only_version_marker_and_prose_guidance_change_wire_input(flow, monkeypatch):
    request = flow.ready()
    saved = flow.get('/v1/analysis/requests/' + request['request_id'])
    with flow.w.db.read() as tx:
        prompt, schema, _ = flow.w.analysis.model_packet(request['request_id'], scope=tx)
        config = flow.w.model_api.settings.snapshot(scope=tx)['values']
    body = json.loads(request_body(config, prompt, schema))
    guidance = model_api.concise_output_guidance()
    assert prompt.endswith(guidance)
    with monkeypatch.context() as old:
        old.setattr(model_api, 'API_PROMPT_VERSION', 'api-output/1')
        old.setattr(model_api, 'concise_output_guidance', lambda: '')
        with flow.w.db.read() as tx:
            baseline, old_schema, _ = flow.w.analysis.model_packet(request['request_id'], scope=tx)
    old_body = json.loads(request_body(config, baseline, old_schema))
    assert schema == old_schema
    assert body.pop('input').removesuffix(guidance).replace('（api-output/2）', '（api-output/1）') == old_body.pop('input')
    assert body == old_body  # Model, effort, schema, stream, limits and all protocol fields identical.
    assert flow.get('/v1/analysis/requests/' + request['request_id']) == saved
    assert len(flow.sent) == 1  # Only the fixture's injected SYNTHETIC probe, no generation sent here.


def test_guidance_preserves_analysis_requirements_and_allows_needed_detail():
    text = model_api.concise_output_guidance()
    for phrase in ('完整保留', '关键支持与反对证据', '风险和不确定性', '方向反证',
                   '周期仍分别', '不能因简洁而改为uncertain', '不合并丢失',
                   '无候选仍逐项说明真实缺口', '样本分母', '模式或周期特有风险',
                   '不是新的拒绝门槛', '更多文字时完整写出', '完整JSON对象'):
        assert phrase in text


@pytest.mark.parametrize('long_explanation', [False, True])
def test_valid_answer_is_saved_verbatim_with_identical_business_values(flow, long_explanation):
    request = flow.ready()
    answer = json.loads(flow.answer_text)
    if long_explanation:
        # Well above the soft prose suggestion, while within the original schema's real limit.
        answer['plans'][0]['rationale'] = 'SYNTHETIC必要的独立风险与结构依据。' * 60
    flow.answer_text = json.dumps(answer, ensure_ascii=False, separators=(',', ':'))
    before = copy.deepcopy(answer)
    started = flow.start(request)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record['state'] == 'succeeded' and record['answer_valid'] is True
    assert record['prompt_version'].endswith('+api-output/2')
    version = flow.get('/v1/forecasts/' + request['forecast_id'])['current_version']
    assert version['answer'] == before
    assert version['raw_text'] == flow.answer_text  # No shortening or response rewrite after receipt.
    assert len(flow.sent) == 2 and not flow.w.model_api.process_once(flow.w)


@pytest.mark.parametrize('fault,expected', [('missing_rationale','SCHEMA_INVALID'),
                                          ('wrong_price','PRICE_GRID_INVALID'),
                                          ('missing_mode','SCHEMA_INVALID')])
def test_concise_answers_still_fail_existing_structure_and_business_gates(flow, fault, expected):
    request = flow.ready()
    answer = json.loads(flow.answer_text)
    if fault == 'missing_rationale':
        del answer['plans'][0]['rationale']
    elif fault == 'wrong_price':
        answer['plans'][0]['stop_loss'] = '104.001'
    else:
        del answer['candidate_assessments'][0]['missing_conditions']
    flow.answer_text = json.dumps(answer, ensure_ascii=False, separators=(',', ':'))
    started = flow.start(request)
    flow.w.model_api.process_once(flow.w)
    record = flow.record(started)
    assert record['error_code'] == expected and record['answer_valid'] is False
    assert record['version_id'] is None
    assert flow.get('/v1/model/calls/' + record['call_id'] + '/raw')['answer'] == flow.answer_text
    assert len(flow.sent) == 2 and not flow.w.model_api.process_once(flow.w)
