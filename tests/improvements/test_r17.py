import copy
import json
from datetime import timedelta

import pytest
from xau_next.domains.plans.advice import rule_check
from xau_next.shared.values import parse_utc
from tests.guidance.test_workflow import Flow
from tests.scalp.test_integration import facts
from tests.scalp.test_request_horizons import command


@pytest.fixture
def flow(tmp_path):
    f = Flow(tmp_path)
    yield f
    f.close()


def test_create_binds_capture_not_later_current_portfolio(flow):
    old = flow.refresh()
    with flow.w.db.read() as tx:
        paired = flow.w.portfolio.get_portfolio(scope=tx).to_dict()
    flow.clock.value = '2026-10-12T02:00:05Z'
    flow.refresh()
    current = flow.get('/v1/portfolio')
    req = flow.post('/v1/analysis/requests', command(old))
    frozen = facts(req['packet_text'])
    assert paired['snapshot_id'] != current['snapshot_id']
    assert frozen['portfolio']['snapshot_id'] == paired['snapshot_id']
    assert frozen['portfolio']['account_status']['state'] == 'available'
    assert frozen['snapshot_alignment']['market_snapshot_id'] == old['snapshot_id']


def test_five_horizons_freeze_quality_and_roles_in_actual_projection(flow):
    req = flow.post('/v1/analysis/requests', command(flow.refresh()))
    frozen = facts(req['packet_text'])
    assert frozen['prediction_policy']['direction_semantics'] == 'endpoint_change'
    assert frozen['prediction_policy']['neutral_band'] == '0.00'
    assert frozen['prediction_policy']['version'] == 'prediction-quality/2'
    assert frozen['prediction_policy']['review_method'] == 'prediction_review/3'
    assert frozen['prediction_policy']['direction_anchor'] == 'request_frozen_bid'
    assert frozen['condition_policy']['version'] == 'condition-observation/1'
    assert [h['horizon_minutes'] for h in frozen['forecast_evidence']['horizons']] == [30,60,180,360,720]
    with flow.w.db.read() as tx:
        outbound, schema, _ = flow.w.analysis.model_packet(req['request_id'], scope=tx)
    projected = facts(outbound)
    for name in ['prediction_policy','condition_policy','forecast_evidence','snapshot_alignment']:
        assert projected[name] == frozen[name]
    assert schema['properties']['schema_version']['const'] == '1.7.0'


def test_background_uses_fresh_existing_close_but_new_event_does_not():
    at = '2026-10-12T02:03:00Z'
    status = {'state':'available','captured_at':at,'observed_at':at,'reasons':[]}
    bars = [{'open_at':'2026-10-12T01:50:00Z','close_at':'2026-10-12T01:55:00Z','closed':True,'close':'100'},
            {'open_at':'2026-10-12T01:55:00Z','close_at':'2026-10-12T02:00:00Z','closed':True,'close':'100'}]
    market = {'market_cutoff':at,'series':[{'timeframe':'M5','price_basis':'bid','status':status,'bars':bars}]}
    rule = {'kind':'bar_close','timeframe':'M5','operator':'lt','price':'101',
            'observation':'background','purpose':'M5 structure still holds'}
    assert rule_check(rule,market,at,30000,after='2026-10-12T02:00:01Z')['matched'] is True
    rule['observation'] = 'new_since_available'
    assert rule_check(rule,market,at,30000,after='2026-10-12T02:00:01Z')['matched'] is False


def test_lifecycle_rejects_future_close_before_cutoff_and_duplicate_event():
    from xau_next.domains.plans.lifecycle import advance
    definition = {'valid_from':'2026-10-12T02:00:01Z','entry_deadline':'2026-10-12T02:30:00Z',
        'activation':{'kind':'bar_close','timeframe':'M1','operator':'gt','price':'101'},
        'invalidation':{'kind':'quote_compare','quote_side':'bid','operator':'lt','price':'90'},
        'post_activation_invalidation':None,'trading_style':'scalp'}
    record = {'definition':definition,'available_at':definition['valid_from'],'lifecycle':'WAITING_TRIGGER',
              'lifecycle_at':definition['valid_from'],'manual_observations':[], 'condition_policy_version':'condition-observation/1'}
    bars = [{'open_at':'2026-10-12T02:00:00Z','close_at':'2026-10-12T02:01:00Z','close':'102','closed':True}]
    series = {'timeframe':'M1','price_basis':'bid','status':{'state':'available','captured_at':'2026-10-12T02:01:01Z','reasons':[]},'bars':bars}
    market = {'market_cutoff':'2026-10-12T02:00:59Z','quote':None,'series':[series]}
    assert advance(record,market,'2026-10-12T02:01:02Z')['lifecycle'] == 'WAITING_TRIGGER'
    market['market_cutoff'] = '2026-10-12T02:01:01Z'
    series['bars'] = bars*2
    assert advance(record,market,'2026-10-12T02:01:02Z')['lifecycle'] == 'WAITING_TRIGGER'


def test_quality_wide_containment_is_not_accuracy_and_missing_is_unknown():
    from xau_next.domains.analysis.quality import range_diagnostics
    narrow = range_diagnostics({'low':'99','high':'103'},'100','102')
    wide = range_diagnostics({'low':'80','high':'120'},'100','102')
    assert narrow['contained'] == wide['contained'] == True
    assert wide['boundary_mae'] > narrow['boundary_mae']
    assert wide['width_ratio'] > narrow['width_ratio']
    assert range_diagnostics({'low':'99','high':'103'},None,None)['state'] == 'unknown'


def test_experience_revisions_and_horizons_are_one_episode():
    from xau_next.domains.analysis.quality import experience_cohort
    exp = {'cases':[{'review_ref':f'r{i}','target_kind':'forecast','target_id':'v1',
        'inputs':{'forecast_version_id':'v1','horizon_minutes':h},
        'window_start':'2026-10-12T02:00:00Z','window_end':'2026-10-12T03:00:00Z'} for i,h in enumerate([30,60,30])]}
    cohort = experience_cohort(exp)
    assert cohort['case_count'] == 3 and cohort['unique_forecast_versions'] == 1
    assert cohort['overlap_components'] == 1 and cohort['independent_sample_count'] is None


@pytest.mark.parametrize('observation,expected', [('background','READY'),('new_since_available','WAITING')])
def test_m1_activation_and_retest_m5_background_or_new_event(flow,monkeypatch,observation,expected):
    from tests.scalp.test_ready_flow import install_market_path,local_candidate,create_and_import
    path = install_market_path(flow,monkeypatch)
    def candidate(answer):
        p=local_candidate(answer)
        p['entry_checks']=[{'kind':'bar_close','timeframe':'M5','operator':'lt','price':'101',
            'observation':observation,'purpose':'M5 local structure below independent upper boundary'}]
    req,raw=create_and_import(flow,candidate)
    path['m1_closes'][parse_utc('2026-10-12T02:01:00Z')]='100.30'
    flow.clock.value='2026-10-12T02:01:02Z'
    flow.refresh()
    ws=flow.get('/v1/workspace')
    assert ws['plans'][0]['lifecycle']=='ACTIVE'
    assert ws['action_advice'][0]['state']==expected, ws['action_advice']
    assert all(ws['evaluations'][0][name]['state']=='satisfied' for name in ['quote_check','cost_check','budget_check','margin_check'])
    assert flow.get('/v1/forecasts/'+req['forecast_id'])['current_version']['raw_text']==raw


def test_post_activation_same_event_waits_for_later_close(flow,monkeypatch):
    from tests.scalp.test_ready_flow import install_market_path,local_candidate,create_and_import
    path=install_market_path(flow,monkeypatch)
    def candidate(answer):
        p=local_candidate(answer)
        p['entry_checks']=[{'kind':'bar_close','timeframe':'M1','operator':'gt','price':'99.50',
            'observation':'new_since_activation','purpose':'Later M1 must retain distinct lower support after retest'}]
    create_and_import(flow,candidate)
    path['m1_closes'][parse_utc('2026-10-12T02:01:00Z')]='100.30'
    flow.clock.value='2026-10-12T02:01:02Z';flow.refresh()
    assert flow.get('/v1/workspace')['action_advice'][0]['state']=='WAITING'
    flow.clock.value='2026-10-12T02:02:02Z';flow.refresh()
    assert flow.get('/v1/workspace')['action_advice'][0]['state']=='READY'


@pytest.mark.parametrize('defect',['duplicate','redundant','contradiction','unverified_background'])
def test_role_defects_reject_without_repair_or_import(flow,defect):
    from tests.scalp.test_ready_flow import local_candidate
    from xau_next.domains.analysis.validation import validate_answer
    from xau_next.shared.errors import DomainError
    flow.configure();req=flow.request(flow.refresh());answer=flow.answer(req)
    p=local_candidate(answer)
    if defect=='duplicate':
        p['entry_checks']*=2;code='ENTRY_CHECK_DUPLICATE'
    elif defect=='redundant':
        p['entry_checks']=[{**p['activation'],'observation':'new_since_available','purpose':'duplicate same event'}];code='ENTRY_CHECK_REDUNDANT'
    elif defect=='contradiction':
        p['entry_checks']=[{**p['invalidation'],'observation':'new_since_available','purpose':'impossible support'}];code='RULE_CONTRADICTION'
    else:
        p['entry_checks']=[{'kind':'bar_close','timeframe':'M5','operator':'gt','price':'101',
            'observation':'background','purpose':'not established'}];code='ENTRY_BACKGROUND_UNVERIFIED'
    original=copy.deepcopy(answer)
    with flow.w.db.read() as scope:
        row=flow.w.analysis._docs.get(scope,'request',req['request_id'])
    with pytest.raises(DomainError) as error:
        validate_answer(answer,flow.app.state.contracts.answer,row,row['context'])
    assert error.value.code==code
    assert answer==original
    assert flow.get('/v1/workspace')['current_forecast'] is None


def test_future_account_in_same_batch_stays_unknown(flow,monkeypatch):
    original=flow.w.market._bridge.request
    def source(operation,params):
        value=original(operation,params)
        if operation=='snapshot':
            portfolio=value['result']['portfolio']
            portfolio['account_status']['observed_at']='2026-10-12T02:00:05Z'
            portfolio['orders_status']['observed_at']='2026-10-12T02:00:05Z'
        return value
    monkeypatch.setattr(flow.w.market._bridge,'request',source)
    market=flow.refresh();req=flow.post('/v1/analysis/requests',command(market))
    frozen=facts(req['packet_text'])
    assert frozen['portfolio']['account'] is None
    assert frozen['portfolio']['account_status']['state']=='unavailable'
    assert frozen['portfolio']['orders_status']['state']=='unavailable'
    with flow.w.db.read() as scope:
        original_portfolio=flow.w.portfolio.get_snapshot(frozen['portfolio']['snapshot_id'],scope=scope).to_dict()
    assert original_portfolio['account']['balance'] is not None
    assert original_portfolio['account_status']['observed_at']=='2026-10-12T02:00:05Z'


def test_concurrent_refresh_cannot_change_selected_capture(flow,monkeypatch):
    import threading
    market=flow.refresh()
    original=flow.w.market.get_snapshot
    read_done,resume=threading.Event(),threading.Event()
    def read(snapshot_id,scope=None):
        result=original(snapshot_id,scope)
        if snapshot_id==market['snapshot_id'] and not read_done.is_set():
            read_done.set();assert resume.wait(10)
        return result
    monkeypatch.setattr(flow.w.market,'get_snapshot',read)
    results=[]
    thread=threading.Thread(target=lambda:results.append(flow.post('/v1/analysis/requests',command(market))),name='freeze-request')
    thread.start();assert read_done.wait(10)
    try:
        flow.clock.value='2026-10-12T02:00:05Z';flow.refresh()
    finally:
        resume.set();thread.join(10)
    assert not thread.is_alive() and len(results)==1
    frozen=facts(results[0]['packet_text'])
    assert frozen['portfolio']['account_status']['observed_at']==market['market_cutoff']
    assert frozen['snapshot_alignment']['binding']=='same_capture_evidence'


def test_new_diagnostics_preserve_provider_body_and_separate_local_phases(tmp_path):
    from tests.model_api.test_workflow import ModelFlow
    f=ModelFlow(tmp_path)
    try:
        req=f.ready();started=f.start(req)
        f.w.model_api.process_once(f.w)
        record=f.record(started)
        timing=record['local_timing']
        assert record['state']=='succeeded', record
        assert timing['version']=='model-local/1'
        assert all(timing[name] is not None and timing[name]>=0 for name in ['input_build_ms','extraction_ms','validation_ms','import_ms'])
        assert timing['input_bytes']==len(f.sent[-1].content)
        assert json.loads(f.sent[-1].content)['max_output_tokens']==record['max_output_tokens']
        assert 'key' not in json.dumps(timing)
    finally:f.close()


def test_experience_rows_roundtrip_preserves_absence_null_zero_and_order():
    from xau_next.domains.analysis.experience import compact_experience,expand_experience
    original={'cases':[{'review_ref':str(i),'metrics':[{'name':'direction_correct','state':'available','value':'0','unit':'label','reason':None} for _ in range(20)],
        'original_prediction':{'summary':'中文原文'},'note':None} for i in range(8)]}
    original['cases'][3].pop('note')
    compact=compact_experience(original)
    assert expand_experience(compact)==original
    assert len(json.dumps(compact))<len(json.dumps(original))


def test_specialized_frozen_schema_keeps_authoritative_new_constraints():
    from pathlib import Path
    from xau_next.domains.analysis.package import effective_frozen_schema
    schema=json.loads(Path('contracts/analysis-answer.schema.json').read_text(encoding='utf8'))
    frozen=effective_frozen_schema(schema,'1.6.0')
    assert frozen['properties']['schema_version']['const']=='1.6.0'
    assert 'forecast_basis' in frozen['$defs']['Horizon']['required']
    assert 'holding_time_basis' in frozen['$defs']['Plan']['required']
    assert frozen['$defs']['Plan']['properties']['entry_checks']['items']=={'$ref':'#/$defs/ObservedEntryCheck'}
    assert schema['properties']['schema_version']['enum'][-2:]==['1.6.0','1.7.0']
