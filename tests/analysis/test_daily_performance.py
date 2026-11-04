"""Score conservation and model/template separation, all SYNTHETIC."""
from copy import deepcopy
from datetime import timedelta
import pytest
from tests.analysis.test_daily_analysis import flow as flow,current_format as current_format,prepare,run,execute,post
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.platform.model_gateway.lab_simulation import simulation_actual,answer_for
from xau_next.domains.review.public import compute_prediction
from xau_next.shared.values import parse_utc
from tests.guidance.test_workflow import TOKEN

def score_saved(f,group,*,missing_ticks=False):
    f.clock.value=(parse_utc(group['market_cutoff'])+timedelta(minutes=183)).isoformat().replace('+00:00','Z')
    for arm in f.get('/v1/analysis/daily/groups/'+group['group_id'])['arms']:
        if not arm['version_id']:continue
        with f.w.db.read() as tx:
            v=f.w.analysis.get_version(arm['version_id'],scope=tx).to_dict()
            q=f.w.analysis.model_request(arm['request_id'],scope=tx,require_pending=False)['request']
            basis=f.w.analysis.review_basis(v['version_id'],scope=tx).to_dict()
        actual=simulation_actual(q,as_of=f.clock.value)
        if missing_ticks:actual['ticks']=[];actual['tick_status']='unavailable'
        for minutes in [30,60,180]:
            result=compute_prediction(v,actual,horizon_minutes=minutes,as_of=f.clock.value,review_basis=basis)
            with f.w.db.write() as tx:f.w.review.commit_review(result,uow=tx)

def stats(f,extra=''):
    return f.get('/v1/analysis/daily/performance?data_kind=SYNTHETIC&minutes=30'+extra)

def test_all_four_models_scored_without_turning_uncertain_into_accuracy(flow):
    g=prepare(flow);run(flow,g['group_id']);execute(flow,4);score_saved(flow,g)
    result=stats(flow);rows={r['model_id']:r for r in result['rows']}
    assert len(rows)==4
    assert rows['gpt-6.1-sol']['correct']==rows['deepseek-v4-pro']['correct']==1
    assert rows['gpt-6-astra']['incorrect']==1
    assert rows['deepseek-flash']['uncertain']==1 and rows['deepseek-flash']['scored']==0
    assert all(r['records']==r['ended']==1 and r['range_mae'] is not None for r in rows.values())
    assert all(r['range_scored']==1 and r['range_covered']==1 for r in rows.values())
    assert flow.get('/v1/analysis/daily/performance?minutes=30')['rows']==[]  # Synthetic never mixed into REAL.
    assert len(stats(flow,'&paired_only=true')['rows'])==4

def test_single_model_and_different_templates_remain_separate(flow):
    a=prepare(flow,'A');run(flow,a['group_id']);execute(flow,1);score_saved(flow,a)
    flow.refresh();b=prepare(flow,'A','improved');run(flow,b['group_id']);execute(flow,1);score_saved(flow,b)
    rows=stats(flow)['rows'];assert {r['template_version'] for r in rows}=={'manual-analysis/14','manual-analysis/23'}
    assert len(stats(flow,'&template=manual-analysis%2F14')['rows'])==1
    assert not stats(flow,'&paired_only=true')['rows']

def test_missing_endpoints_keep_direction_unknown_even_with_range(flow):
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1);score_saved(flow,g,missing_ticks=True)
    row=stats(flow)['rows'][0]
    assert row['records']==row['ended']==row['unknown']==1 and row['scored']==row['correct']==0
    assert row['range_scored']==1

def test_unfilled_and_failed_branches_stay_in_registered_counts(flow):
    g=prepare(flow,'AB');run(flow,g['group_id'])
    group=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    with flow.w.db.write() as tx:flow.w.model_api.cancel(group['arms'][0]['model_call']['job_id'],uow=tx)
    execute(flow,1);score_saved(flow,g)
    rows={r['model_id']:r for r in stats(flow)['rows']}
    assert rows['gpt-6.1-sol']['records']==rows['gpt-6.1-sol']['failed']==1
    assert rows['gpt-6.1-sol']['scored']==0

def test_manual_answer_without_model_identity_is_not_credited_to_requested_model(flow):
    g=prepare(flow,'A');arm=g['arms'][0]
    with flow.w.db.read() as tx:q=flow.w.analysis.model_request(arm['request_id'],scope=tx)['request']
    import json
    raw=json.dumps(answer_for(q,'A'),ensure_ascii=False)
    prepared=flow.w.analysis.prepare_import({'request_id':arm['request_id'],'raw_text':raw,'source':'manual_paste',
      'provider':None,'model_id':None,'reasoning_effort':'unknown','analysis_platform':None})
    with flow.w.db.write() as tx:flow.w.commit_answer(prepared,uow=tx)
    score_saved(flow,g)
    rows=stats(flow)['rows'];assert len(rows)==1 and rows[0]['model_id']=='unknown'
    assert rows[0]['correct']==1

def test_date_and_horizon_filters_do_not_invent_samples(flow):
    g=prepare(flow,'A');assert stats(flow,'&from_date=2099-01-01')['rows']==[]
    assert flow.get('/v1/analysis/daily/performance?data_kind=SYNTHETIC&minutes=720')['records_considered']==0
    assert flow.client.get('/v1/analysis/daily/performance?paired_only=maybe',headers={'Authorization':'Bearer '+TOKEN}).status_code==422


def test_different_frozen_generation_limits_are_not_merged(flow,monkeypatch):
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1);score_saved(flow,g)
    flow.refresh();original=flow.w.model_api.settings.snapshot
    def changed(*args,**kw):
        value=deepcopy(original(*args,**kw));value['values']['max_output_tokens']=24000;return value
    monkeypatch.setattr(flow.w.model_api.settings,'snapshot',changed)
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1);score_saved(flow,g)
    rows=stats(flow)['rows'];assert len(rows)==2
    import json
    assert {json.loads(r['configuration'])['max_output_tokens'] for r in rows}=={12000,24000}
    assert all('credential' not in r['configuration'] and 'SYNTHETIC-daily' not in r['configuration'] for r in rows)


def history(f,extra=''):
    return stats(f,'&include_history=true'+extra)


def test_history_partition_preserves_scores_and_does_not_evaluate_or_send(flow,monkeypatch):
    g=prepare(flow);run(flow,g['group_id']);execute(flow,4);score_saved(flow,g)
    before=stats(flow)
    def forbidden(*a,**kw):raise AssertionError('read-only history must not write, generate, or read credentials')
    monkeypatch.setattr(flow.w.review,'commit_review',forbidden)
    monkeypatch.setattr(flow.w.model_api.settings,'snapshot',forbidden)
    result=history(flow)
    assert result['history']['saved_requests']==4 and result['history']['total_rounds']==1
    assert result['history']['evaluation_items']==4 and result['history']['issue_records']==0
    assert result['rows']==before['rows']
    for row in result['rows']:
        assert sum(row['display_counts'].values())==row['records']==1
    items=result['history']['items'][0]['items']
    assert {i['state'] for i in items}=={'correct','incorrect','uncertain'}
    assert all(i['actual_low'] is not None and i['review_id'] for i in items)
    assert all('raw_text' not in i and 'credential' not in str(i) for i in items)


def test_history_failed_unknown_overlap_is_not_double_counted(flow):
    g=prepare(flow,'A');run(flow,g['group_id'])
    arm=flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]
    with flow.w.db.write() as tx:flow.w.model_api.cancel(arm['model_call']['job_id'],uow=tx)
    score_saved(flow,g)
    result=history(flow);row=result['rows'][0]
    assert row['failed']==row['unknown']==1  # Original overlapping fields stay intact.
    assert row['display_counts']['cancelled']==1 and row['display_counts']['unknown']==0
    assert sum(row['display_counts'].values())==1
    assert result['history']['issue_records']==1


def test_history_pending_and_not_sent_are_not_failures(flow):
    g=prepare(flow,'AB');a=history(flow)
    assert all(i['state']=='not_sent' for i in a['history']['items'][0]['items'])
    assert a['history']['issue_records']==0
    run(flow,g['group_id']);execute(flow,2)
    b=history(flow)
    assert all(i['state']=='pending' for i in b['history']['items'][0]['items'])
    assert b['history']['issue_records']==0


def test_history_missing_direction_keeps_available_range_and_reasons(flow):
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1);score_saved(flow,g,missing_ticks=True)
    result=history(flow,'&history_view=issues');item=result['history']['items'][0]['items'][0]
    assert item['state']=='unknown' and item['review_id'] and item['actual_low'] is not None
    assert result['rows'][0]['range_scored']==1 and result['rows'][0]['correct']==0


def test_history_pages_filter_before_paging_and_keep_global_totals(flow):
    for template in ['current','improved','current']:
        g=prepare(flow,'A',template);run(flow,g['group_id']);execute(flow,1);score_saved(flow,g);flow.refresh()
    first=history(flow,'&history_limit=1&template=manual-analysis%2F14')
    assert first['history']['total_rounds']==2 and first['records_considered']==2
    second=history(flow,'&history_limit=1&template=manual-analysis%2F14&history_cursor='+first['history']['next_cursor'])
    assert first['history']['items'][0]['round_key']!=second['history']['items'][0]['round_key']
    assert second['history']['next_cursor'] is None and second['history']['total_rounds']==2
    assert all(r['template_version']=='manual-analysis/14' for r in second['rows'])
    assert history(flow,'&provider=DOES_NOT_EXIST')['records_considered']==0
    assert history(flow,'&method=DOES_NOT_EXIST')['history']['evaluation_items']==0


def test_history_drill_down_and_invalid_cursor_are_explicit(flow):
    g=prepare(flow);run(flow,g['group_id']);execute(flow,4);score_saved(flow,g)
    rows=history(flow)['rows'];row=next(r for r in rows if r['correct'])
    selected=history(flow,'&history_row='+row['row_id']+'&history_status=correct')
    assert sum(len(g['items']) for g in selected['history']['items'])==1
    assert len(selected['rows'])==4  # A list drill-down must not silently change score scope.
    response=flow.client.get('/v1/analysis/daily/performance?include_history=true&data_kind=SYNTHETIC&history_cursor=missing',headers={'Authorization':'Bearer '+TOKEN})
    assert response.status_code==422 and response.json()['code']=='HISTORY_CURSOR_INVALID'


def test_history_multiple_horizons_are_items_not_extra_calls(flow):
    g=prepare(flow,'A');run(flow,g['group_id']);execute(flow,1);score_saved(flow,g)
    result=flow.get('/v1/analysis/daily/performance?data_kind=SYNTHETIC&include_history=true')
    assert result['records_considered']==result['history']['saved_requests']==1
    assert result['history']['evaluation_items']==3
    assert result['history']['answer_versions']==1
