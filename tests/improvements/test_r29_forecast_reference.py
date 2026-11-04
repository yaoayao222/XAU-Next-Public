"""SYNTHETIC deterministic references and mocked HTTP; no real model or MT5."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from tests.guidance.test_workflow import Flow
from tests.model_api.test_workflow import ModelFlow
from xau_next.domains.analysis.forecast_reference import build_forecast_reference, project_forecast_reference
from xau_next.domains.analysis.package import build_packet, frozen_answer_schema
from xau_next.domains.analysis.public import outbound_packet
from xau_next.domains.analysis.quality import forecast_evidence


def stamp(value):return value.isoformat().replace('+00:00','Z')
def facts(packet):return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]


def sample():
    cutoff=datetime(2026,10,21,12,tzinfo=timezone.utc)
    status={'state':'available','captured_at':stamp(cutoff),'observed_at':stamp(cutoff),'reasons':[]}
    market={'snapshot_id':'SYNTHETIC-reference','source_kind':'synthetic','market_cutoff':stamp(cutoff),
        'captured_at':stamp(cutoff),'quote':{'bid':'102.50','ask':'102.60','event_time':stamp(cutoff)},
        'quote_status':deepcopy(status),'series':[]}
    for timeframe,period,count in [('M1',1,1440),('M15',15,240),('H1',60,240)]:
        bars=[]
        for i in range(count):
            base=Decimal('100')+Decimal(i%10)/10;opened=cutoff-timedelta(minutes=period*(count-i))
            bars.append({'open_at':stamp(opened),'close_at':stamp(opened+timedelta(minutes=period)),
                'open':str(base),'high':str(base+1),'low':str(base-1),'close':str(base+Decimal('.2')),'closed':True,'tick_volume':1})
        market['series'].append({'timeframe':timeframe,'price_basis':'bid','status':deepcopy(status),'bars':bars})
    request={'request_id':'SYNTHETIC-request','market_snapshot_id':market['snapshot_id'],
        'market_cutoff':stamp(cutoff),'window_start':stamp(cutoff),'horizon_minutes':[30,60,180]}
    context={'market':market,'settings':{'values':{'quote_max_age_ms':10000}}}
    refresh(request,context)
    return request,context


def refresh(request,context):
    context['forecast_evidence']=forecast_evidence(context['market'],request['horizon_minutes'],window_start=request['window_start'])


def test_all_horizons_are_explicit_bid_anchored_comparisons_without_a_selected_direction():
    request,context=sample();before=deepcopy((request,context));value=build_forecast_reference(request,context)
    assert [h['horizon_minutes'] for h in value['horizons']]==[30,60,180]
    assert all(h['state']=='available' for h in value['horizons'])
    for h in value['horizons']:
        assert Decimal(h['median_extrema_reference']['low'])<=Decimal(value['quote']['bid'])<=Decimal(h['median_extrema_reference']['high'])
        assert h['baseline']['reanchored_no_change']=='102.50'
        assert Decimal(h['baseline']['reanchored_fixed_drift'])==Decimal('102.50')+Decimal(h['baseline']['drift_change'])
        assert not set(h)&{'direction','side','signal','probability','ready','selected_mode'}
    assert (request,context)==before


@pytest.mark.parametrize('defect',['future_quote','stale_quote','future_capture','missing_capture','wrong_identity','future_market','nan','reversed'])
def test_unsafe_quote_and_market_cannot_create_price_references(defect):
    r,c=sample();m=c['market']
    if defect=='future_quote':m['quote']['event_time']='2099-01-01T00:00:00Z'
    if defect=='stale_quote':m['quote']['event_time']='2000-01-01T00:00:00Z'
    if defect=='future_capture':m['quote_status']['captured_at']='2099-01-01T00:00:00Z'
    if defect=='missing_capture':m['quote_status'].pop('captured_at')
    if defect=='wrong_identity':r['market_snapshot_id']='another'
    if defect=='future_market':m['captured_at']='2099-01-01T00:00:00Z'
    if defect=='nan':m['quote']['bid']='NaN'
    if defect=='reversed':m['quote']['ask']='1'
    rows=build_forecast_reference(r,c)['horizons']
    assert all(h['state']=='unknown' and h['median_extrema_reference'] is None and h['baseline'] is None for h in rows)


@pytest.mark.parametrize('defect',['future_end','future_anchor','modified_quantile','duplicate_row','wrong_snapshot','wrong_cutoff','missing_quantiles'])
def test_cached_evidence_is_checked_against_raw_cutoff_safe_data(defect):
    r,c=sample();e=c['forecast_evidence'];h=e['horizons'][0]
    if defect=='future_end':h['end_at']='2099-01-01T00:00:00Z'
    if defect=='future_anchor':h['baseline']['anchor_at']='2099-01-01T00:00:00Z'
    if defect=='modified_quantile':h['quantiles']['low_excursion']['p50']='-0.01'
    if defect=='duplicate_row':e['horizons'].append(deepcopy(h))
    if defect=='wrong_snapshot':e['market_snapshot_id']='another'
    if defect=='wrong_cutoff':e['market_cutoff']='2099-01-01T00:00:00Z'
    if defect=='missing_quantiles':h['quantiles']=None
    first=build_forecast_reference(r,c)['horizons'][0]
    assert first['state']=='unknown' and first['median_extrema_reference'] is None


def test_future_raw_bar_is_not_evidence_and_small_samples_remain_unknown():
    r,c=sample();expected=build_forecast_reference(r,c)
    for series in c['market']['series']:
        series['bars'].append({'open_at':'2099-01-01T00:00:00Z','close_at':'2099-01-01T00:01:00Z',
            'open':'1','high':'99999','low':'1','close':'99999','closed':True})
    assert build_forecast_reference(r,c)==expected
    for series in c['market']['series']:series['bars']=series['bars'][-3:-1]
    refresh(r,c)
    assert all(h['state']=='unknown' and h['history'] is None for h in build_forecast_reference(r,c)['horizons'])


def test_update_retains_exact_original_endpoints_and_exposes_fractional_remaining_time():
    r,c=sample();cutoff=datetime(2026,10,21,12,10,44,tzinfo=timezone.utc)
    r['market_cutoff']=stamp(cutoff);m=c['market'];m['market_cutoff']=m['captured_at']=stamp(cutoff)
    m['quote']['event_time']=m['quote_status']['captured_at']=stamp(cutoff)
    for series in m['series']:series['status']['captured_at']=stamp(cutoff)
    refresh(r,c);first=build_forecast_reference(r,c)['horizons'][0]
    assert first['end_at']=='2026-10-21T12:30:00Z' and first['remaining_seconds']=='1156'
    assert first['state']=='available' and first['history']['remaining_minutes']==19
    r['window_start']='2026-10-21T10:00:00Z';refresh(r,c)
    ended=build_forecast_reference(r,c)['horizons'][0]
    assert ended['state']=='ended' and ended['median_extrema_reference'] is None


def test_nested_projection_keeps_only_declared_fields():
    r,c=sample();value=build_forecast_reference(r,c);expected=deepcopy(value)
    nodes=[value,value['quote']]
    for h in value['horizons']:
        nodes.extend([h,h['history'],h['history']['endpoint_delta'],h['history']['path_width'],h['baseline'],
            h['median_extrema_reference'],h['outer_extrema_reference']])
    for item in nodes:item['unapproved_extension']='SYNTHETIC-private-do-not-send'
    assert project_forecast_reference(value)==expected


def test_new_packet_changes_only_comparison_facts_and_prompt_identity(tmp_path):
    f=Flow(tmp_path)
    try:
        f.configure();request=f.request(f.refresh())
        with f.w.db.read() as scope:stored=f.w.db.documents('analysis').get(scope,'request',request['request_id'])
        assert request['prompt_version']=='manual-analysis/16' and request['answer_schema_version']=='1.7.0'
        new=facts(request['packet_text']);sent=facts(outbound_packet(request['packet_text']))
        assert new['forecast_comparison_context']==sent['forecast_comparison_context']
        old_request=deepcopy(new['request']);old_request['prompt_version']='manual-analysis/15'
        old=facts(build_packet(old_request,stored['context'],frozen_answer_schema(stored)))
        clean=deepcopy(new);clean.pop('forecast_comparison_context');clean['request']['prompt_version']='manual-analysis/15'
        assert clean==old and 'forecast_comparison_context' not in old
        assert '不是当前80%概率区间' in request['packet_text'] and '允许uncertain' in request['packet_text']
    finally:f.close()


@pytest.mark.parametrize('prompt,projection',[('manual-analysis/16','api-projection/9'),('manual-analysis/15','api-projection/8')])
def test_mock_transport_records_exact_version_and_never_regenerates_old_packet(tmp_path,prompt,projection):
    f=ModelFlow(tmp_path)
    try:
        request=f.ready()
        if prompt.endswith('/15'):
            with f.w.db.write() as tx:
                docs=f.w.db.documents('analysis');old=docs.get(tx,'request',request['request_id'])
                packet_request=deepcopy(facts(old['packet_text'])['request']);packet_request['prompt_version']=prompt
                old['prompt_version']=prompt;old['packet_text']=build_packet(packet_request,old['context'],frozen_answer_schema(old))
                docs.update(tx,'request',old['request_id'],old,old['revision'])
            request={**request,'prompt_version':prompt,'packet_text':old['packet_text']}
        before=len(f.sent);started=f.start(request)
        assert f.w.model_api.process_once(f.w)
        record=f.record(started);assert record['state']=='succeeded',record
        packet=facts(json.loads(f.sent[-1].content)['input'])
        assert ('forecast_comparison_context' in packet)==prompt.endswith('/16')
        with f.w.db.read() as tx:saved=f.w.db.documents('analysis').get(tx,'model_call',record['call_id'])
        assert saved['configuration']['outbound_transform_version']==projection
        assert len(f.sent)==before+1 and not f.w.model_api.process_once(f.w) and len(f.sent)==before+1
    finally:f.close()
