"""SYNTHETIC original-page execution, immutable old records and exact-boundary review."""
import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import pytest
from tests.analysis.test_analysis_lab import flow as flow,add_bundle,prepare,counts,facts
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.shared.values import parse_utc,new_id
from xau_next.platform.model_gateway.lab_simulation import simulation_gateway,simulation_actual
from xau_next.workflows.model_selection import digest,read,load_plan
from tests.guidance.test_workflow import TOKEN
from xau_next.domains.analysis.public import build_packet,frozen_answer_schema
from xau_next.platform.model_gateway.selection import SelectionLedger
from xau_next.workflows.model_selection import seal_plan,freeze_sample
from xau_next.shared.errors import DomainError


@pytest.fixture(autouse=True)
def current_format(legacy_request_format,monkeypatch):
    monkeypatch.setattr('xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION','1.7.0')


def comparison(flow,kind='improved'):
    bundle=add_bundle(flow,multi()) if kind=='improved' else None
    r=prepare(flow,kind=kind,bundle=bundle['bundle_id'] if bundle else None)
    assert r.status_code==201,r.text
    return r.json()


def start(flow,id,body=None,key=None):
    return flow.client.post('/v1/analysis/lab/comparisons/'+id+'/run',json=body or {'mode':'simulation'},
      headers={'Authorization':'Bearer '+TOKEN,'Idempotency-Key':key or new_id()})


def score(flow,id,advance=True):
    return flow.client.post('/v1/analysis/lab/comparisons/'+id+'/score',json={'advance_simulation':advance},
      headers={'Authorization':'Bearer '+TOKEN,'Idempotency-Key':new_id()})


def snapshot(flow,id):return flow.get('/v1/analysis/lab/comparisons/'+id)


def test_end_to_end_four_answers_counting_and_three_windows(flow,monkeypatch):
    record=comparison(flow);id=record['comparison_id'];before=counts(flow)
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('real credentials read in simulation'))
    key=new_id();first=start(flow,id,key=key);again=start(flow,id,key=key)
    assert first.status_code==again.status_code==202 and first.json()==again.json()
    assert start(flow,id).status_code==409
    assert not flow.w.process_once()  # Dedicated comparison channel only.
    assert flow.w.process_once(kind='comparison_run')
    result=snapshot(flow,id);e=result['execution']
    assert e['state']=='awaiting_review',e
    assert e['attempts_reserved']==e['simulation_calls_sent']==4 and e['real_calls_sent']==0
    assert all(a['state']=='succeeded' and a['business_valid'] for a in result['arms']),result['arms']
    assert counts(flow)==before and not flow.sent
    assert score(flow,id,False).status_code==409  # Actual clock has not ended.
    response=score(flow,id);assert response.status_code==202,response.text
    assert flow.w.process_once(kind='comparison_score')
    done=snapshot(flow,id)['execution'];assert done['state']=='completed' and len(done['scores'])==36
    common=[r for r in done['scores'] if r['scope']=='after_common_available' and r['minutes']==30]
    assert {r['arm']:r['direction_class'] for r in common}=={'A':'correct','B':'incorrect','C':'uncertain','D':'correct'}
    assert all(r['range_boundary_mae'] is not None for r in common)
    answer=flow.get('/v1/analysis/lab/comparisons/'+id+'/answers/A')
    assert answer['data_kind']=='SYNTHETIC' and answer['business_valid'] and 'SYNTHETIC' in answer['answer_text']
    assert score(flow,id).status_code==409 and start(flow,id).status_code==409
    assert flow.get('/v1/analysis/lab')['comparisons'][0]['state']=='completed'
    root=flow.w.analysis_lab.comparison.folder(id);p=load_plan(root)
    bodies=[read(root/('sample-'+id)/(arm+'-body.json')) for arm in 'ABCD']
    assert len({b['input'] for b in bodies})==1 and p['input_revision']=='macro-research/2'
    assert not (flow.w.db.data_root/'db/xau-next.sqlite3').samefile(Path(p['formal_root'])/'db/xau-next.sqlite3')


def test_default_real_call_requires_new_explicit_authorization_before_key_read(flow,monkeypatch):
    id=comparison(flow,'current')['comparison_id']
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('unauthorized key read'))
    r=start(flow,id,{'mode':'real'})
    assert r.status_code==409 and r.json()['code']=='COMPARISON_BUDGET_REQUIRED'
    assert snapshot(flow,id)['execution'] is None and not flow.sent


def test_pending_scores_are_automatically_enqueued_at_each_due_window(flow):
    id=comparison(flow,'current')['comparison_id'];assert start(flow,id).status_code==202
    flow.w.process_once(kind='comparison_run')
    for minutes in [33,63,183]:
        flow.clock.value=(parse_utc('2026-10-12T02:00:00Z')+timedelta(minutes=minutes)).isoformat().replace('+00:00','Z')
        flow.w.analysis_lab.comparison.enqueue_due()
        assert flow.w.process_once(kind='comparison_score')
        flow.w.analysis_lab.comparison.enqueue_due()
        assert not flow.w.process_once(kind='comparison_score')
    assert snapshot(flow,id)['execution']['completed_horizons']==[30,60,180]


def test_cancelled_queued_group_cannot_be_restarted(flow):
    id=comparison(flow,'current')['comparison_id'];r=start(flow,id);job=r.json()
    flow.w.jobs.cancel(job['job_id']);assert not flow.w.process_once(kind='comparison_run')
    flow.w.analysis_lab.comparison.recover()
    assert snapshot(flow,id)['state']=='cancelled' and start(flow,id).status_code==409 and not flow.sent


def test_invalid_answer_preserved_and_not_turned_into_prediction_success(flow):
    def change(answer,arm):
        if arm=='A':answer['market_cutoff']='bad date'
        return answer
    flow.w.analysis_lab.comparison.simulation_factory=lambda q:simulation_gateway(q,change=change)
    id=comparison(flow)['comparison_id'];start(flow,id);flow.w.process_once(kind='comparison_run')
    result=snapshot(flow,id)
    a=next(a for a in result['arms'] if a['arm']=='A')
    assert a['state']=='failed' and not a['business_valid'] and a['answer_available']
    assert start(flow,id).status_code==409
    raw=flow.get('/v1/analysis/lab/comparisons/'+id+'/answers/A')
    assert 'bad date' in raw['answer_text'] and not raw['schema_valid']


def test_missing_endpoint_ticks_remain_unknown_even_when_ranges_can_be_measured(flow):
    def actual(q,asof):
        value=simulation_actual(q,as_of=asof);value.update(ticks=[],tick_status='unavailable');return value
    flow.w.analysis_lab.comparison.actual_provider=actual
    id=comparison(flow,'current')['comparison_id'];start(flow,id);flow.w.process_once(kind='comparison_run')
    assert score(flow,id).status_code==202;flow.w.process_once(kind='comparison_score')
    rows=snapshot(flow,id)['execution']['scores']
    assert all(r['direction_class']=='unknown' for r in rows) and any(r['range_boundary_mae'] is not None for r in rows)


def test_short_common_window_is_explicitly_unusable(flow):
    id=comparison(flow,'current')['comparison_id'];start(flow,id)
    flow.clock.value='2026-10-12T02:20:00Z';flow.w.process_once(kind='comparison_run')
    score(flow,id);flow.w.process_once(kind='comparison_score')
    rows=snapshot(flow,id)['execution']['scores']
    assert all(r['usable_primary'] is False for r in rows if r['minutes']==30 and r['scope']=='after_common_available')


def test_incomplete_claim_after_crash_recovers_unknown_and_never_resends(flow):
    id=comparison(flow,'current')['comparison_id'];job=start(flow,id).json();e=flow.w.analysis_lab.lab.execution(id)
    root=flow.w.analysis_lab.comparison.folder(id);p=seal_plan(root,{**e['plan'],'status':'DRAFT'})
    q=flow.w.analysis_lab.lab.internal_comparison(id)['frozen_request'];freeze_sample(root,e['sample_id'],q,clock=flow.clock)
    SelectionLedger(root/'selection.sqlite3',digest(p)).claim(p['experiment_id']+':'+e['sample_id']+':A',1,'saved_cpa',flow.clock(),
      total_limit=4,stage_limit=24,daily_limit=50)
    lease=flow.w.jobs.claim('SYNTHETIC-crash',kind='comparison_run',lease_seconds=1);assert lease
    flow.clock.value='2026-10-12T02:00:02Z'
    assert not flow.w.process_once(kind='comparison_run');flow.w.analysis_lab.comparison.recover()
    result=snapshot(flow,id)
    assert result['state']=='interrupted' and result['execution']['attempts_reserved']==1
    assert next(a for a in result['arms'] if a['arm']=='A')['state']=='outcome_unknown'
    assert start(flow,id).status_code==409 and not flow.sent
    with pytest.raises(DomainError):flow.w.jobs.retry(job['job_id'])


def make_real_mode_control_fixture(flow):
    """SYNTHETIC metadata for real-mode control tests; transport remains MockTransport."""
    record=comparison(flow,'current');id=record['comparison_id']
    original=flow.w.analysis_lab.lab.internal_comparison(id);q=deepcopy(original['frozen_request']);synthetic=deepcopy(q)
    q['context']['market']['source_kind']='mt5'
    q['packet_text']=build_packet({k:v for k,v in q.items() if k not in {'context','packet_text'}},q['context'],frozen_answer_schema(q))
    original.update(data_kind='REAL',frozen_request=q,packet_text=q['packet_text'],packet_sha256=digest(q['packet_text'].encode()))
    original['arms']=[{**a,'packet_sha256':original['packet_sha256']} for a in original['arms']]
    with flow.w.db.write() as uow:
        flow.w.analysis_lab.lab.docs.update(uow,'analysis_comparison',id,original,original['revision'])
        for name,base in [('cpa','http://127.0.0.1:8317/v1'),('deep','https://api.deepseek.com/v1')]:
            values=deepcopy(flow.w.model_api.settings.snapshot(scope=uow)['values'])
            values.update(base_url=base,protocol='responses',daily_call_limit=50)
            flow.w.model_api.settings.docs.insert(uow,'model_profile',name,{'name':'SYNTHETIC '+name,'values':values,
               'credential_ref':'XAU-Next/model/SYNTHETIC-'+name,'config_revision':1})
            flow.credentials.write('XAU-Next/model/SYNTHETIC-'+name,'SYNTHETIC-COMPARISON-ONLY')
    flow.w.market._mode='mt5'  # Only gating metadata; no native bridge request is made.
    flow.w.model_api.gateway=simulation_gateway(synthetic)
    body={'mode':'real','max_calls':4,'cpa_profile_id':'cpa','deepseek_profile_id':'deep',
      'acknowledge_possible_charge':True,'accept_unknown_cost':True,'allow_readonly_history':True,
      'authorization_text':'授权本组最多4次真实模型调用'}
    return id,body


@pytest.mark.parametrize('field',['acknowledge_possible_charge','accept_unknown_cost','allow_readonly_history','max_calls','authorization_text'])
def test_real_mode_missing_any_group_approval_is_blocked_before_credentials(flow,field,monkeypatch):
    id,body=make_real_mode_control_fixture(flow);body.pop(field)
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('credential read before approval'))
    result=start(flow,id,body);assert result.status_code==409 and result.json()['code']=='COMPARISON_BUDGET_REQUIRED'


def test_real_mode_binds_profiles_and_counts_only_in_the_isolated_test_database(flow):
    id,body=make_real_mode_control_fixture(flow);before=counts(flow);profile=flow.w.model_api.settings.profiles()['active_profile_id']
    result=start(flow,id,body);assert result.status_code==202,result.text
    flow.w.process_once(kind='comparison_run');record=snapshot(flow,id)
    assert record['state']=='awaiting_review',json.dumps(flow.get('/v1/jobs/'+record['execution']['job_id'])['error'],ensure_ascii=False)
    assert record['execution']['real_calls_sent']==record['execution']['attempts_reserved']==4
    assert record['execution']['simulation_calls_sent']==0 and counts(flow)==before
    with flow.w.db.read() as scope:
        budget=flow.w.model_api.calls.docs.get(scope,'experience_experiment_budget','lab-'+id)
    assert budget['count']==4 and flow.w.model_api.settings.profiles()['active_profile_id']==profile
    assert start(flow,id,body).status_code==409


def test_real_mode_expired_input_and_changed_profile_do_not_dispatch(flow,monkeypatch):
    id,body=make_real_mode_control_fixture(flow)
    flow.clock.value='2026-10-12T02:00:31Z'
    assert start(flow,id,body).json()['code']=='COMPARISON_SOURCE_STALE'
    flow.clock.value='2026-10-12T02:00:00Z';assert start(flow,id,body).status_code==202
    with flow.w.db.write() as uow:
        profile=flow.w.model_api.settings.docs.get(uow,'model_profile','cpa')
        profile['values']['timeout_seconds']=299
        flow.w.model_api.settings.docs.update(uow,'model_profile','cpa',profile,profile['revision'])
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('changed profile read a key'))
    flow.w.process_once(kind='comparison_run');flow.w.analysis_lab.comparison.recover()
    assert snapshot(flow,id)['state']=='interrupted' and snapshot(flow,id)['execution']['real_calls_sent']==0


def test_real_group_never_allows_simulated_future_scoring(flow):
    id,body=make_real_mode_control_fixture(flow);start(flow,id,body);flow.w.process_once(kind='comparison_run')
    response=score(flow,id,True)
    assert response.status_code==409 and response.json()['code']=='COMPARISON_SYNTHETIC_ONLY'
