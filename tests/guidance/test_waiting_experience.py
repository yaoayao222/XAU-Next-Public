"""Waiting, preparation reminders and hard stops are independent, using synthetic facts."""
import copy

import pytest
from xau_next.domains.analysis.refresh import refresh_status
from xau_next.domains.plans.advice import entry_advice
from xau_next.shared.entry_timing import entry_timing, planned_timing

from tests.guidance.test_advice import case
from tests.guidance.test_workflow import Flow


def test_due_is_preparation_only_and_current_eligible_plan_can_still_be_ready():
    values = case()
    values[-1]["state"] = "DUE"
    assert entry_advice(*values, now="2026-10-12T02:07:00Z")["state"] == "READY"


def test_real_terminal_fact_is_not_hidden_by_temporary_reassessment_data_gap():
    values = case()
    values[-1].update(state="WAITING_DATA", reasons=["合成缺柱"])
    values[0]["definition"]["entry_deadline"] = "2026-10-12T02:06:00Z"
    result = entry_advice(*values, now="2026-10-12T02:07:00Z")
    assert result["state"] == "EXPIRED"
    assert result["stopping_reason"]["code"] == "ENTRY_DEADLINE"
    assert result["stopping_reason"]["occurred_at"] == "2026-10-12T02:06:00Z"


def test_analysis_expiry_has_specific_deadline_and_next_step_instead_of_generic_reanalysis():
    values = case()
    values[-1].update(state="STALE", fresh_until="2026-10-12T02:06:00Z")
    result = entry_advice(*values, now="2026-10-12T02:07:00Z")
    assert result["stopping_reason"]["code"] == "ANALYSIS_EXPIRED"
    assert result["stopping_reason"]["deadline"] == "2026-10-12T02:06:00Z"
    assert "分析时效" in result["title"]


def test_request_age_and_answer_delay_are_separate_and_both_reduce_actual_window():
    request = {"market_cutoff":"2026-10-12T02:00:00Z", "created_at":"2026-10-12T02:04:00Z", "window_end":"2026-10-12T14:00:00Z"}
    plan = {"trading_style":"scalp", "valid_from":request["market_cutoff"], "entry_deadline":"2026-10-12T02:30:00Z",
        "entry":{"mode":"limit_retest"},"activation":{"kind":"bar_close","timeframe":"M5"},"entry_checks":[]}
    result = entry_timing(plan,request,"2026-10-12T02:11:00Z")
    assert result["cutoff_to_request_seconds"] == 240
    assert result["request_to_available_seconds"] == 420
    assert result["import_delay_seconds"] == 660 and result["remaining_at_import_seconds"] == 1140
    planned = planned_timing(plan,request,{"timing_reference":{"policy_version":"entry-observation/2","planning_delay_seconds":420}})
    assert planned["available_at"] == "2026-10-12T02:11:00Z"


@pytest.mark.parametrize("delay,state,remaining", [(1,"SUFFICIENT",1799),(437,"SUFFICIENT",1363),
                                                  (1200,"SUFFICIENT",600),(1201,"INSUFFICIENT",599),(1799,"INSUFFICIENT",1),(1800,"INSUFFICIENT",0)])
def test_actual_window_boundaries_without_restarting_analysis_clock(delay,state,remaining):
    from datetime import timedelta

    from xau_next.shared.values import parse_utc
    request={"market_cutoff":"2026-10-12T02:00:00Z","created_at":"2026-10-12T02:00:00Z","window_end":"2026-10-12T14:00:00Z"}
    plan={"trading_style":"scalp","valid_from":request["market_cutoff"],"entry_deadline":"2026-10-12T02:30:00Z",
          "entry":{"mode":"limit_retest"},"activation":{"kind":"bar_close","timeframe":"M5"},"entry_checks":[],"max_holding_minutes":20}
    original=copy.deepcopy(plan)
    available=(parse_utc(request['market_cutoff'])+timedelta(seconds=delay)).isoformat().replace('+00:00','Z')
    result=entry_timing(plan,request,available)
    assert result["state"]==state and result["remaining_at_import_seconds"]==remaining
    assert result["analysis_fresh_until"]=="2026-10-12T02:30:00Z" and plan==original


def test_due_refresh_exposes_that_it_does_not_block_entry():
    request={"market_cutoff":"2026-10-12T02:00:00Z","created_at":"2026-10-12T02:00:05Z","window_end":"2026-10-12T14:00:00Z"}
    version={"version_id":"v","forecast_id":"f","available_at":"2026-10-12T02:07:17Z",
             "origin":{"import_latency_seconds":432},"answer":{"schema_version":"1.3.0"}}
    item=refresh_status(version,request,{"values":{}},now="2026-10-12T02:23:00Z")[0]
    assert item["state"] == "DUE" and item["blocks_entry"] is False
    earlier=refresh_status(version,request,{"values":{}},now="2026-10-12T02:13:00Z",
                           candidate_deadlines={"scalp":"2026-10-12T02:20:00Z"})[0]
    assert earlier['state']=='DUE' and earlier['blocks_entry'] is False
    assert earlier['remind_at']=='2026-10-12T02:12:43Z'
    assert earlier['fresh_until']=='2026-10-12T02:30:00Z'


def test_full_m5_wait_breakout_retest_checks_and_due_do_not_cancel_or_repeat_requests(tmp_path, monkeypatch):
    f=Flow(tmp_path)
    quote={"bid":"99.98","ask":"100.00"}
    latest_close="101.20"  # A matching close BEFORE import cannot activate the new candidate.
    original=f.w.market._bridge.request
    def source(operation,params):
        result=original(operation,params)
        if operation=="snapshot":
            market=result['result']['market']; market['quote'].update(quote)
            bars=next(s for s in market['series'] if s['timeframe']=='M5')['bars']
            bar=max((b for b in bars if b['closed']),key=lambda b:b['close_at'])
            bar.update(close=latest_close,high=max(bar['high'],latest_close,key=float),low=min(bar['low'],latest_close,key=float))
        return result
    monkeypatch.setattr(f.w.market._bridge,'request',source)
    def check(at,state):
        f.clock.value=at; f.refresh(); f.w.guidance.tick()
        ws=f.get('/v1/workspace')
        assert ws['action_advice'][0]['state']==state,ws['action_advice']
        return ws
    try:
        f.configure(); req=f.request(f.refresh()); a=f.answer(req)
        p=a['plans'][0]
        p.update(activation={'kind':'bar_close','timeframe':'M5','operator':'gt','price':'101'},
                 post_activation_invalidation={'kind':'quote_compare','quote_side':'bid','operator':'lt','price':'99.50'},
                 entry_checks=[{'kind':'quote_compare','quote_side':'ask','operator':'lte','price':'99.99'}])
        f.clock.value='2026-10-12T02:07:17Z'; job,raw=f.submit(req,a); assert job['state']=='succeeded',job
        ws=check('2026-10-12T02:07:18Z','WAITING')
        assert ws['plans'][0]['lifecycle']=='WAITING_TRIGGER'
        latest_close='101.20'; quote.update(bid='101.20',ask='101.22')
        ws=check('2026-10-12T02:10:01Z','WAITING')
        assert ws['plans'][0]['lifecycle']=='ACTIVE'
        latest_close='100.50'; quote.update(bid='100.50',ask='100.52')
        ws=check('2026-10-12T02:20:01Z','WAITING')
        assert not ws['notifications']
        assert f.get('/v1/analysis/requests?state=awaiting_answer')['items']==[]
        ws=check('2026-10-12T02:22:44Z','WAITING')
        refresh=next(r for r in ws['analysis_refresh'] if r['trading_style']=='scalp')
        assert refresh['state']=='DUE' and refresh['blocks_entry'] is False
        pending=refresh['pending_request_id']; assert pending
        assert len([n for n in ws['notifications'] if n['kind']=='refresh'])==1
        assert '可准备' in next(n for n in ws['notifications'] if n['kind']=='refresh')['title']
        quote.update(bid='99.98',ask='100.00')
        ws=check('2026-10-12T02:23:01Z','WAITING')  # Zone reached, one entry check still false.
        quote.update(bid='99.97',ask='99.99')
        ws=check('2026-10-12T02:23:30Z','READY')
        for at in ['2026-10-12T02:24:00Z','2026-10-12T02:24:05Z']:
            ws=check(at,'READY')
        assert len([n for n in ws['notifications'] if n['kind']=='refresh'])==1
        assert len([n for n in ws['notifications'] if n['kind']=='entry'])==1
        assert len(f.get('/v1/analysis/requests?state=awaiting_answer')['items'])==1
        assert next(r for r in ws['analysis_refresh'] if r['trading_style']=='scalp')['pending_request_id']==pending
        assert f.get('/v1/forecasts/'+req['forecast_id'])['current_version']['raw_text']==raw
        ws=check('2026-10-12T02:30:00Z','EXPIRED')
        assert ws['action_advice'][0]['stopping_reason']['code']=='ENTRY_DEADLINE'
        assert len([n for n in ws['notifications'] if n['kind']=='refresh'])==2  # Preparation plus one true stop.
        for _ in range(3): f.w.guidance.tick()
        assert len([n for n in f.get('/v1/workspace')['notifications'] if n['kind']=='refresh'])==2
    finally:
        f.close()


def test_genuine_reassessment_stays_confirmed_after_price_returns_and_restart(tmp_path, monkeypatch):
    f=Flow(tmp_path); quote={'bid':'99.98','ask':'100.00'}
    def connect(current):
        original=current.w.market._bridge.request
        def source(operation,params):
            result=original(operation,params)
            if operation=='snapshot': result['result']['market']['quote'].update(quote)
            return result
        monkeypatch.setattr(current.w.market._bridge,'request',source)
    try:
        connect(f)
        def condition(a):
            a['analysis_basis']['reassessment_conditions']=[{'trading_style':'scalp','condition':
                {'kind':'quote_compare','quote_side':'bid','operator':'lt','price':'99.50'}}]
        req,raw=f.initial(mutate=condition)
        quote.update(bid='99.40',ask='99.42'); f.clock.value='2026-10-12T02:00:03Z'; f.refresh(); f.w.guidance.tick()
        ws=f.get('/v1/workspace'); reason=ws['action_advice'][0]['stopping_reason']
        assert reason['code']=='REASSESSMENT_CONDITION' and reason['condition']['price']=='99.50'
        assert reason['occurred_at']=='2026-10-12T02:00:03Z'
        quote.update(bid='99.98',ask='100.00'); f.clock.value='2026-10-12T02:00:04Z'; f.refresh(); f.w.guidance.tick()
        assert f.get('/v1/workspace')['action_advice'][0]['state']=='REFRESH_REQUIRED'
        saved_clock,sink=f.clock,f.sink; f.close(); f=Flow(tmp_path,saved_clock,sink); connect(f)
        f.clock.value='2026-10-12T02:00:05Z'; f.refresh(); f.w.guidance.tick()
        ws=f.get('/v1/workspace')
        assert ws['action_advice'][0]['stopping_reason']==reason
        assert len([n for n in ws['notifications'] if n['kind']=='refresh'])==1
        assert f.get('/v1/forecasts/'+req['forecast_id'])['current_version']['raw_text']==raw
    finally:
        f.close()


def test_timing_reference_uses_recent_generation_delay_not_cutoff_age():
    from xau_next.shared.entry_timing import timing_reference
    samples=[{'version_id':'v1','created_at':'2026-10-12T02:04:00Z','market_cutoff':'2026-10-12T02:00:00Z','available_at':'2026-10-12T02:11:00Z'},
             {'version_id':'v2','created_at':'2026-10-12T01:00:00Z','market_cutoff':'2026-10-12T00:59:00Z','available_at':'2026-10-12T01:08:00Z'}]
    result=timing_reference(samples)
    assert result['sample_count']==2 and result['planning_delay_seconds']==480


def test_new_candidates_cannot_define_normal_trigger_as_reassessment():
    from xau_next.domains.analysis.validation import validate_answer
    from xau_next.shared.errors import DomainError

    from tests.guidance.test_staged_candidates import new_case
    answer,schema,request,context=new_case()
    request['prompt_version']='manual-analysis/7'
    p=answer['plans'][0]
    answer['analysis_basis']['reassessment_conditions']=[{'trading_style':p['trading_style'],'condition':copy.deepcopy(p['activation'])}]
    with pytest.raises(DomainError) as error: validate_answer(answer,schema,request,context)
    assert error.value.code=='REASSESSMENT_CONTRADICTION'
    request['prompt_version']='manual-analysis/6'
    validate_answer(answer,schema,request,context)  # Do not reinterpret a frozen earlier request.


def test_legacy_refresh_notification_is_not_resent_on_upgrade(tmp_path):
    f=Flow(tmp_path)
    try:
        f.initial()
        with f.w.db.read() as scope:
            _guidance,context=f.w.guidance.snapshot(scope)
        version=context['version']['version_id']
        with f.w.db.write() as uow:
            f.w.guidance.notifications.enqueue(uow,f'refresh:{version}:scalp',context['scope_key'],
                {'kind':'refresh','resource_id':version,'title':'合成旧提醒','body':'原提醒保留',
                 'context':{'version_id':version,'trading_style':'scalp'}},expires_at='2026-10-12T03:00:00Z')
        for at in ['2026-10-12T02:29:00Z','2026-10-12T02:30:00Z']:
            f.clock.value=at; f.refresh(); f.w.guidance.tick()
        notices=[n for n in f.get('/v1/workspace')['notifications'] if n['kind']=='refresh']
        assert len(notices)==1 and notices[0]['title']=='合成旧提醒'
    finally:
        f.close()


def test_linked_position_keeps_original_exit_rules_after_analysis_expiry(tmp_path):
    from xau_next.shared.values import new_id

    from tests.guidance.test_workflow import associate, captured_order
    from tests.portfolio.test_portfolio import deal, envelope
    f=Flow(tmp_path)
    try:
        _req,_raw=f.initial()
        plan_id=f.get('/v1/workspace')['plans'][0]['plan_id']
        f.clock.value='2026-10-12T02:27:00Z'; f.refresh()
        identity=captured_order(f); associate(f,identity,plan_id)
        original_order=f.w.portfolio.get_portfolio().to_dict()['orders'][0]
        pf=f.w.portfolio.get_portfolio().to_dict(); pf['snapshot_id']=new_id()
        opening=envelope([deal('synthetic-open','in','0.01','100','0','-0.025',event_time=f.clock.value)],
                         account=identity['account_scope_id'],broker_id=identity['broker_id'])
        opening.update(opened_at=f.clock.value,opening_complete=True,source_kind='synthetic')
        f.w.portfolio.sync_facts(pf,trades=(opening,))
        for at,expected in [('2026-10-12T02:31:00Z','HOLD'),('2026-10-12T02:48:00Z','EXIT')]:
            f.clock.value=at; f.refresh()
            pf=f.w.portfolio.get_portfolio().to_dict()
            pf['snapshot_id']=new_id()
            pf['orders']=[{**original_order,'captured_at':at}]
            f.w.portfolio.sync_facts(pf)
            ws=f.get('/v1/workspace')
            assert next(r for r in ws['analysis_refresh'] if r['trading_style']=='scalp')['state']=='STALE'
            assert ws['position_advice'][0]['state']==expected,ws['position_advice']
            if expected=='EXIT': assert ws['position_advice'][0]['reason_code']=='HOLDING_LIMIT'
    finally:
        f.close()
