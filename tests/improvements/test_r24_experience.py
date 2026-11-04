"""SYNTHETIC full request→projection→manual import; never contacts a model."""
from copy import deepcopy
from datetime import timedelta
import json

from tests.guidance.test_workflow import Flow
from xau_next.domains.analysis.public import outbound_packet, expand_experience
from xau_next.shared.values import parse_utc


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]


def no_candidates(answer):
    answer['plans']=[]
    for value in answer['candidate_assessments']:
        value.update(outcome='no_candidate',plan_key=None,rationale='SYNTHETIC：仅检查经验反馈。',missing_conditions=['不生成交易方案。'])


def test_new_manual_request_keeps_statistics_selection_projection_and_originals(tmp_path):
    f=Flow(tmp_path)
    try:
        old, original=f.initial(mutate=no_candidates)
        f.clock.value='2026-10-12T14:04:00Z'
        job=f.post('/v1/reviews/refresh',{'forecast_id':old['forecast_id'],'through':f.clock()},202)
        assert f.w.process_once(kind='review_refresh')
        assert f.get('/v1/jobs/'+job['job_id'])['state']=='succeeded'
        market=f.refresh()
        request=f.post('/v1/analysis/requests',{'kind':'new','forecast_id':None,'base_version_id':None,'market_snapshot_id':market['snapshot_id']})
        context=facts(request['packet_text']);experience=context['experience']
        assert request['prompt_version']=='manual-analysis/16'
        assert request['horizon_minutes']==[30,60,180,360,720]
        assert experience['schema_version']=='experience/2' and '_case_candidates' not in experience
        assert experience['forecast_statistics']['record_total']==3
        assert experience['forecast_statistics']['scope']=='same_source_as_current_request'
        assert experience['case_selection']['requested_horizons']==request['horizon_minutes']
        assert experience['case_selection']['covered_horizons']==[180,360,720]
        assert 0<len(experience['cases'])<=8
        assert {c['review_ref'] for c in experience['cases']}<=set(context['evidence_refs'])
        for row in experience['forecast_statistics']['rows']:
            for name in ('direction','range_containment'):
                c=row[name];assert sum(c[k] for k in ('correct','incorrect','unknown','not_predicted'))==row['total']
        sent=facts(outbound_packet(request['packet_text']))
        assert expand_experience(sent['experience'])==experience
        assert 'prediction_policy' in sent and 'condition_policy' in sent and 'near_term' in sent

        answer=f.answer(request);no_candidates(answer)
        sample=deepcopy(answer['horizons'][0])
        start=parse_utc(request['market_cutoff']);answer['horizons']=[]
        for minutes in request['horizon_minutes']:
            end=(start+timedelta(minutes=minutes)).isoformat().replace('+00:00','Z')
            value={**deepcopy(sample),'horizon_minutes':minutes,'end_at':end,'direction':'uncertain',
                   'phases':[{'start_at':request['market_cutoff'],'end_at':end,'direction':'uncertain','note':'SYNTHETIC：仅校验结构。'}],
                   'turning_points':[],'confidence':None}
            answer['horizons'].append(value)
        f.clock.value='2026-10-12T14:04:01Z'
        imported, raw=f.submit(request,answer)
        assert imported['state']=='succeeded',imported
        assert f.get('/v1/forecasts/'+request['forecast_id'])['current_version']['raw_text']==raw
        assert f.get('/v1/analysis/requests/'+old['request_id'])['packet_text']==old['packet_text']
        assert f.get('/v1/forecasts/'+old['forecast_id'])['current_version']['raw_text']==original
    finally:f.close()


def test_selected_old_reference_is_kept_within_eighty_and_same_source_statistics():
    from xau_next.domains.review.public import ReviewService
    from xau_next.shared.snapshots import Snapshot
    from tests.review.test_experience_selection import candidate,signature,CUTOFF
    values=[candidate('recent'+str(i),360) for i in range(85)]+[candidate('old-short',30,'0')]
    cases=[v[0] for v in values];metadata={v[0]['inputs']['forecast_version_id']:v[1] for v in values}
    # This service path only enriches detached values; no database is used.
    service=ReviewService.__new__(ReviewService)
    context={'cutoff':CUTOFF,'review_reference_total':86,'review_refs':[c['review_ref'] for c in cases[:80]],
             'omitted_reference_count':6,'cases':[],'_case_candidates':cases,'notes':[]}
    original=deepcopy(context)
    result=service.enrich_context(Snapshot.of(context),metadata,selection_target={
        'market_signature':signature(),'horizon_minutes':[30,60],'review_method':'prediction_review/3'}).to_dict()
    assert len(result['review_refs'])==80 and result['omitted_reference_count']==6
    assert 'old-short-r00000001' in result['review_refs']
    assert result['forecast_statistics']['record_total']==86
    assert context==original and '_case_candidates' not in result


def test_automatic_preparation_and_update_inherited_horizons(tmp_path):
    f=Flow(tmp_path)
    try:
        old,_=f.initial(mutate=no_candidates)
        with f.w.db.read() as scope:
            target=f.w.analysis.experience_target({'kind':'update','forecast_id':old['forecast_id']},
                facts(old['packet_text'])['market'],{'values':{'daily_horizons_minutes':[180]}},scope=scope)
        assert target['horizon_minutes']==[180,360,720]
        f.clock.value='2026-10-12T14:04:00Z'
        f.post('/v1/reviews/refresh',{'forecast_id':old['forecast_id'],'through':f.clock()},202)
        assert f.w.process_once(kind='review_refresh')
        f.refresh();f.w.guidance.tick()
        requests=f.get('/v1/analysis/requests?state=awaiting_answer')['items']
        assert requests
        for row in requests:
            pending=f.get('/v1/analysis/requests/'+row['request_id'])
            experience=facts(pending['packet_text'])['experience']
            assert experience['case_selection']['requested_horizons']==pending['horizon_minutes']
            assert experience['forecast_statistics']['record_total']==3
            assert '_case_candidates' not in experience
    finally:f.close()
