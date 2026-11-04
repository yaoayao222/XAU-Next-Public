"""SYNTHETIC upstream errors: strict admission, no retries, immutable saved history."""
import asyncio
import json
import httpx
import pytest

from xau_next.platform.model_gateway.public import Gateway, GatewayResult, extract, stream_failure_diagnosis
from xau_next.domains.settings.model_api import defaults
from xau_next.shared.errors import DomainError
from tests.model_api.test_workflow import ModelFlow


def frame(value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


ERROR = frame({'error': {'type': 'upstream_error', 'message': 'SYNTHETIC-SECRET https://private.invalid'}})
TEXT = frame({'choices': [{'index': 0, 'delta': {'content': '{}'}, 'finish_reason': 'stop'}]})


@pytest.mark.parametrize('raw,has_text', [(ERROR, False), (ERROR+b'data: [DONE]\n\n', False), (TEXT+ERROR, True), (TEXT+ERROR+b'data: [DONE]\n\n', True)])
def test_explicit_failure_dominates_text_and_done_without_leaking_provider_prose(raw, has_text):
    with pytest.raises(DomainError) as caught:
        extract(GatewayResult(raw=raw, status=200, content_type='text/event-stream', complete=True), 'chat_completions')
    assert caught.value.code == 'MODEL_UPSTREAM_FAILED'
    assert 'SYNTHETIC-SECRET' not in caught.value.detail and 'private.invalid' not in caught.value.detail
    diagnosis = stream_failure_diagnosis(raw, 'chat_completions')
    assert diagnosis['answer_text_received'] is has_text
    assert ('未收到答案正文' if not has_text else '部分答案正文') in diagnosis['detail']


def test_chat_error_is_a_terminal_failure_even_if_channel_keeps_socket_open():
    sent=[]
    class KeptOpen(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(0,len(ERROR),3):yield ERROR[index:index+3]
            await asyncio.sleep(30)
    def send(request):
        sent.append(request)
        return httpx.Response(200,stream=KeptOpen(),headers={'content-type':'text/event-stream'})
    result=Gateway(httpx.MockTransport(send)).exchange({**defaults(),'protocol':'chat_completions','stream':True,'timeout_seconds':0.5},'SYNTHETIC',b'{}')
    assert result.complete and result.error_code is None and result.raw==ERROR and len(sent)==1
    assert result.diagnostics['terminal_event_ms'] is not None
    with pytest.raises(DomainError) as caught:extract(result,'chat_completions')
    assert caught.value.code=='MODEL_UPSTREAM_FAILED'


@pytest.mark.parametrize('code,status,transport_error', [('MODEL_RATE_LIMITED',429,None),('MODEL_TIMEOUT_UNKNOWN',200,'MODEL_TIMEOUT_UNKNOWN'),('MODEL_CONNECTION_UNKNOWN',200,'MODEL_CONNECTION_UNKNOWN')])
def test_transport_and_http_errors_keep_their_original_priority(code,status,transport_error):
    with pytest.raises(DomainError) as caught:
        extract(GatewayResult(raw=ERROR,status=status,content_type='text/event-stream',complete=True,error_code=transport_error),'chat_completions')
    assert caught.value.code==code


def test_normal_success_missing_end_and_length_limit_remain_distinct():
    assert extract(GatewayResult(raw=TEXT+b'data: [DONE]\n\n',status=200,content_type='text/event-stream',complete=True),'chat_completions')['answer']=='{}'
    for raw,code in [(TEXT,'MODEL_INCOMPLETE'),(TEXT.replace(b'"stop"',b'"length"')+b'data: [DONE]\n\n','MODEL_OUTPUT_LIMIT')]:
        with pytest.raises(DomainError) as caught:extract(GatewayResult(raw=raw,status=200,content_type='text/event-stream',complete=True),'chat_completions')
        assert caught.value.code==code
    assert stream_failure_diagnosis(TEXT,'chat_completions') is None


@pytest.mark.parametrize('raw', [b'data: {"error":null}\n\n', b'data: {"error":{}}\n\n', b'data: {"choices":[{"delta":{"content":"error"}}]}\n\n', b'data: invalid\n\n', b'x'*16000001], ids=['null','empty','answer-text','malformed','oversized'])
def test_ambiguous_or_oversized_diagnostics_do_not_invent_an_upstream_error(raw):
    assert stream_failure_diagnosis(raw,'chat_completions') is None


def test_old_failed_record_is_diagnosed_on_raw_get_without_rewriting_or_resending(tmp_path):
    f=ModelFlow(tmp_path)
    try:
        request=f.ready();started=f.start(request);id=started['call']['call_id']
        prepared=f.w.model_api.calls.prepare(ERROR)
        with f.w.db.write() as tx:
            ref=f.w.model_api.calls.register(prepared,uow=tx)
            f.w.model_api.calls.update(id,{'state':'failed','stage':'response_saved','protocol':'chat_completions','error_code':'MODEL_INCOMPLETE','detail':'原缺结束标记错误','raw_response':ref,'answer':None},uow=tx)
        before=f.w.model_api.calls.get(id,internal=True);sent=len(f.sent)
        value=f.get('/v1/model/calls/'+id+'/raw')
        assert value['failure_diagnosis']['code']=='MODEL_UPSTREAM_FAILED'
        assert value['raw_response'].encode()==ERROR and value['answer'] is None
        assert f.w.model_api.calls.get(id,internal=True)==before and len(f.sent)==sent
        assert f.get('/v1/model/calls/'+id)['error_code']=='MODEL_INCOMPLETE'
        assert f.get('/v1/workspace')['current_forecast'] is None
    finally:f.close()


def test_new_chat_failure_is_saved_once_without_a_prediction_or_retry(tmp_path):
    f=ModelFlow(tmp_path)
    try:
        f.configure()
        sample={'data_kind':'SYNTHETIC','ok':True,'date_time':'2000-01-02T03:04:05.006Z','reference':'probe-a_b--01','signed_number':-12.5,'text':'中文'}
        probe=frame({'model':'gpt-6.1-sol','choices':[{'index':0,'delta':{'content':json.dumps(sample,ensure_ascii=False)},'finish_reason':'stop'}]})+b'data: [DONE]\n\n'
        f.handler=lambda _:httpx.Response(200,content=probe,headers={'content-type':'text/event-stream'})
        f.setup_model(protocol='chat_completions',stream=True)
        request=f.request(f.refresh())
        f.handler=lambda _:httpx.Response(200,content=ERROR,headers={'content-type':'text/event-stream'})
        started=f.start(request);f.w.model_api.process_once(f.w);record=f.record(started)
        assert record['state']=='failed' and record['error_code']=='MODEL_UPSTREAM_FAILED'
        assert record['answer'] is None and record['version_id'] is None
        assert '未收到答案正文' in record['detail'] and 'SYNTHETIC-SECRET' not in record['detail']
        assert f.get('/v1/model/calls/'+record['call_id']+'/raw')['raw_response'].encode()==ERROR
        assert len(f.sent)==2 and not f.w.model_api.process_once(f.w)
        assert f.get('/v1/workspace')['current_forecast'] is None
    finally:f.close()
