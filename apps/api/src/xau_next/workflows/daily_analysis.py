"""One daily round, independent ordinary forecasts, no selected primary model."""
from copy import deepcopy
import json
from xau_next.domains.analysis.public import DailyAnalysisStore, DAILY_MODELS, market_scope_key, explain_entry_timing, document_diagnosis
from xau_next.shared.errors import DomainError
from xau_next.shared.preparation_timing import preparation_span, preparation_fact
from xau_next.shared.values import new_id,parse_utc
from .model_selection import digest


def reject(code,text):raise DomainError(code,text,409)


class DailyAnalysisCoordinator:
    def __init__(self,workflows):
        self.w=workflows;self.store=DailyAnalysisStore(workflows.db)

    def prepare(self,body,*,uow):
        w=self.w;models=body['models']
        preparation_fact(models_count=len(models),package_kind=body['package_kind'])
        if not 1<=len(models)<=4 or len(set(models))!=len(models) or not set(models)<=set(DAILY_MODELS):
            reject('DAILY_MODELS_INVALID','请勾选1至4个不同模型。')
        selections=body.get('selections') or []
        selected={s['model_key']:s for s in selections}
        if selections and (len(selected)!=len(selections) or set(selected)!=set(models)):
            reject('DAILY_SELECTION_INVALID','每个所选分析位置须有且仅有一个模型连接。')
        if len({(s['profile_id'],s['model_id']) for s in selections})!=len(selections):
            reject('DAILY_SELECTION_INVALID','同一个连接的同一个模型不能在本轮重复勾选。')
        selected_providers={}
        for s in selections:
            profile=w.model_api.settings.comparison_profile(s['profile_id'],scope=uow)
            selected_providers[s['model_key']]=profile['values']['provider']
            if s['model_id'] not in w.model_api.settings.model_ids(profile['values']):
                reject('DAILY_MODEL_NOT_SAVED','该模型尚未保存在所选供应商的可选模型中。')
        id=new_id();kind=body.get('kind','new');base=None
        if kind=='update':
            if not body.get('base_group_id'):reject('DAILY_BASE_REQUIRED','窗口内更新须选择原分析轮次。')
            base=self.store.get(body['base_group_id'],scope=uow)
            if base['package_kind']!=body['package_kind']:reject('DAILY_TEMPLATE_CHANGED','窗口内更新保留原模板；换模板请新建一轮。')
        prototype=None;arms=[];hashes=[]
        for key in models:
            command={'kind':kind,'market_snapshot_id':body['market_snapshot_id'],'forecast_id':None,'base_version_id':None}
            if body.get('horizon_minutes') is not None:command['horizon_minutes']=body['horizon_minutes']
            if base:
                old=next((a for a in base['arms'] if a['model_key']==key),None)
                if not old:reject('DAILY_UPDATE_MODEL_MISSING','原轮次没有这个模型的回答，请新建一轮。')
                old={**old,**self.store.request_connection(old['request_id'],scope=uow)}
                s=selected.get(key)
                if (old.get('selection_explicit') and (not s or s['profile_id']!=old['profile_id'] or s['model_id']!=old['model_id'])) or (s and (s['model_id']!=old['model_id'] or old.get('profile_id') and s['profile_id']!=old['profile_id'])):
                    reject('DAILY_UPDATE_CHANNEL_CHANGED','窗口内更新保留该模型和渠道；更换模型或渠道请新建一轮。')
                result=w.model_api.lookup(old['request_id'],scope=uow)
                if not result['version_id']:reject('DAILY_UPDATE_BASE_MISSING','所选模型还没有成功保存的上一版，不能用别的模型回答替代。')
                version=w.analysis.get_version(result['version_id'],scope=uow).to_dict()
                current=w.analysis.get_forecast(version['forecast_id'],scope=uow).to_dict()['forecast']
                if current['current_version_id']!=version['version_id']:reject('DAILY_BASE_CHANGED','该轮次已有后续更新，请选择最新轮次。')
                command.update(forecast_id=version['forecast_id'],base_version_id=version['version_id'])
            if prototype is None or base:
                with preparation_span('common_preparation'):
                    q=w.analysis_lab.prepare(body,command=command,scope=uow,daily=True)
                if not base:prototype=q
            else:q=prototype
            with preparation_span('branch_packet_and_save'):
                request=w.analysis.save_daily_request(q,group_id=id,model_key=key,uow=uow).to_dict()
            hashes.append(digest(q['context']))
            arms.append({'model_key':key,'model_id':selected[key]['model_id'] if key in selected else DAILY_MODELS[key],
                         **({'profile_id':selected[key]['profile_id'],'selection_explicit':True,'selected_provider':selected_providers[key]} if key in selected else {}),'request_id':request['request_id'],
                         'forecast_id':request['forecast_id'],'packet_sha256':digest(request['packet_text'].encode())})
        market=q['context']['market']
        row={'group_id':id,'kind':kind,'base_group_id':body.get('base_group_id'),'created_at':w.clock(),
          'market_cutoff':q['market_cutoff'],'window_end':q['window_end'],'horizon_minutes':q['horizon_minutes'],
          'source_scope':market_scope_key(market),'data_kind':'SYNTHETIC' if market['source_kind']=='synthetic' else 'REAL',
          'package_kind':body['package_kind'],'template_version':q['prompt_version'],'bundle_id':body.get('bundle_id'),
          'context_sha256':hashes[0],'same_question':not base and len(set(hashes))==1,'models':models,'arms':arms,'run':None}
        preparation_fact(group_id=id)
        with preparation_span('group_save'):
            self.store.put(id,row,uow=uow);w._event(uow,'analysis_request',id)
        with preparation_span('response_view'):
            return self.view(row,scope=uow)

    def start(self,id,command,*,uow):
        w=self.w;group=self.store.get(id,scope=uow);n=len(group['arms']);simulation=command['mode']=='simulation'
        preparation_fact(group_id=id,market_cutoff=group['market_cutoff'],
            source_age_at_dispatch_ms=(parse_utc(w.clock())-parse_utc(group['market_cutoff'])).total_seconds()*1000)
        if group['run'] is not None:reject('DAILY_ALREADY_STARTED','本轮已经登记过发送，不能补发、重发或自动重试。')
        if command.get('max_calls')!=n or command.get('acknowledge_possible_charge') is not True or command.get('accept_unknown_cost') is not True or command.get('allow_readonly_history') is not True:
            reject('DAILY_AUTHORIZATION_REQUIRED',f'请明确本轮最多{n}次、费用政策和到期只读复盘许可；未读取密钥、未发送。')
        if simulation:
            if group['data_kind']!='SYNTHETIC' or w.market.source_kind!='synthetic':reject('DAILY_SYNTHETIC_ONLY','模拟仅限合成来源，不能把真实数据写成模拟成绩。')
        else:
            if group['data_kind']!='REAL' or w.market.source_kind!='mt5':reject('DAILY_REAL_SOURCE_REQUIRED','真实发送须使用真实MT5同源资料。')
            if not 0<=(parse_utc(w.clock())-parse_utc(group['market_cutoff'])).total_seconds()<=30:
                reject('DAILY_SOURCE_STALE','本轮资料已过30秒冻结期限，请主动重新准备新资料；未自动刷新或发送。')
            for arm in group['arms']:
                frozen=w.analysis.model_request(arm['request_id'],scope=uow,require_pending=False)['request']
                market=frozen['context']['market'];quote=market.get('quote')
                max_age=frozen['context']['settings']['values']['quote_max_age_ms']
                if market.get('quote_status',{}).get('state')!='available' or not quote or not 0<=(parse_utc(group['market_cutoff'])-parse_utc(quote['event_time'])).total_seconds()*1000<=max_age:
                    reject('DAILY_QUOTE_STALE','真实报价缺失或陈旧，不能把休市旧价当作新鲜资料发送。')
        configurations={}
        for arm in group['arms']:
            if w.model_api.lookup(arm['request_id'],scope=uow)['version_id']:
                reject('DAILY_ALREADY_ANSWERED','本轮已有手工保存的回答；不能再启动本轮模型组。')
            key=arm['model_key'];profile=None
            if simulation:
                values=deepcopy(w.model_api.settings.snapshot(scope=uow)['values'])
                values.update(enabled=True,base_url='http://127.0.0.1:8317/v1',daily_budget=None,
                  input_price_per_million=None,output_price_per_million=None,price_currency=None,price_source=None)
                reference='XAU-Next/model/SYNTHETIC-daily'
            else:
                profile_id=arm.get('profile_id') or command.get('cpa_profile_id' if key in 'AB' else 'deepseek_profile_id')
                if not profile_id:reject('DAILY_PROFILE_REQUIRED','请选择所选模型使用的已有连接配置，不切换日常配置。')
                profile=w.model_api.settings.comparison_profile(profile_id,scope=uow)
                values=deepcopy(profile['values']);reference=profile['credential_ref']
                base_url=values['base_url']
                if not arm.get('profile_id') and key in 'AB' and not base_url.startswith('http://127.0.0.1:'):
                    reject('DAILY_PROFILE_REQUIRED','Sol/Astra须使用已保存的本机CPA连接。')
                if not arm.get('profile_id') and key in 'CD' and base_url!='https://api.deepseek.com/v1':
                    reject('DAILY_PROFILE_REQUIRED','Flash/Pro须使用已保存的DeepSeek官方连接。')
                if values['model_id']!=arm['model_id']:
                    values.update(input_price_per_million=None,output_price_per_million=None,price_currency=None,price_source=None)
                if arm.get('profile_id') and arm['model_id'] not in w.model_api.settings.model_ids(values):
                    reject('DAILY_MODEL_NOT_SAVED','所选模型已从该连接移除，请重新核对本轮。')
            if not arm.get('profile_id') or simulation:
                values.update(reasoning_effort='high',protocol='responses',output_format='json_schema',stream=True)
            values['model_id']=arm['model_id']
            arm.update(profile_id=profile['profile_id'] if profile else None,
              provider=values['provider'],base_url=values['base_url'],protocol=values['protocol'],
              reasoning_effort=values['reasoning_effort'],max_output_tokens=values['max_output_tokens'])
            configurations[key]={'revision':w.model_api.settings.snapshot(scope=uow)['revision'],
              'values':values,'credential_ref':reference,'daily_group_id':id,'daily_simulation':simulation,
              'daily_profile_snapshot':profile,'daily_accepted_models':[arm['model_id']]}
        # Persist registration and all ordinary calls in the caller's single
        # idempotent transaction. Partial enqueue failure rolls everything back.
        for arm in group['arms']:
            w.model_api.enqueue(arm['request_id'],{'retry_of':None,'acknowledge_possible_charge':True},uow=uow,
                 frozen_config=configurations[arm['model_key']],job_kind='daily_model_call')
        group['run']={'mode':command['mode'],'max_calls':n,'registered_at':w.clock()}
        self.store.put(id,group,uow=uow);w._event(uow,'analysis_request',id)
        return self.view(group,scope=uow)

    def view(self,group,*,scope,with_guidance=True):
        w=self.w;arms=[]
        for saved in group['arms']:
            arm={k:deepcopy(v) for k,v in saved.items() if k!='selection_explicit'};result=w.model_api.lookup(arm['request_id'],scope=scope);call=result['call'];v=None
            arm.update(self.store.request_connection(arm['request_id'],scope=scope))
            if result['version_id']:v=w.analysis.get_version(result['version_id'],scope=scope).to_dict()
            state='succeeded' if v else call['state'] if call else 'prepared'
            q=w.analysis.get_request(arm['request_id'],scope=scope).to_dict()
            if not v and not call and q['state']!='awaiting_answer':state='expired'
            if not v and call and call['state']=='failed' and call.get('error_code')=='SCHEMA_INVALID' and call.get('answer'):
                raw=w.model_api.calls.read(call['answer'],scope=scope).decode('utf-8')
                diagnosis=document_diagnosis(raw)
                syntax_detail=diagnosis['detail']
                if syntax_detail:
                    call={**call,'detail':syntax_detail+'（按保存原文只读定位，原失败记录未改写。）'}
                if diagnosis['duplicates']:
                    arm['duplicate_field_diagnostics']=diagnosis['duplicates']
            timing_diagnostics=[]
            if not v and call and call['state']=='failed' and call.get('error_code')=='ENTRY_WINDOW_INSUFFICIENT' and call.get('answer'):
                # Derived display only: original call/error/answer remains untouched.
                frozen=w.analysis.model_request(arm['request_id'],scope=scope,require_pending=False)['request']
                raw=w.model_api.calls.read(call['answer'],scope=scope)
                try:
                    answer=json.loads(raw)
                    timing_diagnostics=explain_entry_timing(answer,frozen,frozen['context'])
                except (ValueError,KeyError,TypeError):
                    # Missing historical detail stays unknown; never invent times.
                    timing_diagnostics=[]
            plans=[p.to_dict() for p in w.plans.list_plans(v['version_id'],scope=scope)] if v else []
            guidance={}
            if v and with_guidance and w.guidance:
                f=w.analysis.get_forecast(v['forecast_id'],scope=scope).to_dict()['forecast']
                # Keep the displayed branch version explicit even if a later
                # round has updated this model's forecast.
                f['current_version_id']=v['version_id']
                g,_=w.guidance.snapshot(scope,forecast=f,plans=plans,include_positions=False)
                guidance={k:g[k] for k in ['action_advice','analysis_refresh','candidate_assessments']}
            arm.update(entry_timing_diagnostics=timing_diagnostics,state=state,version_id=v['version_id'] if v else None,model_call=call,
              answer_model_id=v['origin'].get('model_id') if v else None,answer_source=v['origin']['source'] if v else None,
              available_at=v['available_at'] if v else None,summary=v['answer']['summary'] if v else None,
              horizons=[{'minutes':h['horizon_minutes'],'end_at':h['end_at'],'direction':h['direction'],
                  'low':h['expected_range']['low'],'high':h['expected_range']['high']} for h in v['answer']['horizons']] if v else [],
              plans=plans,guidance=guidance)
            arms.append(arm)
        state=('running' if any(a['state'] in {'queued','waiting','receiving','validating'} for a in arms)
               else 'prepared' if not group['run'] and any(a['state']=='prepared' for a in arms) else 'finished')
        result={k:deepcopy(group[k]) for k in ['group_id','kind','base_group_id','created_at','market_cutoff','window_end',
          'horizon_minutes','source_scope','data_kind','package_kind','template_version','bundle_id','context_sha256','same_question','models']}
        result.update(arms=arms,state=state,run_registered=group['run'] is not None)
        return result

    def get(self,id,*,scope=None):
        if scope is None:
            with self.w.db.read() as tx:return self.get(id,scope=tx)
        return self.view(self.store.get(id,scope=scope),scope=scope)

    def tick_guidance(self):
        w=self.w;branches=[]
        with w.db.read() as scope:
            market=w.market.latest(scope=scope)
            if not market:return
            groups,_=self.store.list(scope=scope,source_scope=market_scope_key(market),limit=1)
            if not groups:return
            for arm in groups[0]['arms']:
                result=w.model_api.lookup(arm['request_id'],scope=scope)
                if result['version_id']:
                    f=w.analysis.get_forecast(arm['forecast_id'],scope=scope).to_dict()['forecast']
                    if f['current_version_id']==result['version_id']:
                        branches.append((f,arm['model_id']+' · '+(arm.get('provider') or '渠道未记录')+' / '+groups[0]['template_version']))
        for f,label in branches:w.guidance.tick(forecast=f,model_label=label)

    def list(self,*,cursor=None,limit=20,scope=None,source_scope=None):
        if scope is None:
            with self.w.db.read() as tx:return self.list(cursor=cursor,limit=limit,scope=tx,source_scope=source_scope)
        rows,next_cursor=self.store.list(scope=scope,source_scope=source_scope,limit=limit,cursor=cursor)
        return {'items':[self.view(g,scope=scope,with_guidance=False) for g in rows],'next_cursor':next_cursor}
