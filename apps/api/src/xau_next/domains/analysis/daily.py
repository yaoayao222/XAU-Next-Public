"""Analysis-owned round identities; normal requests/versions retain original evidence."""
from contextlib import nullcontext
from copy import deepcopy
from xau_next.shared.errors import DomainError

MODELS={'A':'gpt-6.1-sol','B':'gpt-6-astra','C':'deepseek-flash','D':'deepseek-v4-pro'}


class DailyAnalysisStore:
    def __init__(self,db):self.db=db;self.docs=db.documents('analysis')

    def get(self,id,*,scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:row=self.docs.get(tx,'daily_group',id)
        if not row:raise DomainError('DAILY_GROUP_NOT_FOUND','没有找到这轮分析。',404)
        return row

    def put(self,id,value,*,uow):
        prior=self.docs.get(uow,'daily_group',id)
        value={k:deepcopy(v) for k,v in value.items() if k not in {'id','revision','recorded_at'}}
        if prior:return self.docs.update(uow,'daily_group',id,value,prior['revision'],parent_id=value['source_scope'])
        return self.docs.insert(uow,'daily_group',id,value,parent_id=value['source_scope'])

    def list(self,*,scope=None,source_scope=None,limit=20,cursor=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            rows=self.docs.list(tx,'daily_group',parent_id=source_scope,limit=limit+1,cursor=cursor)
            return rows[:limit],rows[limit-1]['id'] if len(rows)>limit else None

    def request_metadata(self,request_id,*,scope):
        return self.docs.get(scope,'daily_request',request_id)

    def request_connection(self,request_id,*,scope):
        pointer=self.docs.get(scope,'model_latest_call',request_id)
        call=self.docs.get(scope,'model_call',pointer['call_id']) if pointer else None
        if not call:return {}
        config=call.get('configuration',{});values=config.get('values',{})
        result={k:values.get(k) for k in ['provider','base_url','protocol','reasoning_effort','max_output_tokens']}
        profile=config.get('daily_profile_snapshot')
        if profile:result['profile_id']=profile['profile_id']
        return result

    def performance_records(self,*,scope):
        """No cross-domain queries; all existing versions and unfilled branches retained."""
        records=[];seen=set();cursor=None
        def latest_call(request_id):
            pointer=self.docs.get(scope,'model_latest_call',request_id)
            row=self.docs.get(scope,'model_call',pointer['call_id']) if pointer else None
            if not row:return None
            call=deepcopy(row['record'])
            # Only explicitly allowed, non-secret generation settings leave
            # this store. Never expose credential references or private run data.
            values=row.get('configuration',{}).get('values',{})
            call['_performance_configuration']={key:values.get(key) for key in (
                'base_url','provider','protocol','reasoning_effort','output_format','stream',
                'timeout_seconds','max_output_tokens','max_response_bytes','max_answer_bytes','chat_token_parameter')}
            snapshot=row.get('configuration',{}).get('daily_profile_snapshot')
            if snapshot:call['_performance_configuration']['profile_id']=snapshot['profile_id']
            return call
        while True:
            rows=self.docs.list(scope,'version',limit=1000,cursor=cursor)
            for row in rows:
                q=self.docs.get(scope,'request',row['request_id'])
                if not q or q.get('holding_review_order'):continue
                meta=self.request_metadata(q['request_id'],scope=scope);group=self.get(meta['group_id'],scope=scope) if meta else None
                model=(next(a['model_id'] for a in group['arms'] if a['model_key']==meta['model_key']) if meta and row['origin']['source']=='model_api'
                       else row['origin'].get('model_id') or 'unknown')
                records.append({'request':q,'version':row,'model_id':model,'group':group,'arm':meta['model_key'] if meta else None,
                    'call':latest_call(q['request_id'])})
                seen.add(q['request_id'])
            if len(rows)<1000:break
            cursor=rows[-1]['id']
        cursor=None
        while True:
            rows=self.docs.list(scope,'daily_group',limit=1000,cursor=cursor)
            for group in rows:
                for arm in group['arms']:
                    if arm['request_id'] in seen:continue
                    q=self.docs.get(scope,'request',arm['request_id'])
                    call=latest_call(arm['request_id'])
                    records.append({'request':q,'version':None,'model_id':arm['model_id'],'group':group,'arm':arm['model_key'],'call':call})
                    seen.add(q['request_id'])
            if len(rows)<1000:break
            cursor=rows[-1]['id']
        cursor=None
        while True:
            rows=self.docs.list(scope,'request',limit=1000,cursor=cursor)
            for q in rows:
                if q['request_id'] in seen or q.get('holding_review_order'):continue
                call=latest_call(q['request_id'])
                records.append({'request':q,'version':None,'model_id':call['model_requested'] if call else 'unknown',
                    'group':None,'arm':None,'call':call})
            if len(rows)<1000:break
            cursor=rows[-1]['id']
        return records
