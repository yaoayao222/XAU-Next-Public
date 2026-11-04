"""Opt-in27, strict duplicates, and legacy26 preservation. SYNTHETIC only."""
from copy import deepcopy
import json
import pytest
from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, post, run, execute
from tests.analysis.test_json_document import prepare26, facts
from tests.analysis.test_analysis_lab import add_bundle
from tests.analysis.test_multi_source_research import multi
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.public import build_packet, frozen_answer_schema, outbound_packet
from xau_next.domains.analysis.member_once_candidate import INSTRUCTIONS, project_plan
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from datetime import timedelta


def prepare27(f, models='D', kind='new', base=None):
    bundle = add_bundle(f, multi())
    with f.w.db.read() as tx:market = f.w.market.latest(scope=tx).to_dict()
    response = post(f, '/v1/analysis/daily/groups', {'package_kind': 'candidate27', 'models': list(models), 'kind': kind,
        'base_group_id': base, 'bundle_id': bundle['bundle_id'], 'market_snapshot_id': market['snapshot_id'],
        'horizon_minutes': [30,60,180] if kind == 'new' else None})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize('models', ['D', 'ABCD'])
def test27_only_adds_once_plan_and_all_selected_models_have_same_facts(flow, monkeypatch, models):
    monkeypatch.setattr(flow.credentials, 'read', lambda *_: pytest.fail('Key read'))
    group = prepare27(flow, models)
    assert group['template_version'] == 'manual-analysis/27' and not group['run_registered']
    shapes = []
    for arm in group['arms']:
        with flow.w.db.read() as tx:q = flow.w.analysis.model_request(arm['request_id'], scope=tx)['request']
        before = deepcopy(q);manual = facts(q['packet_text']);outbound = facts(outbound_packet(q['packet_text']))
        plan = manual['json_member_once'];shapes.append(plan)
        assert plan == outbound['json_member_once']
        assert plan['root_write_order'].count('analysis_basis') == 1
        assert len(plan['root_write_order']) == len(set(plan['root_write_order']))
        assert plan['max_occurrences_per_object'] == 1 and plan['append_completed_member'] is False
        assert '不要把这份清单' in q['packet_text']
        base = {k:v for k,v in q.items() if k not in {'context','packet_text','id','revision','recorded_at'}}
        base['prompt_version'] = 'manual-analysis/26'
        old = build_packet(base,q['context'],frozen_answer_schema(q))
        for new, control in [(manual,facts(old)),(outbound,facts(outbound_packet(old)))]:
            new = deepcopy(new);new.pop('json_member_once');new['request']['prompt_version'] = 'manual-analysis/26'
            assert new == control
        assert q == before
        private = {**deepcopy(plan), 'private_debug':'DO_NOT_SEND'}
        assert project_plan(private,manual['json_document_shape']) == plan
        private['root_write_order'].append('analysis_basis')
        with pytest.raises(DomainError):project_plan(private,manual['json_document_shape'])
    assert all(p == shapes[0] for p in shapes) and not flow.sent


def test27_simulation_and_update_keep_identity_and_original_endpoints(flow):
    group = prepare27(flow,'AD');assert run(flow,group['group_id']).status_code == 202;execute(flow,2)
    saved = flow.get('/v1/analysis/daily/groups/'+group['group_id'])
    assert all(a['state']=='succeeded' and a['model_call']['outbound_transform_version']=='api-projection/27-member1' for a in saved['arms'])
    flow.clock.value=(parse_utc(flow.clock.value)+timedelta(minutes=1)).isoformat().replace('+00:00','Z');flow.refresh()
    update=prepare27(flow,'AD','update',group['group_id'])
    assert update['horizon_minutes']==group['horizon_minutes'] and update['window_end']==group['window_end']
    assert run(flow,update['group_id']).status_code==202;execute(flow,2)
    assert all(a['state']=='succeeded' for a in flow.get('/v1/analysis/daily/groups/'+update['group_id'])['arms'])
    assert not flow.sent


def test_failed26_duplicate_is_display_only_and_never_reimported(flow,monkeypatch):
    group=prepare26(flow,'D');run(flow,group['group_id'])
    raw=b'{\n"analysis_basis":{"x":1},\n"analysis_basis":{"x":1}\n}'
    with flow.w.db.write() as tx:
        call=flow.w.model_api.calls.latest(group['arms'][0]['request_id'],scope=tx)
        ref=flow.w.model_api.calls.register(flow.w.model_api.calls.prepare(raw),uow=tx)
        flow.w.model_api.calls.update(call['call_id'],{'state':'failed','answer':ref,'transport_succeeded':True,'answer_valid':False,'error_code':'SCHEMA_INVALID','detail':'Original stored generic error'},uow=tx)
    with flow.w.db.read() as tx:before=deepcopy(flow.w.model_api.calls.get(call['call_id'],scope=tx,internal=True))
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Key read'))
    arm=flow.get('/v1/analysis/daily/groups/'+group['group_id'])['arms'][0]
    assert arm['state']=='failed' and arm['version_id'] is None
    d=arm['duplicate_field_diagnostics'];assert d['items'][0]['values_relation']=='identical'
    assert d['items'][0]['first']['line']==2 and d['items'][0]['duplicate']['line']==3
    assert '两处内容相同' in arm['model_call']['detail']
    with flow.w.db.read() as tx:
        assert flow.w.model_api.calls.get(call['call_id'],scope=tx,internal=True)==before
        assert flow.w.model_api.calls.read(ref,scope=tx)==raw
    assert not flow.sent
