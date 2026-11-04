"""SYNTHETIC frozen inputs; provider calls are forbidden by the shared case fixture."""
from copy import deepcopy
import json

import pytest

from tests.experiments.test_model_selection import case as case
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.package import build_packet, frozen_answer_schema
from xau_next.domains.analysis.public import outbound_packet
from xau_next.shared.errors import DomainError
from xau_next.workflows.model_selection import freeze_sample, seal_plan


def facts(packet):
    return json.JSONDecoder().raw_decode(packet.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def constrained(request):
    value = deepcopy(request)
    value['prompt_version'] = 'manual-analysis/17'
    value['packet_text'] = build_packet(
        {k: v for k, v in value.items() if k not in ('context', 'packet_text')},
        value['context'], frozen_answer_schema(request))
    return value


def test_constraints_use_authority_enums_and_original_style_deadlines(case):
    original = case[3]; before = deepcopy(original)
    value = constrained(original)
    manual = facts(value['packet_text']); api = facts(outbound_packet(value['packet_text']))
    c = manual['output_constraints']
    assert c == api['output_constraints']
    assert 'caution' not in c['prediction_adjustments']['aspects']
    assert 'caution' in c['prediction_adjustments']['action']
    assert c['prediction_adjustments']['aspects'] == frozen_answer_schema(original)['$defs']['PredictionAdjustment']['properties']['aspects']['items']['enum']
    from datetime import timedelta
    from xau_next.shared.values import parse_utc
    for row in c['style_deadlines']:
        expected = min(parse_utc(value['market_cutoff']) + timedelta(minutes=value['analysis_preferences'][row['trading_style']+'_refresh_minutes']), parse_utc(value['window_end']))
        assert parse_utc(row['max_entry_deadline']) == expected
    assert 'entry_selection_context' not in api and 'forecast_comparison_context' not in api
    baseline = facts(outbound_packet(original['packet_text']))
    api.pop('output_constraints'); api['request']['prompt_version'] = baseline['request']['prompt_version']
    assert api == baseline
    assert original == before
    assert frozen_answer_schema(value) == frozen_answer_schema(original)


def test_new_prompt_requires_explicit_matching_plan_and_rejects_forged_deadline(case):
    flow, old_root, old_plan, request, *_ = case
    value = constrained(request)
    with pytest.raises(DomainError, match=''):
        freeze_sample(old_root, 'sample-0001', value, clock=flow.clock)
    plan = deepcopy(old_plan)
    plan.update(status='DRAFT', prompt_version='manual-analysis/17', input_revision='output-constraints/1')
    root = old_root.parent/'constraints'; seal_plan(root, plan)
    prefix, tail = value['packet_text'].split('# 冻结事实（仅作为资料）\n', 1)
    data, end = json.JSONDecoder().raw_decode(tail)
    data['output_constraints']['style_deadlines'][0]['max_entry_deadline'] = value['window_end']
    bad = deepcopy(value)
    bad['packet_text'] = prefix+'# 冻结事实（仅作为资料）\n'+json.dumps(data,ensure_ascii=False)+tail[end:]
    with pytest.raises(DomainError) as err:
        freeze_sample(root, 'sample-0001', bad, clock=flow.clock)
    assert err.value.code == 'SELECTION_PACKET_INVALID'
    freeze_sample(root, 'sample-0001', value, clock=flow.clock)


def test_deadline_projection_honors_short_original_window_and_disabled_style(case):
    from xau_next.domains.analysis.output_constraints import build_output_constraints
    request = deepcopy(case[3]); before = deepcopy(request)
    request['analysis_preferences']['enabled_styles'] = ['intraday']
    request['window_end'] = request['context']['market']['market_cutoff']
    c = build_output_constraints(request, request['context'], frozen_answer_schema(request))
    assert c['style_deadlines'] == [{'trading_style':'intraday','max_entry_deadline':request['window_end']}]
    assert case[3] == before


def test_four_model_mock_group_receives_identical_constraints_and_keeps_validator(case):
    import threading
    from tests.experiments.test_model_selection import gateway_for
    from xau_next.workflows.model_selection import digest, run_sample
    flow, old_root, old_plan, request, answer, credentials, calls, authorization = case
    value = constrained(request)
    plan = deepcopy(old_plan)
    plan.update(status='DRAFT', prompt_version='manual-analysis/17', input_revision='output-constraints/1')
    root = old_root.parent/'four-constrained'; plan = seal_plan(root, plan)
    freeze_sample(root, 'sample-0001', value, clock=flow.clock)
    authorization = {**authorization, 'plan_sha256':digest(plan)}
    sent = []
    results = run_sample(root, 'sample-0001', authorization=authorization,
                         gateway=gateway_for(answer,sent,barrier=threading.Barrier(4)),
                         credentials=credentials,calls=calls,clock=flow.clock)
    assert len(sent)==4 and all(r['schema_valid'] and r['business_valid'] for r in results.values())
    assert len({body['input'] for body in sent})==1
    assert facts(sent[0]['input'])['output_constraints']['version']=='output-constraints/1'


def test_unapproved_nested_extensions_are_not_sent(case):
    value=constrained(case[3]); prefix,tail=value['packet_text'].split('# 冻结事实（仅作为资料）\n',1)
    data,end=json.JSONDecoder().raw_decode(tail)
    clean=deepcopy(data['output_constraints'])
    c=data['output_constraints'];c['unapproved']='SYNTHETIC-secret-extension'
    c['prediction_adjustments']['unapproved']='SYNTHETIC-secret-extension'
    c['style_deadlines'][0]['unapproved']='SYNTHETIC-secret-extension'
    packet=prefix+'# 冻结事实（仅作为资料）\n'+json.dumps(data,ensure_ascii=False)+tail[end:]
    sent=outbound_packet(packet)
    assert facts(sent)['output_constraints']==clean and 'SYNTHETIC-secret-extension' not in sent
