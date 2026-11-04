"""Optional24 uses ordinary strict persistence and synthetic I/O only."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, post, run, execute
from tests.analysis.test_analysis_lab import add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.shared.values import parse_utc
from xau_next.domains.analysis.public import outbound_packet, build_packet, frozen_answer_schema


def facts(text):
    return json.JSONDecoder().raw_decode(text.split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]


def prepare24(f, models='ABCD', kind='new', base=None):
    bundle=add_bundle(f,multi())
    with f.w.db.read() as scope:market=f.w.market.latest(scope=scope).to_dict()
    result=post(f,'/v1/analysis/daily/groups',{'package_kind':'candidate24','models':list(models),
        'kind':kind,'base_group_id':base,'bundle_id':bundle['bundle_id'],
        'market_snapshot_id':market['snapshot_id'],'horizon_minutes':[30,60,180] if kind=='new' else None})
    assert result.status_code==201,result.text
    return result.json()


@pytest.mark.parametrize('models',['A','ABCD'])
def test_prepare_projects_and_saves24_without_any_credentials_or_send(flow,monkeypatch,models):
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Preparation must not read a model key'))
    group=prepare24(flow,models)
    assert group['template_version']=='manual-analysis/24' and group['package_kind']=='candidate24'
    assert group['same_question'] and not group['run_registered']
    with flow.w.db.read() as tx:
        packets=[flow.w.analysis.model_request(a['request_id'],scope=tx)['request'] for a in group['arms']]
    assert all(q['context']==packets[0]['context'] for q in packets)
    for q in packets:
        raw=facts(q['packet_text']);api=facts(outbound_packet(q['packet_text']))
        assert raw['forecast_deliberation']==api['forecast_deliberation']
        assert api['request']['prompt_version']=='manual-analysis/24'
        assert api['economic_context'] and api['macro_research']
        assert all(h['end_at'] for h in api['forecast_deliberation']['horizons'])
    assert not flow.sent


def test24_simulated_answer_import_updates_original_endpoints_and_history(flow):
    g=prepare24(flow,'AB');assert run(flow,g['group_id']).status_code==202;execute(flow,2)
    result=flow.get('/v1/analysis/daily/groups/'+g['group_id'])
    assert all(a['state']=='succeeded' for a in result['arms'])
    for arm in result['arms']:
        assert arm['model_call']['outbound_transform_version']=='api-projection/24-deliberation1'
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(minutes=1)).isoformat().replace('+00:00','Z');flow.refresh()
    update=prepare24(flow,'AB',kind='update',base=g['group_id'])
    assert update['window_end']==g['window_end'] and not update['same_question']
    assert [a['forecast_id'] for a in update['arms']]==[a['forecast_id'] for a in g['arms']]
    assert run(flow,update['group_id']).status_code==202;execute(flow,2)
    saved=flow.get('/v1/analysis/daily/groups/'+update['group_id'])
    assert all(a['state']=='succeeded' for a in saved['arms'])
    history=flow.get('/v1/analysis/daily/groups')
    assert any(x['template_version']=='manual-analysis/24' for x in history['items'])
    assert not flow.sent


def test_manual_copy_uses_same_strict_import_and_rejects_invalid_answer(flow):
    g=prepare24(flow,'A')
    q=flow.get('/v1/analysis/requests/'+g['arms'][0]['request_id'])
    with flow.w.db.read() as scope:frozen=flow.w.analysis.model_request(q['request_id'],scope=scope)['request']
    # Invalid identity cannot become a successful ordinary version.
    from xau_next.platform.model_gateway.lab_simulation import answer_for
    answer=answer_for(frozen,'A');answer['request_id']='wrong-identity'
    command={'request_id':q['request_id'],'raw_text':json.dumps(answer),'source':'manual_paste',
             'provider':'SYNTHETIC','model_id':'SYNTHETIC-manual','reasoning_effort':'unknown','analysis_platform':'SYNTHETIC'}
    response=post(flow,'/v1/analysis/imports',command)
    assert response.status_code==202,response.text
    flow.w.process_once(kind='answer_import')
    job=flow.get('/v1/jobs/'+response.json()['job_id'])
    assert job['state']=='failed' and job['error']['code']=='REQUEST_MISMATCH',job
    answer['request_id']=q['request_id'];command['raw_text']=json.dumps(answer)
    response=post(flow,'/v1/analysis/imports',command);assert response.status_code==202,response.text
    flow.w.process_once(kind='answer_import')
    assert flow.get('/v1/jobs/'+response.json()['job_id'])['state']=='succeeded'
    assert flow.get('/v1/analysis/daily/groups/'+g['group_id'])['arms'][0]['state']=='succeeded'
    assert not flow.sent


def test24_nested_projection_drops_private_extensions():
    from tests.improvements.test_r29_forecast_reference import sample
    from xau_next.domains.analysis.forecast_candidate import build_deliberation, project_deliberation
    r,c=sample();table=build_deliberation(r,c);expected=deepcopy(table)
    nodes=[table,table['quote'],table['evidence_paths']]
    for row in table['horizons']:
        nodes.extend([row,row['history'],row['history']['endpoint_delta'],row['history']['path_width'],*row['references']])
    for node in nodes:node['private_extension']='DO_NOT_SEND'
    assert project_deliberation(table)==expected
