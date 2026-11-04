"""SYNTHETIC new answer explanations and frozen legacy import compatibility."""
import copy
import json

import pytest
from xau_next.domains.analysis.validation import validate_answer
from xau_next.shared.errors import DomainError

from tests.model_api.test_experience_projection import synthetic_experience
from tests.model_api.test_workflow import ModelFlow


@pytest.fixture(autouse=True)
def legacy_request_format():
    """This module tests current behavior; do not apply directory legacy-format pinning."""


@pytest.fixture
def prepared(tmp_path):
    flow = ModelFlow(tmp_path)
    flow.configure(); req = flow.request(flow.refresh())
    with flow.w.db.read() as tx:
        row = flow.w.analysis._docs.get(tx, 'request', req['request_id'])
    answer = flow.answer(req)
    request = copy.deepcopy(row)
    context = copy.deepcopy(row['context'])
    exp = synthetic_experience(); context['experience'] = exp
    ref = exp['cases'][0]['review_ref']; context['evidence_refs'] += [exp['digest_id'],ref]
    answer.update(schema_version=request['answer_schema_version'], experience_application={'digest_id':exp['digest_id'], 'status':'applied',
        'case_assessments':[{'review_ref':ref,'relevance':'partial','current_evidence_refs':[req['market_snapshot_id']],
            'similarities':['SYNTHETIC 同样在区间边缘。'],'differences':['当前周期不同。'],
            'decision':'adopt','reason':'保留原方向，明确历史失败不能自动变成反向规则。'}],
        'limitations':['只验证字段与引用，不证明解释正确。']},
        prediction_adjustments=[{'horizon_minutes':req['horizon_minutes'][0], 'aspects':['direction','range'],
            'related_review_refs':[ref], 'action':'retained','explanation':'依据当前报价结构保持判断；历史反例仅提示适用边界。'}])
    yield flow, request, context, answer
    flow.close()


def check(prepared, answer=None, context=None):
    flow, request, original_context, original_answer = prepared
    validate_answer(answer or original_answer, flow.app.state.contracts.answer, request, context or original_context)


def test_current_request_uses_new_version_and_empty_experience_is_explicitly_unavailable(prepared):
    flow, request, _, _ = prepared
    with flow.w.db.read() as tx:
        row = flow.w.analysis._docs.get(tx,'request',request['request_id'])
    assert row['answer_schema_version'] == '1.7.0'
    assert row['prompt_version'] == 'manual-analysis/14'
    assert row['context']['experience'] is None


def test_experience_may_support_retaining_a_prediction_without_fictional_counterfactual(prepared):
    check(prepared)
    _,_,context,answer = prepared
    declined = copy.deepcopy(answer); declined['experience_application']['status']='reviewed_no_change'
    declined['experience_application']['case_assessments'][0].update(decision='do_not_adopt',relevance='not_applicable')
    declined['prediction_adjustments']=[]
    check(prepared,declined)
    unknown = copy.deepcopy(declined);unknown['experience_application']['case_assessments'][0].update(decision='uncertain',relevance='unknown')
    check(prepared,unknown)
    empty = copy.deepcopy(answer);empty['experience_application'].update(digest_id=None,status='unavailable',case_assessments=[]);empty['prediction_adjustments']=[]
    check(prepared,empty,{**context,'experience':None})


@pytest.mark.parametrize('mutation,code', [
    ('digest','EXPERIENCE_DIGEST_MISMATCH'), ('case','EXPERIENCE_CASE_REF_INVALID'),
    ('current','EXPERIENCE_CURRENT_REF_INVALID'), ('horizon','EXPERIENCE_HORIZON_INVALID'),
    ('unregistered','EXPERIENCE_ADJUSTMENT_REF_INVALID'), ('no_adopt','EXPERIENCE_STATUS_INVALID'),
    ('no_effect','EXPERIENCE_EFFECT_MISSING'), ('unavailable','EXPERIENCE_STATUS_INVALID'),
    ('duplicate','EXPERIENCE_CASE_DUPLICATE'), ('reviewed_adjusted','EXPERIENCE_STATUS_INVALID'),
])
def test_invalid_experience_relationship_is_rejected_with_a_field(prepared, mutation, code):
    answer = copy.deepcopy(prepared[3]);app=answer['experience_application'];case=app['case_assessments'][0]
    if mutation=='digest':app['digest_id']='exp-wrong'
    elif mutation=='case':case['review_ref']='review-not-in-packet'
    elif mutation=='current':case['current_evidence_refs']=['not-current-evidence']
    elif mutation=='horizon':answer['prediction_adjustments'][0]['horizon_minutes']=17
    elif mutation=='unregistered':answer['prediction_adjustments'][0]['related_review_refs']=['review-not-assessed']
    elif mutation=='no_adopt':case['decision']='uncertain'
    elif mutation=='no_effect':answer['prediction_adjustments']=[]
    elif mutation=='unavailable':app['status']='unavailable'
    elif mutation=='duplicate':app['case_assessments'].append(copy.deepcopy(case))
    elif mutation=='reviewed_adjusted':app['status']='reviewed_no_change';case['decision']='do_not_adopt';answer['prediction_adjustments'][0]['action']='adjusted'
    with pytest.raises(DomainError) as error:check(prepared,answer)
    assert error.value.code==code
    assert error.value.field_errors[0]['path'].startswith(('/experience_application','/prediction_adjustments'))


def test_no_experience_cannot_claim_adoption(prepared):
    _,_,context,answer=prepared
    changed=copy.deepcopy(answer);changed['experience_application']['digest_id']=None
    with pytest.raises(DomainError) as error:check(prepared,changed,{**context,'experience':None})
    assert error.value.code=='EXPERIENCE_STATUS_INVALID'


def test_old_request_schema_is_read_from_the_saved_packet_not_new_global_contract(tmp_path,monkeypatch):
    from xau_next.domains.analysis.service import AnalysisService
    monkeypatch.setattr('xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION','1.3.0')
    monkeypatch.setattr('xau_next.domains.analysis.service.PROMPT_VERSION','manual-analysis/7')
    monkeypatch.setitem(AnalysisService.create_request.__kwdefaults__,'prompt_version','manual-analysis/7')
    flow=ModelFlow(tmp_path)
    try:
        flow.configure();req=flow.request(flow.refresh());answer=flow.answer(req)
        frozen=req['packet_text']
        flow.w.analysis._schema['required'].append('future_required_field')
        with flow.w.db.read() as tx:
            _,schema,_=flow.w.analysis.model_packet(req['request_id'],scope=tx)
        assert 'future_required_field' not in schema['required']
        prepared=flow.w.analysis.prepare_import({'request_id':req['request_id'],'raw_text':json.dumps(answer), 'source':'manual_paste'})
        assert prepared.error is None
        assert flow.get('/v1/analysis/requests/'+req['request_id'])['packet_text']==frozen
    finally:flow.close()
