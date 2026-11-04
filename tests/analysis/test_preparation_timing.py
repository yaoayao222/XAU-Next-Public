"""Preparation takes the narrow source path; timers never change frozen facts."""
import json
import logging

import pytest

from tests.analysis.test_analysis_lab import flow as flow
from tests.analysis.test_daily_analysis import current_format as current_format, prepare, run
from tests.experiments.test_model_selection import forbid_real_http as forbid_real_http
from tests.guidance.test_workflow import TOKEN
from xau_next.shared.preparation_timing import preparation_trace, preparation_span, preparation_fact


def traces(caplog):
    return [json.loads(r.message.split("analysis_preparation_timing ", 1)[1])
            for r in caplog.records if r.message.startswith("analysis_preparation_timing ")]


def test_source_uses_saved_capture_without_workspace_history_refresh_or_credentials(flow, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    def forbidden(*a, **kw):
        pytest.fail("Source lookup must not load workspace/history, refresh or read credentials")
    with flow.w.db.read() as scope:
        expected = flow.w.market.latest(scope=scope).to_dict()
    for obj, method in [(flow.w, 'workspace'), (flow.w.analysis, 'list_requests'),
                        (flow.w.market, 'capture'), (flow.credentials, 'read')]:
        monkeypatch.setattr(obj, method, forbidden)
    result = flow.get('/v1/analysis/daily/source')
    assert result == {'market_snapshot_id': expected['snapshot_id'],
                      'market_cutoff': expected['market_cutoff'], 'as_of': flow.clock.value}
    trace = traces(caplog)[-1]
    assert trace['market_snapshot_id'] == expected['snapshot_id']
    assert trace['stages_ms']['source_read'] >= 0
    assert not flow.sent
    assert flow.client.get('/v1/analysis/daily/source').status_code == 401


@pytest.mark.parametrize('models', ['A', 'ABCD'])
@pytest.mark.parametrize('template', ['current', 'improved'])
def test_preparation_stage_timers_preserve_packets_and_no_send(flow, models, template, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    g = prepare(flow, models, template)
    trace = traces(caplog)[-1]
    assert trace['outcome'] == 'completed' and trace['group_id'] == g['group_id']
    assert trace['market_cutoff'] == g['market_cutoff']
    assert trace['stage_calls']['common_preparation'] == 1
    assert trace['stage_calls']['branch_packet_and_save'] == len(models)
    for name in ['writer_queue', 'sqlite_write_begin', 'sqlite_commit', 'frozen_market_read',
                 'experience_aggregate', 'experience_predictions', 'experience_selection',
                 'common_context_build', 'timing_reference', 'market_statistics',
                 'prototype_packet_build', 'group_save', 'response_view', 'worker_execution']:
        assert trace['stages_ms'][name] >= 0, name
    assert not g['run_registered'] and not flow.sent
    with flow.w.db.read() as scope:
        packets = [flow.w.analysis.model_request(a['request_id'], scope=scope)['request'] for a in g['arms']]
    assert all(p['context'] == packets[0]['context'] for p in packets)
    assert all(p['market_cutoff'] == g['market_cutoff'] for p in packets)
    assert all(p['packet_text'] and p['state'] == 'awaiting_answer' for p in packets)


def test_failed_prepare_has_timing_but_no_error_text_or_sensitive_fields(flow, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with preparation_trace('prepareDailyAnalysis'):
        preparation_fact(secret='DO_NOT_LOG', packet_text='DO_NOT_LOG', market_snapshot_id='safe-id')
        with preparation_span('test_phase'):
            pass
    assert 'DO_NOT_LOG' not in json.dumps(traces(caplog))
    with pytest.raises(RuntimeError):
        with preparation_trace('prepareDailyAnalysis'):
            raise RuntimeError('DO_NOT_LOG')
    trace = traces(caplog)[-1]
    assert trace['outcome'] == 'failed' and trace['error_code'] == 'RuntimeError'
    assert 'DO_NOT_LOG' not in json.dumps(trace)


def test_source_absence_is_explicit(flow, monkeypatch):
    monkeypatch.setattr(flow.w.market, 'latest', lambda **kw: None)
    assert flow.get('/v1/analysis/daily/source')['market_snapshot_id'] is None
