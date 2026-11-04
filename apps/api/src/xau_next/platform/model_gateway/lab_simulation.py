"""Explicit SYNTHETIC workflow demonstration. No external sockets or real keys."""
import json
from datetime import timedelta
from decimal import Decimal
import httpx
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from .public import Gateway

SIM_KEY='SYNTHETIC-COMPARISON-ONLY'


class SimulationCredentials:
    def configured(self,reference):return bool(reference and reference.startswith('XAU-Next/model/'))
    def read(self,reference):
        if not reference.startswith('XAU-Next/model/'):raise ValueError('invalid simulation reference')
        return SIM_KEY


def answer_for(request,arm):
    if request['context']['market']['source_kind']!='synthetic':
        raise DomainError('COMPARISON_SYNTHETIC_ONLY','模拟回答只能用于明确的合成资料。',409)
    context=request['context'];at=parse_utc(request['market_cutoff']);price=Decimal(context['market']['quote']['bid'])
    span=min(Decimal(2),price/10);direction={'A':'long','B':'short','C':'uncertain','D':'long'}[arm]
    refs=[request['market_snapshot_id']];note='SYNTHETIC流程验收，不是真实模型判断或交易建议。'
    basis={'direction_basis':note,'range_basis':note,'supporting_evidence':[note],
           'opposing_evidence':[note],'evidence_refs':refs,'limitations':[note]}
    horizons=[]
    for minutes in request['horizon_minutes']:
        end=(parse_utc(request['window_start'])+timedelta(minutes=minutes)).isoformat().replace('+00:00','Z')
        phase_start=request['market_cutoff'] if parse_utc(end)>at else request['window_start']
        horizons.append({'horizon_minutes':minutes,'end_at':end,'direction':direction,
          'expected_range':{'low':format(price-span,'f'),'high':format(price+span,'f')},
          'phases':[{'start_at':phase_start,'end_at':end,'direction':direction,'note':note}],
          'turning_points':[],'confidence':None,'forecast_basis':basis})
    experience=context.get('experience')
    value={k:request[k] for k in ['request_id','forecast_id','base_version_id','market_snapshot_id','market_cutoff']}
    value.update(schema_version='1.7.0',summary=note,market_regime='range',horizons=horizons,plans=[],order_advice=[],evidence_refs=refs,
      analysis_basis={'main_thesis':note,'supporting_evidence':[note],'opposing_evidence':[note],
        'alternative':note,'range_basis':note,'change_since_previous':note,'limitations':[note],'reassessment_conditions':[]},
      candidate_assessments=[{'trading_style':style,'outcome':'no_candidate','plan_key':None,'rationale':note,'missing_conditions':[note]}
        for style in request['analysis_preferences']['enabled_styles']],
      experience_application={'digest_id':experience.get('digest_id') if experience else None,
        'status':'reviewed_no_change' if experience else 'unavailable',
        'case_assessments':[{'review_ref':c['review_ref'],'relevance':'unknown','current_evidence_refs':[],
          'similarities':[],'differences':[note],'decision':'uncertain','reason':note} for c in (experience or {}).get('cases',[])],
        'limitations':[note]},prediction_adjustments=[])
    return value


def simulation_gateway(request,*,change=None):
    models={'gpt-6.1-sol':'A','gpt-6-astra':'B','deepseek-flash':'C','deepseek-v4-pro':'D'}
    def receive(incoming):
        body=json.loads(incoming.content);model=body['model'];answer=answer_for(request,models[model])
        if change:answer=change(answer,models[model])
        payload={'id':'SYNTHETIC-'+model,'status':'completed','model':model,
          'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer,ensure_ascii=False)}]}],
          'usage':{'input_tokens':1,'output_tokens':1}}
        raw=b'data: '+json.dumps({'type':'response.completed','response':payload},ensure_ascii=False).encode()+b'\n\n'
        return httpx.Response(200,content=raw,headers={'Content-Type':'text/event-stream'})
    return Gateway(httpx.MockTransport(receive))


def simulation_actual(request,*,as_of,available_times=()):
    """Fabricated upward path for workflow checks, never real price evidence."""
    if request['context']['market']['source_kind']!='synthetic':raise ValueError('synthetic only')
    market=request['context']['market'];start=parse_utc(request['market_cutoff']);price=Decimal(market['quote']['bid'])
    stamp=lambda seconds:(start+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')
    bid=lambda seconds:price+Decimal(str(seconds))/Decimal(10800)
    bars=[]
    for i in range(180):
        p,q=bid(i*60),bid((i+1)*60)
        bars.append({'open_at':stamp(i*60),'close_at':stamp((i+1)*60),'open':str(p),'close':str(q),
          'low':str(p-Decimal('.1')),'high':str(q+Decimal('.1')),'closed':True,'tick_volume':1})
    offsets={i*60 for i in range(181)}
    offsets.update((parse_utc(t)-start).total_seconds() for t in available_times if t and start<=parse_utc(t)<=start+timedelta(minutes=180))
    bars=[b for b in bars if parse_utc(b['close_at'])<=parse_utc(as_of)]
    offsets={i for i in offsets if start+timedelta(seconds=i)<=parse_utc(as_of)}
    return {'snapshot_id':'SYNTHETIC-comparison-actual','source_kind':'synthetic','account_scope_id':market['account_scope_id'],
      'symbol':market['symbol'],'price_basis':'bid','from':stamp(-60),'to':stamp(10980),'bars':bars,
      'tick_status':'available','ticks':[{'event_time':stamp(i),'bid':str(bid(i)),'ask':str(bid(i)+Decimal('.1'))} for i in sorted(offsets)],
      'status':{'state':'available','captured_at':as_of,'observed_at':as_of,'reasons':[]},
      'synthetic':True,'limitation':'Fabricated path; never evidence of model quality.'}
