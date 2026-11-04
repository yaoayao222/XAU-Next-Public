"""SYNTHETIC daily rounds: real SQLite, strict validation, no external I/O."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest
from tests.analysis.test_analysis_lab import flow as flow,add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.guidance.test_workflow import TOKEN
from xau_next.shared.values import new_id,parse_utc
from datetime import timedelta

@pytest.fixture(autouse=True)
def current_format(legacy_request_format,monkeypatch):
    monkeypatch.setattr('xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION','1.7.0')

def post(f,path,body,key=None):
    return f.client.post(path,json=body,headers={'Authorization':'Bearer '+TOKEN,'Idempotency-Key':key or new_id()})

def prepare(f,models='ABCD',template='current',kind='new',base=None):
    bundle=add_bundle(f,multi()) if template=='improved' else None
    with f.w.db.read() as scope:market=f.w.market.latest(scope=scope).to_dict()
    r=post(f,'/v1/analysis/daily/groups',{'models':list(models),'package_kind':template,'kind':kind,
      'base_group_id':base,'bundle_id':bundle['bundle_id'] if bundle else None,
      'market_snapshot_id':market['snapshot_id'],'horizon_minutes':[30,60,180] if kind=='new' else None})
    assert r.status_code==201,r.text
    return r.json()

def run(f,id,**kw):
    group=f.get('/v1/analysis/daily/groups/'+id)
    body={'mode':'simulation','max_calls':len(group['arms']),'acknowledge_possible_charge':True,
      'accept_unknown_cost':True,'allow_readonly_history':True,'cpa_profile_id':None,'deepseek_profile_id':None,**kw}
    return post(f,'/v1/analysis/daily/groups/'+id+'/run',body)

def execute(f,n):
    with ThreadPoolExecutor(max_workers=n) as pool:
        done=list(pool.map(lambda _:f.w.model_api.process_once(f.w,kind='daily_model_call'),range(n)))
    assert all(done)

@pytest.mark.parametrize('models',['A','BD','ACD','ABCD'])
@pytest.mark.parametrize('template',['current','improved'])
def test_selected_models_have_independent_normal_forecasts_and_no_primary(flow,models,template,monkeypatch):
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Real credential adapter read'))
    g=prepare(flow,models,template);assert len(g['arms'])==len(models)
    assert len({a['forecast_id'] for a in g['arms']})==len(models)
    assert g['same_question'] and g['template_version']=='manual-analysis/'+('14' if template=='current' else '23')
    with flow.w.db.read() as tx:
        contexts=[flow.w.analysis.model_request(a['request_id'],scope=tx)['request']['context'] for a in g['arms']]
    assert all(c==contexts[0] for c in contexts)
    response=run(flow,g['group_id']);assert response.status_code==202,response.text
    assert not flow.w.process_once()  # Generic worker cannot dispatch paid-model jobs.
    execute(flow,len(models))
    result=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    assert all(a['state']=='succeeded' and a['version_id'] for a in result['arms']),result
    assert len({a['version_id'] for a in result['arms']})==len(models)
    assert flow.get('/v1/workspace')['current_forecast'] is None
    assert len(flow.get('/v1/workspace')['daily_group']['arms'])==len(models)
    assert run(flow,g['group_id']).status_code==409
    assert not flow.sent
    for arm in result['arms']:
        v=flow.get('/v1/forecasts/'+arm['forecast_id']+'/versions/'+arm['version_id'])
        assert v['origin']['model_id']==arm['model_id'] and 'SYNTHETIC' in v['raw_text']

def test_group_requires_exact_authorization_and_blocks_ordinary_send(flow,monkeypatch):
    g=prepare(flow,'AD')
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Unapproved credential read'))
    r=run(flow,g['group_id'],max_calls=4);assert r.status_code==409 and r.json()['code']=='DAILY_AUTHORIZATION_REQUIRED'
    r=run(flow,g['group_id'],acknowledge_possible_charge=False);assert r.status_code==409
    r=post(flow,'/v1/analysis/requests/'+g['arms'][0]['request_id']+'/model-call',{'retry_of':None,'acknowledge_possible_charge':True})
    assert r.status_code==409 and r.json()['code']=='DAILY_GROUP_RUN_REQUIRED'
    assert run(flow,g['group_id'],mode='real').status_code==409
    assert not flow.sent

def test_updates_keep_each_models_own_base_and_original_endpoints(flow):
    g=prepare(flow,'AB');assert run(flow,g['group_id']).status_code==202;execute(flow,2)
    prior=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(minutes=1)).isoformat().replace('+00:00','Z');flow.refresh()
    update=prepare(flow,'AB',kind='update',base=g['group_id'])
    assert not update['same_question'] and update['window_end']==g['window_end']
    for arm,old in zip(update['arms'],prior['arms']):
        request=flow.get('/v1/analysis/requests/'+arm['request_id'])
        assert request['forecast_id']==old['forecast_id'] and request['base_version_id']==old['version_id']
        with flow.w.db.read() as tx:raw=flow.w.analysis.model_request(arm['request_id'],scope=tx)['request']
        assert raw['context']['previous_analysis']['version_id']==old['version_id']
    assert run(flow,update['group_id']).status_code==202;execute(flow,2)
    assert all(a['state']=='succeeded' for a in flow.get('/v1/analysis/daily/groups/'+update['group_id'])['arms'])

def test_cancelled_queued_branch_is_not_resent(flow):
    g=prepare(flow,'AB');assert run(flow,g['group_id']).status_code==202
    r=flow.get('/v1/analysis/daily/groups/'+g['group_id']);job=r['arms'][0]['model_call']['job_id']
    with flow.w.db.write() as tx:flow.w.model_api.cancel(job,uow=tx)
    while flow.w.model_api.process_once(flow.w,kind='daily_model_call'):pass
    result=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    assert result['arms'][0]['state']=='cancelled' and result['arms'][1]['state']=='succeeded'
    assert run(flow,g['group_id']).status_code==409


def test_idempotent_prepare_and_start_replay_exact_round_without_extra_calls(flow):
    with flow.w.db.read() as tx:market=flow.w.market.latest(scope=tx).to_dict()
    body={'models':['B','D'],'package_kind':'current','kind':'new','base_group_id':None,'bundle_id':None,
          'market_snapshot_id':market['snapshot_id'],'horizon_minutes':[30,60,180]}
    key=new_id();a=post(flow,'/v1/analysis/daily/groups',body,key);b=post(flow,'/v1/analysis/daily/groups',body,key)
    assert a.status_code==b.status_code==201 and a.json()==b.json()
    path='/v1/analysis/daily/groups/'+a.json()['group_id']+'/run'
    command={'mode':'simulation','max_calls':2,'acknowledge_possible_charge':True,'accept_unknown_cost':True,
             'allow_readonly_history':True,'cpa_profile_id':None,'deepseek_profile_id':None}
    key=new_id();a=post(flow,path,command,key);b=post(flow,path,command,key)
    assert a.status_code==b.status_code==202 and a.json()==b.json()
    execute(flow,2);assert not flow.w.model_api.process_once(flow.w,kind='daily_model_call')
    with flow.w.db.read() as tx:assert len(flow.w.analysis._docs.list(tx,'model_call',limit=100))==2


@pytest.mark.parametrize('template',['current','improved'])
def test_update_guidance_does_not_create_ungrouped_auto_requests(flow,template):
    g=prepare(flow,'AB',template);run(flow,g['group_id']);execute(flow,2)
    with flow.w.db.read() as tx:before=len(flow.w.analysis._docs.list(tx,'request',limit=100))
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(minutes=28)).isoformat().replace('+00:00','Z');flow.refresh()
    flow.w.daily.tick_guidance()
    with flow.w.db.read() as tx:assert len(flow.w.analysis._docs.list(tx,'request',limit=100))==before
    update=prepare(flow,'AB',template,kind='update',base=g['group_id'])
    assert run(flow,update['group_id']).status_code==202;execute(flow,2)
    assert all(a['state']=='succeeded' for a in flow.get('/v1/analysis/daily/groups/'+update['group_id'])['arms'])


def test_expired_lease_after_send_becomes_unknown_and_never_resends(flow):
    g=prepare(flow,'A');assert run(flow,g['group_id']).status_code==202
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    lease=flow.w.jobs.claim('SYNTHETIC-crashed-worker',kind='daily_model_call',lease_seconds=1)
    with flow.w.db.write() as tx:flow.w.model_api.calls.update(arm['model_call']['call_id'],
          {'stage':'sent_waiting','state':'waiting','started_at':flow.clock.value},uow=tx)
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(seconds=2)).isoformat().replace('+00:00','Z')
    assert not flow.w.model_api.process_once(flow.w,kind='daily_model_call')
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    assert arm['state']=='outcome_unknown' and not arm['version_id']
    assert not flow.sent and run(flow,g['group_id']).status_code==409


def test_invalid_answer_saves_raw_but_never_repays(flow,monkeypatch):
    from xau_next.platform.model_gateway.lab_simulation import simulation_gateway
    def invalid(q):
        def change(answer,arm):answer['horizons'][0]['expected_range']={'low':'200','high':'100'};return answer
        return simulation_gateway(q,change=change)
    monkeypatch.setattr('xau_next.workflows.model_api.simulation_gateway',invalid)
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1)
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    assert arm['state']=='failed' and not arm['version_id'] and arm['model_call']['raw_response']
    assert not flow.w.model_api.process_once(flow.w,kind='daily_model_call') and not flow.sent


def fake_real_group(f,monkeypatch):
    """Only in this isolated SYNTHETIC DB; no provider or Windows key access."""
    original=f.w.daily.store.get
    def marked(*args,**kw):row=deepcopy(original(*args,**kw));row['data_kind']='REAL';return row
    monkeypatch.setattr(f.w.daily.store,'get',marked)
    monkeypatch.setattr(type(f.w.market),'source_kind',property(lambda _:'mt5'))


def test_real_stale_source_block_precedes_any_credential_read(flow,monkeypatch):
    g=prepare(flow,'A');fake_real_group(flow,monkeypatch)
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(seconds=31)).isoformat().replace('+00:00','Z')
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Stale source read key'))
    r=run(flow,g['group_id'],mode='real',cpa_profile_id='SYNTHETIC-only')
    assert r.status_code==409 and r.json()['code']=='DAILY_SOURCE_STALE' and not flow.sent


def test_old_quote_block_precedes_any_credential_read(flow,monkeypatch):
    g=prepare(flow,'A');fake_real_group(flow,monkeypatch)
    original=flow.w.analysis.model_request
    def old_quote(*args,**kw):
        data=deepcopy(original(*args,**kw));q=data['request'];q['context']['market']['quote']['event_time']='2000-01-01T00:00:00Z';return data
    monkeypatch.setattr(flow.w.analysis,'model_request',old_quote)
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Old quote read key'))
    r=run(flow,g['group_id'],mode='real',cpa_profile_id='SYNTHETIC-only')
    assert r.status_code==409 and r.json()['code']=='DAILY_QUOTE_STALE' and not flow.sent


@pytest.mark.parametrize('failure,code',[('stale','DAILY_DISPATCH_STALE'),('profile','DAILY_PROFILE_CHANGED')])
def test_dispatch_rechecks_frozen_source_and_profile_before_key_or_http(flow,monkeypatch,failure,code):
    g=prepare(flow,'A');fake_real_group(flow,monkeypatch)
    profile=deepcopy(flow.w.model_api.settings.snapshot())
    profile.update(profile_id='SYNTHETIC-channel',credential_ref='XAU-Next/model/SYNTHETIC-dispatch')
    profile['values'].update(enabled=True,provider='SYNTHETIC-only',base_url='http://127.0.0.1:8317/v1',daily_budget=None)
    flow.credentials.write(profile['credential_ref'],'SYNTHETIC-NOT-A-REAL-KEY')
    monkeypatch.setattr(flow.w.model_api.settings,'comparison_profile',lambda *_,**__:deepcopy(profile))
    r=run(flow,g['group_id'],mode='real',cpa_profile_id=profile['profile_id']);assert r.status_code==202,r.text
    if failure=='stale':flow.clock.value=(parse_utc(flow.clock.value)+timedelta(seconds=61)).isoformat().replace('+00:00','Z')
    else:profile['values']['provider']='SYNTHETIC-changed'
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Dispatch guard read a key'))
    assert flow.w.model_api.process_once(flow.w,kind='daily_model_call')
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    assert arm['state']=='failed' and arm['model_call']['error_code']==code and not arm['version_id']
    assert not flow.sent and not flow.w.model_api.process_once(flow.w,kind='daily_model_call')


def test_candidate_plans_and_guidance_keep_their_own_model_version(flow,monkeypatch):
    from xau_next.platform.model_gateway.lab_simulation import simulation_gateway
    flow.configure()
    def candidates(q):
        def change(answer,key):
            public=flow.w.analysis.get_request(q['request_id']).to_dict()
            candidate=flow.answer(public)
            candidate.update(horizons=answer['horizons'],summary='SYNTHETIC candidate '+key)
            return candidate
        return simulation_gateway(q,change=change)
    monkeypatch.setattr('xau_next.workflows.model_api.simulation_gateway',candidates)
    g=prepare(flow,'AB');run(flow,g['group_id']);execute(flow,2)
    result=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    for arm in result['arms']:
        assert arm['state']=='succeeded',arm['model_call']
        assert len(arm['plans'])==1 and arm['plans'][0]['version_id']==arm['version_id']
        assert all(a['version_id']==arm['version_id'] for a in arm['guidance']['action_advice'])
        assert all(r['forecast_id']==arm['forecast_id'] for r in arm['guidance']['analysis_refresh'])
    assert result['arms'][0]['plans'][0]['plan_id']!=result['arms'][1]['plans'][0]['plan_id']
    assert flow.get('/v1/workspace')['plans']==[]
