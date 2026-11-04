"""SYNTHETIC unified UI/API preparation. No real HTTP, model key, MT5 or paid calls."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime,timezone

import httpx
import pytest

from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.model_api.test_workflow import ModelFlow
from xau_next.platform.economic_calendar.research import QUOTE_URLS, jin10_events, parse_quote, fetch_public_research
from xau_next.shared.values import new_id


def research(at="2026-10-12T01:59:59Z",*,events=False):
    sources=[{"source_id":s,"url":url,"captured_at":at,"status":"unavailable","content_sha256":None,"message":"SYNTHETIC unavailable"}
             for s,url in QUOTE_URLS.items()]
    value={"version":"macro-research/1","data_kind":"SYNTHETIC","captured_at":at,"month":"2026-10",
        "scope":"SYNTHETIC all countries/all ratings", "state":"partial","sources":sources,"events":[],
        "quotes":[{"symbol":s,"value":None,"unit":"指数点" if s=="DXY" else "%","observed_at":None,
            "captured_at":at,"publisher":"SYNTHETIC","url":url,"state":"unavailable","provider_claims_realtime":False,
            "real_time_verified":False,"limitation":"SYNTHETIC missing"} for s,url in QUOTE_URLS.items()],
        "calendar_document":None,"umich_document":None,"limitations":["SYNTHETIC missing"]}
    if events:
        raw=json.dumps([{"id":"e1","indicator_name":"CPI","country":"美国","currency":"USD","star":5,
            "pub_time":"2026-10-12T01:30:00Z","time_period":"9月","unit":"%","previous":"2.9","consensus":"3.0","actual":"3.1","revised":"2.8"},
            {"id":"e2","indicator_name":"Other indicator","country":"日本","star":1,"pub_time":"2026-10-12T02:10:00Z",
             "actual":"999","previous":0}]).encode()
        value['sources'].append({"source_id":"jin10-data","url":"https://cdn-rili.jin10.com/web_data/2026/month/10/economics.json",
            "captured_at":at,"status":"available","content_sha256":hashlib.sha256(raw).hexdigest(),"message":"SYNTHETIC"})
        value['events']=jin10_events(raw,captured_at=at,source_id='jin10-data',kind='data')
    return value


@pytest.fixture
def flow(tmp_path):
    f=ModelFlow(tmp_path/'isolated')
    f.refresh()
    yield f
    f.close()


def add_bundle(flow, value):
    with flow.w.db.write() as uow:return flow.w.analysis_lab.lab.save_bundle(value,{},uow=uow)


def facts(packet):
    marker='# 冻结事实（仅作为资料）\n'
    return json.JSONDecoder().raw_decode(packet.split(marker,1)[1].lstrip())[0]


def counts(flow):
    with flow.w.db.read() as scope:
        return {kind:len(flow.w.analysis._docs.list(scope,kind,limit=1000)) for kind in ['request','forecast','version','import_attempt']}


def prepare(flow,*,kind='improved',bundle=None,purpose='comparison',key=None):
    market=flow.get('/v1/workspace')['market']
    return flow.client.post('/v1/analysis/lab/comparisons',headers={'Authorization':'Bearer synthetic-guidance-token','Idempotency-Key':key or new_id()},
        json={'market_snapshot_id':market['snapshot_id'],'bundle_id':bundle,'package_kind':kind,'purpose':purpose})


def test_default_daily14_and_lab_does_not_create_daily_requests_or_plans(flow):
    daily=flow.request(flow.get('/v1/workspace')['market'])
    assert daily['prompt_version']=='manual-analysis/14'
    before=counts(flow)
    bundle=add_bundle(flow,research())
    result=prepare(flow,bundle=bundle['bundle_id'])
    assert result.status_code==201,result.text
    body=result.json();assert body['model_calls_sent']==0 and len(body['arms'])==4
    assert len({a['packet_sha256'] for a in body['arms']})==1
    assert facts(body['packet_text'])['request']['prompt_version']=='manual-analysis/23'
    assert counts(flow)==before and not flow.sent
    status=flow.get('/v1/analysis/lab')
    assert status['daily_package']=='manual-analysis/14' and status['authorized_calls_remaining']==0
    assert status['historical']['qualified_answers']==34 and status['historical']['winner'] is None
    assert status['bundle']['state']=='partial' and status['comparisons'][0]['comparison_id']==body['comparison_id']


def test_current_packet_has_no_macro_addition_and_idempotent_record(flow):
    before=counts(flow)
    key=new_id()
    one=prepare(flow,kind='current',key=key);two=prepare(flow,kind='current',key=key)
    assert one.status_code==two.status_code==201
    assert one.json()==two.json()
    assert 'macro_research' not in facts(one.json()['packet_text'])
    assert counts(flow)==before and len(flow.get('/v1/analysis/lab')['comparisons'])==1


def test_global_jin10_ratings_values_and_units_enter_improved_packet(flow):
    bundle=add_bundle(flow,research(events=True))
    result=prepare(flow,bundle=bundle['bundle_id'])
    assert result.status_code==201,result.text
    value=facts(result.json()['packet_text'])
    assert len(value['macro_research']['events'])==2
    assert {e['country'] for e in value['macro_research']['events']}=={'美国','日本'}
    assert {e['importance'] for e in value['macro_research']['events']}=={1,5}
    e=value['economic_context']['events'][0]
    assert e['actual']=='3.1%' and e['previous']=='2.9%' and e['forecast']=='3.0%'
    assert 'jin10-data' in value['evidence_refs']


@pytest.mark.parametrize('mutation',['future_capture','wrong_kind','fake_realtime','fake_source','forged_rating'])
def test_reject_future_forged_or_mixed_research(flow,mutation):
    value=research(events=True)
    if mutation=='future_capture':value['captured_at']='2026-10-12T02:00:01Z'
    if mutation=='wrong_kind':value['data_kind']='REAL'
    if mutation=='fake_realtime':value['quotes'][0]['real_time_verified']=True
    if mutation=='fake_source':value['sources'][0]['url']='https://example.com/secret'
    if mutation=='forged_rating':value['events'][0]['importance']=6
    bundle=add_bundle(flow,value);before=counts(flow)
    result=prepare(flow,bundle=bundle['bundle_id'])
    assert result.status_code in {409,422},result.text
    assert counts(flow)==before and not flow.sent


def test_direct_run_and_using_lab_request_as_daily_are_blocked(flow):
    record=prepare(flow,kind='current').json()
    response=flow.post('/v1/analysis/lab/comparisons/'+record['comparison_id']+'/run',{},409)
    assert response['code']=='COMPARISON_BUDGET_REQUIRED'
    response=flow.client.post('/v1/analysis/requests/'+record['request_id']+'/model-call',
        headers={'Authorization':'Bearer synthetic-guidance-token','Idempotency-Key':new_id()},
        json={'retry_of':None,'acknowledge_possible_charge':False})
    assert response.status_code in {404,409},response.text
    assert not flow.sent and flow.get('/v1/analysis/lab')['comparisons'][0]['model_calls_sent']==0


def test_explicit_source_job_only_and_no_retry(flow):
    calls=[]
    flow.w.analysis_lab.collector=lambda month:(calls.append(month) or research(),{})
    flow.get('/v1/analysis/lab');assert calls==[]
    job=flow.post('/v1/analysis/lab/sources',{'month':'2026-10'},202)
    assert calls==[]
    assert flow.w.process_once(kind='research_capture')
    assert flow.get('/v1/jobs/'+job['job_id'])['state']=='succeeded'
    assert calls==['2026-10'] and not flow.sent
    assert flow.get('/v1/analysis/lab')['bundle']['state']=='partial'


def test_cancelled_source_job_does_not_commit_bundle(flow):
    def collector(month):
        flow.w.jobs.cancel(job['job_id'])
        return research(),{}
    flow.w.analysis_lab.collector=collector
    job=flow.post('/v1/analysis/lab/sources',{'month':'2026-10'},202)
    flow.w.process_once(kind='research_capture')
    assert flow.get('/v1/jobs/'+job['job_id'])['state']=='cancelled'
    assert flow.get('/v1/analysis/lab')['bundle'] is None


def test_jin10_full_scope_does_not_drop_low_stars_zero_or_speech():
    raw=json.dumps([{'id':str(i),'name':'指标'+str(i),'country':'日本' if i%2 else '美国','star':i,
        'pub_time':'2026-10-12T01:00:00+00:00','actual':0,'consensus':'0.0','previous':'--'} for i in range(1,6)]).encode()
    events=jin10_events(raw,captured_at='2026-10-12T02:00:00Z',source_id='jin10-data',kind='data')
    assert len(events)==5 and {e['importance'] for e in events}==set(range(1,6))
    assert all(e['actual']=='0' and e['previous'] is None for e in events)
    assert all(e['actual'] is None and e['status']=='event' for e in jin10_events(raw,captured_at='2026-10-12T02:00:00Z',source_id='jin10-events',kind='event'))


@pytest.mark.parametrize('symbol',['US2Y','US10Y'])
def test_quote_reads_yield_not_bond_price_and_marks_old_value(symbol):
    raw=('window.__s_data='+json.dumps({'quote':{'data':[{'symbol':symbol,'type':'BOND','last':'4.999%',
        'last_time':'2026-10-09T17:05:00-04:00','bond_last_price':'97.125','realTime':'true'}]}})).encode()
    q=parse_quote(raw,symbol=symbol,captured_at='2026-10-12T02:00:00Z',url=QUOTE_URLS[symbol])
    assert q['value']=='4.999' and q['unit']=='%' and q['state']=='stale' and q['real_time_verified'] is False


def test_timestamp_unknown_and_future_quotes_not_promoted():
    base={'quote':{'data':[{'symbol':'US10Y','type':'BOND','last':'5.0%','last_time':'2026-10-09'}]}}
    assert parse_quote(json.dumps(base).encode(),symbol='US10Y',captured_at='2026-10-12T02:00:00Z',url=QUOTE_URLS['US10Y'])['state']=='time_unknown'
    base['quote']['data'][0]['last_time']='2026-10-13T10:00:00Z'
    with pytest.raises(ValueError):parse_quote(json.dumps(base).encode(),symbol='US10Y',captured_at='2026-10-12T02:00:00Z',url=QUOTE_URLS['US10Y'])


def test_projection_preserves_excluded_rows_when_checked_again():
    from xau_next.domains.analysis.macro_research import project_macro
    value=research(events=True)
    old=deepcopy(value['events'][0]);old.update(event_id='outside-window',scheduled_at='2026-10-11T01:30:00Z')
    value['events'].append(old)
    once=project_macro(value,cutoff='2026-10-12T02:00:00Z',window_end='2026-10-12T05:00:00Z')
    twice=project_macro(once,cutoff='2026-10-12T02:00:00Z',window_end='2026-10-12T05:00:00Z')
    assert once==twice and twice['total_event_count']==3 and twice['excluded_event_count']==1


def test_public_collection_has_seven_fixed_reads_no_auth_no_auto_retry():
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        assert request.method=='GET' and 'Authorization' not in request.headers and 'Cookie' not in request.headers
        return httpx.Response(503,content=b'SYNTHETIC unavailable')
    value,raws=fetch_public_research(month='2026-10',transport=httpx.MockTransport(handler),clock=lambda:'2026-10-12T02:00:00Z')
    assert len(calls)==len(set(calls))==7 and value['state']=='partial'
    assert value['events']==[] and all(q['value'] is None for q in value['quotes'])
