"""Original-page group execution over the existing isolated selection engine."""
import copy,json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from xau_next.domains.analysis.public import ModelCallStore,frozen_answer_schema
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore
from xau_next.platform.jobs.public import LeaseLost
from xau_next.platform.model_gateway.lab_simulation import SimulationCredentials,simulation_gateway,simulation_actual
from xau_next.platform.model_gateway.selection import SelectionLedger
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from .model_selection import ARMS,CONFIG_FIELDS,digest,make_plan,seal_plan,freeze_sample,run_sample,score_sample,read,validate_plan

CONFIRMATION='授权本组最多4次真实模型调用'


def fail(code,text):raise DomainError(code,text,409)


class ComparisonCoordinator:
    def __init__(self,*,lab,db,jobs,events,model_api,market,clock,stopped=lambda:False):
        self.lab,self.db,self.jobs,self.events,self.model_api,self.market,self.clock=(lab,db,jobs,events,model_api,market,clock)
        self.stopped=stopped
        self.simulation_factory=simulation_gateway
        self.actual_provider=None

    def folder(self,id):
        if not id or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in id):fail('COMPARISON_ID_INVALID','比较身份无效。')
        # Keep raw experiment files outside the formal data tree, preserving the
        # original selection engine's separation rule and shared accounting root.
        base=(self.db.data_root.parent/(self.db.data_root.name+'-comparisons')).resolve();folder=(base/id).resolve()
        if not folder.is_relative_to(base):fail('COMPARISON_ID_INVALID','比较目录越界。')
        return folder

    def event(self,id,uow):
        self.events.append(uow,'resource.changed',resource={'kind':'analysis_comparison','id':id},revision=1)

    def status(self):
        result=self.lab.status()
        active=[e for e in self.lab.executions(['queued','running']) if e['mode']=='real']
        result.update(execution_ready=True,simulation_available=self.market.source_kind=='synthetic',
          paid_execution_enabled=bool(active),authorized_calls_remaining=sum(4-e['attempts_reserved'] for e in active),
          message='比较流程已接通；当前无新组授权时不发送真实模型。每组4次须明确授权，失败不自动重试。')
        return result

    def queue_run(self,id,command,*,uow):
        original=self.lab.internal_comparison(id,scope=uow)
        if self.lab.execution(id,scope=uow):fail('COMPARISON_ALREADY_STARTED','本组已登记过启动，不能重发、补发或自动重试；请查看原记录。')
        if original['purpose']!='comparison' or len(original['arms'])!=4:fail('COMPARISON_ONLY','仅四模型同题比较记录可启动。')
        mode=command.get('mode','real');q=original['frozen_request'];profiles={}
        if mode=='simulation':
            if original['data_kind']!='SYNTHETIC' or self.market.source_kind!='synthetic':fail('COMPARISON_SYNTHETIC_ONLY','模拟只能用于合成资料，不能把真实输入改成模拟成绩。')
            config=copy.deepcopy(self.model_api.settings.snapshot(scope=uow)['values'])
            config.update(base_url='http://127.0.0.1:8317/v1',protocol='responses',daily_call_limit=50,daily_budget=None,
              input_price_per_million=None,output_price_per_million=None,price_currency=None,price_source=None)
            cpa={'profile_id':'SYNTHETIC','values':config,'credential_ref':'XAU-Next/model/SYNTHETIC-cpa'}
        elif mode=='real':
            if (command.get('max_calls')!=4 or command.get('acknowledge_possible_charge') is not True or
                command.get('accept_unknown_cost') is not True or command.get('authorization_text')!=CONFIRMATION or
                command.get('allow_readonly_history') is not True):
                fail('COMPARISON_BUDGET_REQUIRED','没有本组4次真实调用及只读评分的明确授权；未读取密钥、未发送。')
            if original['data_kind']!='REAL' or q['context']['market']['source_kind']!='mt5' or self.market.source_kind!='mt5':
                fail('COMPARISON_REAL_SOURCE_REQUIRED','真实比较须使用真实同源资料，合成资料不能发送到收费渠道。')
            if not 0<=(parse_utc(self.clock())-parse_utc(q['market_cutoff'])).total_seconds()<=30:
                fail('COMPARISON_SOURCE_STALE','本组行情已过30秒冻结期限，请重新准备新组；不会自动刷新或发送旧包。')
            previous=[e for e in self.lab.executions(['queued','running','awaiting_review','completed','failed','interrupted','cancelled'],scope=uow) if e['mode']=='real']
            if any(parse_utc(q['market_cutoff'])<parse_utc(self.lab.internal_comparison(e['comparison_id'],scope=uow)['market_cutoff'])+timedelta(minutes=180) for e in previous):
                fail('COMPARISON_SAMPLE_OVERLAP','上一组真实预测窗口尚未结束；新组起点至少间隔180分钟，不把重叠窗口当作独立样本。')
            for name in ['cpa_profile_id','deepseek_profile_id']:
                if not command.get(name):fail('COMPARISON_PROFILE_REQUIRED','请选择本组已有的CPA和DeepSeek官方配置；不切换日常配置。')
                profiles[name]=self.model_api.settings.comparison_profile(command[name],scope=uow)
            cpa=profiles['cpa_profile_id'];deep=profiles['deepseek_profile_id']
            if deep['values']['base_url']!='https://api.deepseek.com/v1' or not deep.get('credential_ref'):
                fail('COMPARISON_PROFILE_REQUIRED','DeepSeek须为已保存的官方API配置及本应用凭据引用。')
        else:fail('COMPARISON_MODE_INVALID','比较模式无效。')
        accounting=(self.db.data_root/'comparison-simulation-accounting'/id) if mode=='simulation' else self.db.data_root
        plan=make_plan(cpa,accounting,answer_schema=frozen_answer_schema(q),clock=self.clock,
                       cohort='synthetic' if mode=='simulation' else 'forward')
        sample='sample-'+id
        plan.update(experiment_id='lab-'+id,total_call_limit=4,accept_unknown_cost=True,prompt_version=q['prompt_version'],
          first_source_not_before=q['market_cutoff'],registered_samples=[{'sample_id':sample,'stage':1,
            'not_before':q['market_cutoff'],'arms':list('ABCD')}])
        if q['prompt_version']=='manual-analysis/23':
            macro=q['context'].get('macro_research',{})
            plan.update(input_revision='macro-research/2' if macro.get('version')=='macro-research/2' else 'economic-actuals/1',
              external_research={'mode':q['context']['research_policy']['mode'],'context_path':'frozen-comparison',
                'context_sha256':digest(q['context']['economic_context'])})
            if plan['input_revision']=='macro-research/2':plan['macro_research_sha256']=digest(macro)
        if mode=='real':
            for arm in 'CD':
                # Preserve explicit channel byte/time limits. Unknown per-model
                # prices stay unknown, never copy Flash's price onto Pro.
                config={k:copy.deepcopy(deep['values'][k]) for k in CONFIG_FIELDS}
                config.update(model_id=ARMS[arm],protocol='responses',reasoning_effort='high',output_format='json_schema',stream=True,
                  input_price_per_million=None,output_price_per_million=None,price_currency=None,price_source=None)
                if config['daily_budget'] is not None:fail('COMPARISON_PRICE_UNKNOWN','DeepSeek配置有金额限制但两模型价格未分别核实；不能绕过原金额限制。')
                plan['arms'][arm].update(configuration=config,credential_ref=deep['credential_ref'])
        plan['status']='FROZEN';validate_plan(plan,frozen=True)
        authorization={'allow_paid':True,'plan_sha256':digest(plan),'stages':[1],'max_attempts':4,
          'data_kind':'SYNTHETIC' if mode=='simulation' else 'REAL','accept_unknown_cost':True,'sample_ids':[sample]}
        job=self.jobs.enqueue(uow,'comparison_run',{'comparison_id':id},dedup_key='comparison-run-'+id,max_attempts=1)
        data={'comparison_id':id,'mode':mode,'state':'queued','outcome':None,'job_id':job['job_id'],'group_call_limit':4,
          'attempts_reserved':0,'real_calls_sent':0,'simulation_calls_sent':0,'wire_sha256':None,'started_at':None,'ended_at':None,
          'message':'合成流程已登记，不访问真实模型或密钥。' if mode=='simulation' else '本组已明确授权最多4次，等待一次派发；失败不重发。',
          'scores':[],'completed_horizons':[],'simulation_advanced':False,'billing_status':'SYNTHETIC' if mode=='simulation' else 'UNKNOWN',
          'error_code':None,'arms':copy.deepcopy(original['arms']),'score_jobs':{},'plan':plan,'authorization':authorization,
          'profiles':profiles,'request_sha256':digest(q),'approval':copy.deepcopy(command),'sample_id':sample}
        self.lab.put_execution(id,data,uow=uow);self.event(id,uow)
        return job

    def stopped_for(self,lease):
        if self.stopped():return True
        try:return self.jobs.inspect_lease(lease)['state']=='cancelling'
        except LeaseLost:return True

    def profile_check(self,e):
        if e['mode']=='real':
            for name,old in e['profiles'].items():
                now=self.model_api.settings.comparison_profile(old['profile_id'])
                if digest(now)!=digest(old):fail('COMPARISON_PROFILE_CHANGED','本组封存的配置已改变，未自动换配置或发送。')

    def collect(self,id,e):
        root=self.folder(id);folder=root/e['sample_id'];arms=[];sent=0
        ledger=SelectionLedger(root/'selection.sqlite3',digest(e['plan'])).status() if (root/'selection.sqlite3').exists() else {'attempts_reserved':0,'slots':{}}
        for base in self.lab.internal_comparison(id)['arms']:
            arm=base['arm'];path=folder/(arm+'-result.json');result={}
            if path.exists():
                try:result=read(path)
                except Exception:result={'state':'outcome_unknown','error_code':'COMPARISON_RESULT_INCOMPLETE'}
            elif (folder/(arm+'-started.json')).exists() or (folder/(arm+'-sent.json')).exists() or e['plan']['experiment_id']+':'+e['sample_id']+':'+arm in ledger['slots']:
                result={'state':'outcome_unknown','error_code':'COMPARISON_INTERRUPTED_NO_RESEND'}
            sent+=int((folder/(arm+'-sent.json')).exists())
            arms.append({**base,'state':result.get('state','not_sent'),
              'schema_valid':result.get('schema_valid',False),'business_valid':result.get('business_valid',False),
              'error_code':result.get('error_code'),'available_at':result.get('available_at'),
              'input_tokens':result.get('input_tokens'),'output_tokens':result.get('output_tokens'),
              'answer_available':bool((folder/(arm+'-answer.json')).exists())})
        attempts=ledger['attempts_reserved']
        return {'arms':arms,'attempts_reserved':attempts,'real_calls_sent':sent if e['mode']=='real' else 0,
                'simulation_calls_sent':sent if e['mode']=='simulation' else 0}

    def progress(self,id):
        e=self.lab.execution(id);updates=self.collect(id,e)
        with self.db.write() as uow:
            latest=self.lab.execution(id,scope=uow);latest.update(updates)
            self.lab.put_execution(id,latest,uow=uow);self.event(id,uow)

    def run(self,lease):
        id=lease.input_data['comparison_id'];e=self.lab.execution(id);q=self.lab.internal_comparison(id)['frozen_request']
        if digest(q)!=e['request_sha256']:fail('COMPARISON_INPUT_CHANGED','原冻结请求改变，未发送。')
        self.profile_check(e)
        with self.db.write() as uow:
            self.jobs.assert_current(uow,lease);e.update(state='running',started_at=self.clock(),message='正在处理本组四个模型；已占次数不会因失败或取消退回。')
            self.lab.put_execution(id,e,uow=uow);self.event(id,uow)
        root=self.folder(id)
        plan=seal_plan(root,{**e['plan'],'status':'DRAFT'})
        freeze_sample(root,e['sample_id'],q,clock=self.clock)
        packet=(root/e['sample_id']/'packet.txt').read_bytes()
        with self.db.write() as uow:
            current=self.lab.execution(id,scope=uow);current['wire_sha256']=digest(packet)
            self.lab.put_execution(id,current,uow=uow)
        isolated=None
        try:
            if e['mode']=='simulation':
                isolated=Database(Path(plan['formal_root']),clock=self.clock);isolated.initialize()
                calls=ModelCallStore(isolated,AttachmentStore(isolated),self.clock)
                gateway=self.simulation_factory(q);credentials=SimulationCredentials()
            else:calls=self.model_api.calls;gateway=self.model_api.gateway;credentials=self.model_api.settings.credentials
            run_sample(root,e['sample_id'],authorization=e['authorization'],gateway=gateway,credentials=credentials,calls=calls,
              clock=self.clock,profile_check=lambda:self.profile_check(e),stopped=lambda:self.stopped_for(lease),
              on_result=lambda _result:self.progress(id))
        finally:
            if isolated:isolated.close()
        self.progress(id)
        cancelled=self.stopped_for(lease)
        with self.db.write() as uow:
            self.jobs.assert_current(uow,lease,allow_cancelling=True)
            latest=self.lab.execution(id,scope=uow);good=sum(a['state']=='succeeded' for a in latest['arms'])
            latest.update(ended_at=self.clock(),outcome='succeeded' if good==4 else 'partial' if good else 'failed',
              state='cancelled' if cancelled else 'awaiting_review' if good else 'failed',
              message=('本组已停止；已发送和未知次数保留，不补发。' if cancelled else
                '回答已保存，等待各窗口结束评分；合格回答数不是方向命中率。' if good else '没有通过校验的回答；原失败及计次保留，不补发。'))
            self.lab.put_execution(id,latest,uow=uow);self.event(id,uow)
            if not cancelled:self.jobs.finish(uow,lease,{'kind':'analysis_comparison','id':id})
        if cancelled:self.jobs.acknowledge_cancel(lease)

    def recover(self):
        for e in self.lab.executions(['queued','running']):
            with self.db.read() as scope:job=self.jobs.get(scope,e['job_id'])
            if not job or job['state'] not in {'failed','cancelled'}:continue
            updates=self.collect(e['comparison_id'],e)
            with self.db.write() as uow:
                current=self.lab.execution(e['comparison_id'],scope=uow)
                if current['state'] not in {'queued','running'}:continue
                current.update(updates,state='cancelled' if job and job['state']=='cancelled' else 'interrupted',
                  outcome='interrupted',ended_at=self.clock(),error_code='COMPARISON_INTERRUPTED_NO_RESEND',
                  message='任务中断或取消；已占次数、回答和未知状态保留，不能重发本组。')
                self.lab.put_execution(e['comparison_id'],current,uow=uow);self.event(e['comparison_id'],uow)

    def due(self,e,q,as_of):
        return [m for m in [30,60,180] if m not in e['completed_horizons'] and
          parse_utc(as_of)>=parse_utc(q['market_cutoff'])+timedelta(minutes=m+3)]

    def queue_score(self,id,command,*,uow):
        e=self.lab.execution(id,scope=uow)
        if not e or e['state'] not in {'awaiting_review','completed'}:fail('COMPARISON_SCORE_NOT_READY','本组尚未收到可评分回答或已停止。')
        q=self.lab.internal_comparison(id,scope=uow)['frozen_request'];advance=command.get('advance_simulation',False)
        if advance and (e['mode']!='simulation' or q['context']['market']['source_kind']!='synthetic'):
            fail('COMPARISON_SYNTHETIC_ONLY','只允许合成验收推进模拟时间，真实窗口必须等待结束。')
        asof=(parse_utc(q['market_cutoff'])+timedelta(minutes=183)).isoformat().replace('+00:00','Z') if advance else self.clock()
        due=self.due(e,q,asof)
        if not due:fail('COMPARISON_SCORE_NOT_DUE','当前没有新的已结束窗口，未读取行情或重算。')
        bound=max(due);key=str(bound)
        if key in e['score_jobs']:
            job=self.jobs.get(uow,e['score_jobs'][key])
            if job:return job
        job=self.jobs.enqueue(uow,'comparison_score',{'comparison_id':id,'advance_simulation':advance,'through_minutes':bound},
                              dedup_key='comparison-score-'+id+'-'+key,max_attempts=1)
        e['score_jobs'][key]=job['job_id'];self.lab.put_execution(id,e,uow=uow);self.event(id,uow)
        return job

    def enqueue_due(self):
        for e in self.lab.executions(['awaiting_review']):
            q=self.lab.internal_comparison(e['comparison_id'])['frozen_request']
            if not self.due(e,q,self.clock()):continue
            with self.db.write() as uow:self.queue_score(e['comparison_id'],{},uow=uow)

    def score(self,lease):
        id=lease.input_data['comparison_id'];e=self.lab.execution(id);q=self.lab.internal_comparison(id)['frozen_request']
        advance=lease.input_data.get('advance_simulation',False);bound=lease.input_data['through_minutes']
        if e['state']!='awaiting_review':fail('COMPARISON_SCORE_NOT_READY','本组不在等待评分状态。')
        asof=(parse_utc(q['market_cutoff'])+timedelta(minutes=183)).isoformat().replace('+00:00','Z') if advance else self.clock()
        if e['mode']=='simulation':
            market=(self.actual_provider(q,asof) if self.actual_provider else simulation_actual(q,as_of=asof,
                    available_times=[a.get('available_at') for a in e['arms']]))
        else:
            if advance or not e['approval'].get('allow_readonly_history'):fail('COMPARISON_REAL_WINDOW_REQUIRED','真实评分必须等待结束并获得只读行情授权。')
            start=parse_utc(q['market_cutoff'])-timedelta(minutes=2);end=parse_utc(q['market_cutoff'])+timedelta(minutes=bound+3)
            market=self.market.fetch_history({'symbol':q['context']['market']['symbol'],
              'from':start.isoformat().replace('+00:00','Z'),'to':end.isoformat().replace('+00:00','Z')},q['context']['settings']).to_dict()
            asof=self.clock()
        scored=score_sample(self.folder(id),e['sample_id'],market,as_of=asof,
                            clock=(lambda:asof) if e['mode']=='simulation' and advance else self.clock)
        rows=[]
        folder=self.folder(id)/e['sample_id']
        for arm,records in scored['arms'].items():
            answer=read(folder/(arm+'-answer.json')) if (folder/(arm+'-answer.json')).exists() and next(a for a in e['arms'] if a['arm']==arm)['business_valid'] else None
            for r in records:
                rg=r.get('range',{});error=rg.get('boundary_absolute_error')
                direction=next((h['direction'] for h in answer['horizons'] if h['horizon_minutes']==r['minutes']),None) if answer else None
                rows.append({'arm':arm,'minutes':r['minutes'],'scope':r['scope'],'state':r['state'],
                  'direction':direction,'direction_class':r.get('direction_class','unknown'),
                  'range_boundary_mae':str(Decimal(error)/2) if error is not None else None,
                  'range_contained':bool(rg['contained']) if rg.get('contained') is not None else None,
                  'usable_primary':r.get('usable_primary'),'source_sha256':r.get('actual_source_sha256'),
                  'message':r.get('reason') or ('内部M1区间误差；不是逐笔全路径极值。' if error is not None else '区间或端点资料不足时保持未知。')})
        if self.stopped_for(lease):self.jobs.acknowledge_cancel(lease);return
        with self.db.write() as uow:
            self.jobs.assert_current(uow,lease);current=self.lab.execution(id,scope=uow)
            done=sorted(set(current['completed_horizons'])|set(self.due(current,q,asof)))
            current.update(scores=rows,completed_horizons=done,simulation_advanced=advance or current['simulation_advanced'],
              state='completed' if done==[30,60,180] else 'awaiting_review',
              message='各窗口评分已保存，按周期分别查看；合成成绩不可用于选模。' if e['mode']=='simulation' else '已结束窗口评分已保存，缺失和失败仍保留；不自动选冠军。')
            self.lab.put_execution(id,current,uow=uow);self.event(id,uow)
            self.jobs.finish(uow,lease,{'kind':'analysis_comparison','id':id})

    def answer(self,id,arm):
        if arm not in ARMS:fail('COMPARISON_ARM_INVALID','模型编号无效。')
        e=self.lab.execution(id)
        if not e:fail('COMPARISON_ANSWER_MISSING','本组尚未执行。')
        folder=self.folder(id)/e['sample_id'];path=folder/(arm+'-answer.json')
        if not path.exists():fail('COMPARISON_ANSWER_MISSING','本模型尚无完整回答原文；可能失败或状态未知。')
        raw=path.read_bytes();result_path=folder/(arm+'-result.json')
        result=read(result_path) if result_path.exists() else {}
        if result.get('answer_sha256') and digest(raw)!=result['answer_sha256']:fail('COMPARISON_ANSWER_CHANGED','回答原文哈希改变，拒绝显示为原回答。')
        return {'comparison_id':id,'arm':arm,'data_kind':'SYNTHETIC' if e['mode']=='simulation' else 'REAL',
          'answer_text':raw.decode('utf-8'),'sha256':digest(raw),'schema_valid':result.get('schema_valid',False),
          'business_valid':result.get('business_valid',False)}
