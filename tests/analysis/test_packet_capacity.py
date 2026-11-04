"""Capacity and exact-value regression, SYNTHETIC only; no external I/O."""
from copy import deepcopy
import json
import pytest
from tests.analysis.test_daily_analysis import flow, current_format, prepare, post
from tests.analysis.test_multi_source_research import multi
from tests.analysis.test_analysis_lab import add_bundle, research
from tests.model_api.test_experience_projection import synthetic_experience
from tests.experiments.test_model_selection import forbid_real_http
from xau_next.domains.analysis.public import build_packet, frozen_answer_schema, outbound_packet
from xau_next.domains.analysis.experience import expand_experience, compact_experience, project_experience
from xau_next.shared.errors import DomainError
from xau_next.shared.snapshots import Snapshot


def facts(text):
    return json.JSONDecoder().raw_decode(text.split('# 冻结事实（仅作为资料）\n',1)[1].lstrip())[0]


def large_experience(count=1300):
    value=synthetic_experience();value['cutoff']='2026-10-12T02:00:00Z'
    sample=value['cases'][0]['metrics']
    value['cases'][0]['metrics']=[deepcopy(sample[i%len(sample)]) for i in range(count)]
    # Keep absent, null, zero and negative values distinct in the same group.
    value['cases'][0]['metrics'][0].pop('reason')
    value['cases'][0]['metrics'][1]['value']='0'
    return value


def raw_request(f,g):
    with f.w.db.read() as tx:return f.w.analysis.model_request(g['arms'][0]['request_id'],scope=tx)['request']


def test_large_experience_keeps_every_value_in_manual_and_api_packets(flow):
    g=prepare(flow,'A','improved');q=raw_request(flow,g)
    q['context']['experience']=large_experience()
    packet=build_packet({k:v for k,v in q.items() if k not in {'context','packet_text'}},q['context'],frozen_answer_schema(q))
    assert len(packet)<=250000
    saved=facts(packet)
    assert saved['packet_encoding']=='macro-packet/1'
    assert expand_experience(saved['experience'])==q['context']['experience']
    api=facts(outbound_packet(packet))
    assert expand_experience(api['experience'])==project_experience(q['context']['experience'])
    assert len(expand_experience(api['experience'])['cases'][0]['metrics'])==1300
    # Schema compaction changes whitespace, never any assertion or field.
    schema=json.JSONDecoder().raw_decode(packet.split('# 权威答案 JSON Schema\n',1)[1])[0]
    assert schema==frozen_answer_schema(q)


def test_real_preparation_route_accepts_large_input_without_sending_models(flow,monkeypatch):
    monkeypatch.setattr(flow.w.review,'enrich_context',lambda *_,**__:Snapshot.of(large_experience()))
    monkeypatch.setattr(flow.credentials,'read',lambda *_:pytest.fail('Preparation accessed a credential'))
    g=prepare(flow,'ABCD','improved')
    assert not g['run_registered'] and len(g['arms'])==4 and g['same_question']
    with flow.w.db.read() as tx:
        assert not flow.w.analysis._docs.list(tx,'model_call',limit=10)
        requests=[flow.w.analysis.model_request(a['request_id'],scope=tx)['request'] for a in g['arms']]
    assert all(expand_experience(facts(q['packet_text'])['experience'])==requests[0]['context']['experience'] for q in requests)
    assert all(q['context']==requests[0]['context'] for q in requests)
    assert not flow.sent


def test_noncompressible_excess_reports_size_and_preserves_original_input(flow):
    q=raw_request(flow,prepare(flow,'A','improved'))
    q['context']['experience']=large_experience(6)
    q['context']['experience']['notes']=['SYNTHETIC-'+('资料不可截断'*50000)]
    original=deepcopy(q['context'])
    with pytest.raises(DomainError) as error:
        build_packet({k:v for k,v in q.items() if k not in {'context','packet_text'}},q['context'],frozen_answer_schema(q))
    assert error.value.code=='PACKET_TOO_LARGE'
    assert '250,000字符上限' in error.value.detail and '本轮模型未发送' in error.value.detail
    assert '请减少采集范围' not in error.value.detail and q['context']==original


def test_legacy14_and_macro1_do_not_use_new_encoding(flow):
    q=raw_request(flow,prepare(flow,'A','current'))
    assert 'packet_encoding' not in facts(q['packet_text'])
    bundle=add_bundle(flow,research())
    with flow.w.db.read() as tx:
        market=flow.w.market.latest(scope=tx).to_dict()
        old=flow.w.analysis_lab.prepare({'package_kind':'improved','bundle_id':bundle['bundle_id'],
            'market_snapshot_id':market['snapshot_id']},scope=tx)
    assert 'packet_encoding' not in facts(old['packet_text'])
