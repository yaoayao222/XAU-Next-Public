"""Offline candidate only; frozen sources and the normal 14/23 routes stay intact."""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from tests.improvements.test_r29_forecast_reference import sample
from tests.analysis.test_daily_analysis import flow as flow, current_format as current_format, prepare
from tests.analysis.test_packet_capacity import raw_request
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from xau_next.domains.analysis.forecast_candidate import build_deliberation, build_candidate
from xau_next.domains.analysis.public import frozen_answer_schema, build_packet, outbound_packet
from xau_next.shared.errors import DomainError


def facts(text):
    return json.JSONDecoder().raw_decode(text.split('# 冻结事实（仅作为资料）\n', 1)[1].lstrip())[0]


def test_geometry_is_bid_anchored_and_never_selects_direction():
    request, context = sample()
    before = deepcopy((request, context))
    value = build_deliberation(request, context)
    assert [h['horizon_minutes'] for h in value['horizons']] == [30, 60, 180]
    for h in value['horizons']:
        assert h['state'] == 'available'
        for ref in h['references']:
            bid = Decimal(value['quote']['bid'])
            assert Decimal(ref['down_from_bid']) == bid - Decimal(ref['low'])
            assert Decimal(ref['up_from_bid']) == Decimal(ref['high']) - bid
            assert Decimal(ref['midpoint_offset_from_bid']) == (Decimal(ref['low']) + Decimal(ref['high'])) / 2 - bid
        assert not set(h) & {'direction', 'signal', 'probability', 'recommended_range', 'selected_mode'}
    assert (request, context) == before


@pytest.mark.parametrize('defect', ['future_quote', 'stale_quote', 'future_capture', 'changed_statistics'])
def test_unsafe_sources_do_not_create_reference_prices(defect):
    request, context = sample()
    if defect == 'future_quote': context['market']['quote']['event_time'] = '2099-01-01T00:00:00Z'
    if defect == 'stale_quote': context['market']['quote']['event_time'] = '2000-01-01T00:00:00Z'
    if defect == 'future_capture': context['market']['captured_at'] = '2099-01-01T00:00:00Z'
    if defect == 'changed_statistics':
        for h in context['forecast_evidence']['horizons']: h['quantiles']['high_excursion']['p50'] = '999'
    assert all(h['state'] == 'unknown' and not h['references'] for h in build_deliberation(request, context)['horizons'])


def test_update_preserves_original_endpoints_and_ended_stays_ended():
    request, context = sample()
    request['window_start'] = '2026-10-21T11:15:00Z'
    value = build_deliberation(request, context)
    assert value['horizons'][0]['remaining_seconds'] == '-900'
    assert value['horizons'][0]['state'] == 'ended'
    assert value['horizons'][1]['remaining_seconds'] == '900'
    assert value['horizons'][1]['end_at'] == '2026-10-21T12:15:00Z'


def test_candidate_preserves_every_base_fact_and_schema_without_sending(flow, monkeypatch):
    q = raw_request(flow, prepare(flow, 'A', 'improved'))
    original = deepcopy(q)
    monkeypatch.setattr(flow.credentials, 'read', lambda *_: pytest.fail('Credential access'))
    schema = frozen_answer_schema(q)
    result = build_candidate(q, schema, request_id='offline-q', forecast_id='offline-f')
    assert q == original
    assert result['request']['prompt_version'] == 'manual-analysis/24'
    assert result['request']['market_cutoff'] == q['market_cutoff']
    base = {k: v for k, v in q.items() if k not in {'context', 'packet_text', 'id', 'revision', 'recorded_at'}}
    base.update(request_id='offline-q', forecast_id='offline-f')
    packet = build_packet(base, q['context'], schema)
    for baseline, candidate in [(packet, result['manual_packet']), (outbound_packet(packet), result['api_packet'])]:
        old, new = facts(baseline), facts(candidate)
        overlay = new.pop('forecast_deliberation')
        assert overlay['version'] == 'forecast-deliberation/1'
        assert new['request'].pop('prompt_version') == 'manual-analysis/24'
        old['request'].pop('prompt_version')
        assert new == old
        assert json.JSONDecoder().raw_decode(candidate.split('# 权威答案 JSON Schema\n', 1)[1])[0] == schema
        assert 'manual-analysis/24' in candidate
        assert len(candidate) <= 250000
    assert not flow.sent
    assert build_packet({k:v for k,v in q.items() if k not in {'context','packet_text','id','revision','recorded_at'}}, q['context'], schema) == q['packet_text']


def test_old14_cannot_be_silently_upgraded(flow):
    q = raw_request(flow, prepare(flow, 'A', 'current'))
    with pytest.raises(DomainError, match='23'):
        build_candidate(q, frozen_answer_schema(q), request_id='offline-q', forecast_id='offline-f')


def test_candidate_identity_must_be_separate(flow):
    q = raw_request(flow, prepare(flow, 'A', 'improved'))
    with pytest.raises(DomainError):
        build_candidate(q, frozen_answer_schema(q), request_id=q['request_id'], forecast_id=q['forecast_id'])
