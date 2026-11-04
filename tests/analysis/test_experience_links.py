"""SYNTHETIC relationship diagnostics, immutable old records and optional28 guidance."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest
from tests.analysis.test_experience_application import prepared as prepared, legacy_request_format as legacy_request_format
from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, post, run, execute
from tests.analysis.test_analysis_lab import add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.analysis.test_member_once import prepare27
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.public import build_packet, frozen_answer_schema, outbound_packet, experience_effect_diagnosis
from xau_next.domains.analysis.experience import validate_experience_application
from xau_next.domains.analysis.experience_link_candidate import INSTRUCTIONS, project_plan
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc


def facts(packet):return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]


def gap_case(prepared):
    _,_,context,answer=prepared;context=deepcopy(context);answer=deepcopy(answer)
    case=deepcopy(answer['experience_application']['case_assessments'][0]);source=deepcopy(context['experience']['cases'][0])
    assessments=[];cases=[]
    for index in range(8):
        ref='SYNTHETIC-review-'+str(index)
        assessments.append({**deepcopy(case),'review_ref':ref,'reason':'SYNTHETIC 原采用理由'+str(index+1),'decision':'do_not_adopt' if index==2 else 'adopt'})
        cases.append({**deepcopy(source),'review_ref':ref})
    context['experience']['cases']=cases;context['evidence_refs'] += [row['review_ref'] for row in cases]
    answer['experience_application']['case_assessments']=assessments
    answer['prediction_adjustments'][0]['related_review_refs']=[assessments[i]['review_ref'] for i in [0,3,5,7]]
    return context,answer


def test_exact_gap_counts_use_case_positions_and_do_not_claim_missing_prose(prepared):
    context,answer=gap_case(prepared);raw=json.dumps(answer);before=deepcopy((answer,context))
    diagnosis=experience_effect_diagnosis(raw,context)
    assert (diagnosis['adopted_count'],diagnosis['linked_count'],diagnosis['missing_count'])==(7,4,3)
    assert [r['case_number'] for r in diagnosis['missing_cases']]==[2,5,7]
    assert '第2、5、7条' in diagnosis['detail'] and '案例理由中的文字不能代替关联引用' in diagnosis['detail']
    with pytest.raises(DomainError) as caught:validate_experience_application(answer,context)
    assert caught.value.code=='EXPERIENCE_EFFECT_MISSING'
    assert [e['path'] for e in caught.value.field_errors]==[f'/experience_application/case_assessments/{i}/review_ref' for i in [1,4,6]]
    assert (answer,context)==before


def test_grouped_retained_effect_and_rejected_cases_are_not_false_gaps(prepared):
    context,answer=gap_case(prepared)
    answer['prediction_adjustments'][0]['related_review_refs']=[r['review_ref'] for r in answer['experience_application']['case_assessments'] if r['decision']=='adopt']
    validate_experience_application(answer,context)
    assert experience_effect_diagnosis(json.dumps(answer),context) is None
    answer['experience_application']['status']='reviewed_no_change'
    for row in answer['experience_application']['case_assessments']:row['decision']='do_not_adopt'
    answer['prediction_adjustments']=[];validate_experience_application(answer,context)
    assert experience_effect_diagnosis(json.dumps(answer),context) is None


@pytest.mark.parametrize('fault',['digest','unregistered','duplicate','malformed','oversized','unicode','reason'])
def test_other_failures_and_oversized_input_do_not_invent_a_gap(prepared,fault):
    context,answer=gap_case(prepared)
    if fault=='digest':answer['experience_application']['digest_id']='WRONG'
    if fault=='unregistered':answer['prediction_adjustments'][0]['related_review_refs']=['UNKNOWN']
    if fault=='duplicate':answer['experience_application']['case_assessments'][1]=deepcopy(answer['experience_application']['case_assessments'][0])
    if fault=='reason':answer['experience_application']['case_assessments'][1]['reason']=None
    raw='{' if fault=='malformed' else 'x'*2000001 if fault=='oversized' else json.dumps(answer)
    if fault=='unicode':raw='\ud800'
    assert experience_effect_diagnosis(raw,context) is None


def test_saved_old_failure_get_is_readonly_and_does_not_resend(prepared,monkeypatch):
    f,request,_,_=prepared;context,answer=gap_case(prepared)
    with f.w.db.write() as tx:
        row=f.w.analysis._docs.get(tx,'request',request['request_id']);changed={**row,'context':context}
        changed['packet_text']=build_packet({k:v for k,v in row.items() if k not in {'context','packet_text'}},context,frozen_answer_schema(row))
        f.w.analysis._docs.update(tx,'request',request['request_id'],changed,row['revision'])
    f.setup_model();started=f.start(request);id=started['call']['call_id'];raw=json.dumps(answer).encode()
    with f.w.db.write() as tx:
        ref=f.w.model_api.calls.register(f.w.model_api.calls.prepare(raw),uow=tx)
        f.w.model_api.calls.update(id,{'state':'failed','stage':'response_saved','error_code':'EXPERIENCE_EFFECT_MISSING','detail':'原通用错误','answer':ref,'answer_valid':False},uow=tx)
    before=deepcopy(f.w.model_api.calls.get(id,internal=True));count=len(f.sent)
    monkeypatch.setattr(f.credentials,'read',lambda *_:pytest.fail('Unapproved key read'))
    view=f.get('/v1/model/calls/'+id+'/raw')
    assert view['answer'].encode()==raw and view['experience_effect_diagnostics']['missing_count']==3
    assert f.w.model_api.calls.get(id,internal=True)==before and len(f.sent)==count
    assert f.get('/v1/model/calls/'+id)['detail']=='原通用错误'


def prepare28(f,models='D',kind='new',base=None):
    bundle=add_bundle(f,multi())
    with f.w.db.read() as tx:market=f.w.market.latest(scope=tx).to_dict()
    response=post(f,'/v1/analysis/daily/groups',{'package_kind':'candidate28','models':list(models),'kind':kind,'base_group_id':base,'bundle_id':bundle['bundle_id'],'market_snapshot_id':market['snapshot_id'],'horizon_minutes':[30,60,180] if kind=='new' else None})
    assert response.status_code==201,response.text
    return response.json()


@pytest.mark.parametrize('models',['D','ABCD'])
def test28_preserves_all27_facts_and_projects_only_frozen_guidance(flow,monkeypatch,models):
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Key read'))
    group=prepare28(flow,models);assert group['template_version']=='manual-analysis/28' and not group['run_registered']
    for arm in group['arms']:
        with flow.w.db.read() as tx:q=flow.w.analysis.model_request(arm['request_id'],scope=tx)['request']
        before=deepcopy(q);manual=facts(q['packet_text']);api=facts(outbound_packet(q['packet_text']))
        plan=manual['experience_link_check'];assert api['experience_link_check']==plan
        assert plan['coverage_rule']=='adopted_review_refs_subset_of_related_review_refs'
        assert INSTRUCTIONS in q['packet_text'] and 'A减B为空' in q['packet_text']
        control=build_packet({**facts(q['packet_text'])['request'],'prompt_version':'manual-analysis/27'},q['context'],frozen_answer_schema(q))
        for new,old in [(manual,facts(control)),(api,facts(outbound_packet(control)))]:
            new=deepcopy(new);new.pop('experience_link_check');new['request']['prompt_version']='manual-analysis/27';assert new==old
        extra={**deepcopy(plan),'private_debug':'DO_NOT_SEND'};assert project_plan(extra,manual)==plan
        extra['allowed_case_review_refs'].append('UNKNOWN')
        with pytest.raises(DomainError):project_plan(extra,manual)
        assert q==before
    assert not flow.sent


def test28_simulation_and_update_preserve_original_endpoints(flow):
    group=prepare28(flow,'AD');assert run(flow,group['group_id']).status_code==202;execute(flow,2)
    saved=flow.get('/v1/analysis/daily/groups/'+group['group_id'])
    assert all(a['state']=='succeeded' and a['model_call']['outbound_transform_version']=='api-projection/28-links1' for a in saved['arms'])
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(minutes=1)).isoformat().replace('+00:00','Z');flow.refresh()
    update=prepare28(flow,'AD','update',group['group_id']);assert update['window_end']==group['window_end'] and update['horizon_minutes']==group['horizon_minutes']
    assert run(flow,update['group_id']).status_code==202;execute(flow,2)
    assert all(a['state']=='succeeded' for a in flow.get('/v1/analysis/daily/groups/'+update['group_id'])['arms'])
    assert not flow.sent
