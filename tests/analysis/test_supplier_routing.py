"""Supplier-aware daily analysis: isolated SYNTHETIC inputs and mocked HTTP only."""
import json
from copy import deepcopy

import httpx
import pytest

from tests.analysis.test_daily_analysis import flow, current_format, prepare, run, execute, post, fake_real_group
from tests.analysis.test_daily_performance import score_saved, stats
from tests.experiments.test_model_selection import forbid_real_http
from xau_next.platform.model_gateway.public import Gateway, request_body
from xau_next.platform.model_gateway.lab_simulation import answer_for


def supplier(f, name, base, model, protocol='chat_completions', cap=31000):
    old=f.get('/v1/model/settings')
    values={**old['values'],'enabled':True,'provider':name,'base_url':base,'model_id':model,
            'model_ids':[model],'protocol':protocol,'stream':False,'reasoning_effort':'omit',
            'max_output_tokens':cap,'chat_token_parameter':'max_tokens','daily_budget':None}
    made=post(f,'/v1/model/profiles',{'name':name,'expected_revision':old['revision'],'values':values})
    assert made.status_code==201,made.text
    id=f.get('/v1/model/profiles')['active_profile_id']
    result=post(f,'/v1/model/credential',{'key':'SYNTHETIC-'+name,'expected_revision':made.json()['revision']})
    assert result.status_code==200,result.text
    return id


def selected(f, selections, **extra):
    with f.w.db.read() as tx:m=f.w.market.latest(scope=tx).to_dict()
    return post(f,'/v1/analysis/daily/groups',{'models':[s['model_key'] for s in selections],
        'selections':selections,'package_kind':'current','kind':'new','base_group_id':None,'bundle_id':None,
        'market_snapshot_id':m['snapshot_id'],'horizon_minutes':[30,60,180],**extra})


def test_same_model_two_suppliers_uses_saved_protocol_caps_and_separate_scores(flow,monkeypatch):
    official=supplier(flow,'SYNTHETIC-official','https://api.deepseek.com/v1','deepseek-v4-pro','responses',32000)
    relay=supplier(flow,'SYNTHETIC-relay','https://synthetic.invalid/v1','deepseek-v4-pro',cap=30000)
    active=flow.get('/v1/model/profiles')['active_profile_id']
    choices=[{'model_key':'A','profile_id':official,'model_id':'deepseek-v4-pro'},
             {'model_key':'B','profile_id':relay,'model_id':'deepseek-v4-pro'}]
    response=selected(flow,choices);assert response.status_code==201,response.text
    g=response.json();assert g['same_question']
    requests={}
    for arm in g['arms']:
        with flow.w.db.read() as tx:requests[arm['model_key']]=flow.w.analysis.model_request(arm['request_id'],scope=tx)['request']
    sent=[]
    def receive(incoming):
        body=json.loads(incoming.content);sent.append((str(incoming.url),body,incoming.headers['authorization']))
        key='A' if str(incoming.url).startswith('https://api.deepseek.com/') else 'B'
        text=json.dumps(answer_for(requests[key],key),ensure_ascii=False)
        result=({'model':'deepseek-v4-pro','status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':text}]}]}
          if key=='A' else {'model':'deepseek-v4-pro','choices':[{'finish_reason':'stop','message':{'content':text}}]})
        return httpx.Response(200,json=result)
    flow.w.model_api.gateway=Gateway(httpx.MockTransport(receive))
    fake_real_group(flow,monkeypatch)
    assert run(flow,g['group_id'],mode='real').status_code==202
    execute(flow,2)
    result=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    assert all(a['state']=='succeeded' for a in result['arms']),result
    assert [a['max_output_tokens'] for a in result['arms']]==[32000,30000]
    assert {a['protocol'] for a in result['arms']}=={'responses','chat_completions'}
    a=next(x for x in sent if x[0].endswith('/responses'));b=next(x for x in sent if x[0].endswith('/chat/completions'))
    assert a[1]['max_output_tokens']==32000 and 'reasoning' not in a[1]
    assert b[1]['max_tokens']==30000 and 'max_completion_tokens' not in b[1] and 'reasoning_effort' not in b[1]
    assert a[2]=='Bearer SYNTHETIC-SYNTHETIC-official' and b[2]=='Bearer SYNTHETIC-SYNTHETIC-relay'
    assert flow.get('/v1/model/profiles')['active_profile_id']==active
    score_saved(flow,g)
    rows=stats(flow)['rows'];assert len(rows)==2
    assert {r['profile_id'] for r in rows}=={official,relay}
    assert {r['model_id'] for r in rows}=={'deepseek-v4-pro'}
    assert all('credential' not in r['configuration'] and 'SYNTHETIC-SYNTHETIC' not in json.dumps(r) for r in rows)
    # Renaming / changing a supplier later cannot rewrite the frozen result.
    config=flow.get('/v1/model/settings')
    with flow.w.db.write() as tx:
        flow.w.model_api.settings.update({'expected_revision':config['revision'],'values':{**config['values'],'provider':'SYNTHETIC-renamed','max_output_tokens':29000}},uow=tx)
    assert {r['provider'] for r in stats(flow)['rows']}=={'SYNTHETIC-official','SYNTHETIC-relay'}


def test_unknown_or_duplicate_models_reject_before_calls_or_key_reads(flow,monkeypatch):
    id=supplier(flow,'SYNTHETIC-provider','https://synthetic.invalid/v1','wire-pro')
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('prepare must not read a key'))
    s={'model_key':'A','profile_id':id,'model_id':'not-saved'}
    r=selected(flow,[s]);assert r.status_code==409 and r.json()['code']=='DAILY_MODEL_NOT_SAVED'
    s['model_id']='wire-pro'
    r=selected(flow,[s,{**s,'model_key':'B'}]);assert r.status_code==409 and r.json()['code']=='DAILY_SELECTION_INVALID'
    assert not flow.sent


def test_selected_channel_changes_require_new_round_for_updates(flow,monkeypatch):
    id=supplier(flow,'SYNTHETIC-provider','https://synthetic.invalid/v1','gpt-6.1-sol')
    s={'model_key':'A','profile_id':id,'model_id':'gpt-6.1-sol'}
    g=selected(flow,[s]).json();assert run(flow,g['group_id']).status_code==202;execute(flow,1)
    other=supplier(flow,'SYNTHETIC-other','https://other.invalid/v1','gpt-6.1-sol')
    flow.refresh()
    r=selected(flow,[{**s,'profile_id':other}],kind='update',base_group_id=g['group_id'],horizon_minutes=None)
    assert r.status_code==409 and r.json()['code']=='DAILY_UPDATE_CHANNEL_CHANGED'


def test_chat_token_selection_is_explicit_and_never_sends_both(flow):
    values=flow.w.model_api.settings.snapshot()['values']
    values.update(protocol='chat_completions',max_output_tokens=32000)
    default=json.loads(request_body(values,'SYNTHETIC',{}))
    assert default['max_completion_tokens']==32000 and 'max_tokens' not in default
    values['chat_token_parameter']='max_tokens'
    alternate=json.loads(request_body(values,'SYNTHETIC',{}))
    assert alternate['max_tokens']==32000 and 'max_completion_tokens' not in alternate


@pytest.mark.parametrize('failure,code',[('cutoff','MODEL_OUTPUT_LIMIT'),('schema','ANSWER_SCHEMA_INVALID'),('model','DAILY_RETURNED_MODEL_MISMATCH')])
def test_relay_failure_preserves_raw_and_never_switches_or_resends(flow,monkeypatch,failure,code):
    id=supplier(flow,'SYNTHETIC-relay','https://relay.invalid/v1','deepseek-v4-pro')
    g=selected(flow,[{'model_key':'D','profile_id':id,'model_id':'deepseek-v4-pro'}]).json()
    with flow.w.db.read() as tx:q=flow.w.analysis.model_request(g['arms'][0]['request_id'],scope=tx)['request']
    answer=answer_for(q,'D')
    if failure=='schema':answer['horizons'][0]['direction']='invented-direction'
    sent=[]
    def receive(request):
        sent.append(str(request.url))
        return httpx.Response(200,json={'model':'different-model' if failure=='model' else 'deepseek-v4-pro',
            'choices':[{'finish_reason':'length' if failure=='cutoff' else 'stop','message':{'content':json.dumps(answer)}}]})
    flow.w.model_api.gateway=Gateway(httpx.MockTransport(receive));fake_real_group(flow,monkeypatch)
    assert run(flow,g['group_id'],mode='real').status_code==202
    execute(flow,1)
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    assert arm['state']=='failed' and not arm['version_id'] and arm['model_call']['raw_response']
    if failure=='schema':assert arm['model_call']['error_code'] in {'ANSWER_SCHEMA_INVALID','SCHEMA_INVALID','ANSWER_INVALID','ANSWER_VALIDATION_FAILED','VALIDATION_ERROR'}
    else:assert arm['model_call']['error_code']==code
    assert not flow.w.model_api.process_once(flow.w,kind='daily_model_call')
    assert run(flow,g['group_id'],mode='real').status_code==409
    assert sent==['https://relay.invalid/v1/chat/completions']
