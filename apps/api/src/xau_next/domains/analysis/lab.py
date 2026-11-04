"""Independent research records; never publish forecasts/plans or daily experience."""
import hashlib
from copy import deepcopy
from contextlib import nullcontext

from xau_next.shared.errors import DomainError
from xau_next.shared.values import new_id, utc_now

MODELS={"A":"gpt-6.1-sol","B":"gpt-6-astra","C":"deepseek-flash","D":"deepseek-v4-pro"}
BUNDLE_FIELDS=("bundle_id","captured_at","month","scope","state","data_kind","sources","events","quotes","limitations")
SUMMARY_FIELDS=("comparison_id","created_at","market_cutoff","package_kind","purpose","state","data_kind",
                "source_bundle_id","packet_sha256","model_calls_sent","arms","message")
EXECUTION_FIELDS=('mode','state','outcome','job_id','group_call_limit','attempts_reserved','real_calls_sent',
 'simulation_calls_sent','wire_sha256','started_at','ended_at','message','scores','completed_horizons',
 'simulation_advanced','billing_status','error_code')


class AnalysisLabService:
    def __init__(self,db,files,*,clock=utc_now,history=None):
        self.db,self.files,self.clock=db,files,clock
        self.docs=db.documents("analysis")
        self.history=history or {"attempts":58,"qualified_answers":34,"failed":22,"unknown":2,"winner":None,
            "note":"已封存的历史批次，条件不同，不能混算胜率；回答合格不等于预测命中。"}

    def save_bundle(self,value,raws,*,uow):
        stored=deepcopy(value);stored["bundle_id"]=new_id();stored["raw_attachments"]={}
        for name,raw in raws.items():
            if raw is not None:
                attachment=self.files.prepare(raw,media_type="application/octet-stream")
                ref=self.files.register(uow,attachment)
                stored["raw_attachments"][name]=ref.id
        self.docs.insert(uow,"research_bundle",stored["bundle_id"],stored,state=stored["state"])
        return self.bundle_view(stored)

    @staticmethod
    def bundle_view(stored):
        result={k:deepcopy(stored[k]) for k in BUNDLE_FIELDS}
        result["sources"]=[{k:row.get(k) for k in ("source_id","url","captured_at","status","content_sha256","message")} for row in result["sources"]]
        result["jin10_event_count"]=sum(e["source_id"].startswith("jin10") for e in result["events"])
        result["rated_event_count"]=sum(e["importance"] is not None for e in result["events"])
        result.update(input_revision=stored.get('version','macro-research/1'),
          official_releases=deepcopy(stored.get('official_releases',[])),news=deepcopy(stored.get('news',[])),
          reference_rates=deepcopy(stored.get('reference_rates',[])),quote_series_count=len(stored.get('quote_series',[])),
          search=deepcopy(stored.get('search')))
        return result

    def bundle(self,id,*,scope):
        row=self.docs.get(scope,"research_bundle",id)
        if row is None:raise DomainError("RESEARCH_BUNDLE_NOT_FOUND","尚无这份资料，请先更新公开资料。",404)
        return row

    def status(self):
        with self.db.read() as scope:
            bundles=self.docs.list(scope,"research_bundle",limit=1)
            records=self.docs.list(scope,"analysis_comparison",limit=50)
            return {"daily_package":"manual-analysis/14","improved_package":"manual-analysis/23",
                "paid_execution_enabled":False,"authorized_calls_remaining":0,
                "bundle":self.bundle_view(bundles[0]) if bundles else None,
                "comparisons":[self._comparison_view(r,scope=scope,summary=True) for r in records],"historical":deepcopy(self.history),
                "message":"日常分析包保持；改进包与比较记录独立保存。当前没有新收费授权，不能发送模型。"}

    def save_comparison(self,request,*,package_kind,purpose,bundle_id,uow):
        packet=request["packet_text"]
        checksum=hashlib.sha256(packet.encode()).hexdigest()
        row={"comparison_id":new_id(),"created_at":self.clock(),"market_cutoff":request["market_cutoff"],
            "package_kind":package_kind,"purpose":purpose,"state":"prepared",
            "data_kind":"SYNTHETIC" if request["context"]["market"]["source_kind"]=="synthetic" else "REAL",
            "source_bundle_id":bundle_id,"packet_sha256":checksum,"model_calls_sent":0,
            "arms":[{"arm":a,"model_id":m,"reasoning_effort":"high","state":"not_sent","packet_sha256":checksum}
                    for a,m in MODELS.items()] if purpose=="comparison" else [],
            "message":"资料已保存，模型尚未发送；须另行明确额度和样本后才能执行收费比较。",
            "packet_text":packet,"request_id":request["request_id"],"frozen_request":request}
        row['window_risks']=[]
        if request.get('context',{}).get('macro_research',{}).get('version')=='macro-research/2':
            from .macro_research import project_macro
            row['window_risks']=project_macro(request['context']['macro_research'],cutoff=request['market_cutoff'],window_end=request['window_end'])['window_risks']
        self.docs.insert(uow,"analysis_comparison",row["comparison_id"],row,state="prepared")
        return {**{k:row[k] for k in (*SUMMARY_FIELDS,"packet_text","request_id","window_risks")},'execution':None}

    def comparison(self,id):
        with self.db.read() as scope:
            row=self.internal_comparison(id,scope=scope)
            return self._comparison_view(row,scope=scope)

    def internal_comparison(self,id,*,scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            row=self.docs.get(tx,'analysis_comparison',id)
        if row is None:raise DomainError("COMPARISON_NOT_FOUND","未找到比较资料。",404)
        return row

    def execution(self,id,*,scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            return self.docs.get(tx,'comparison_execution',id)

    def executions(self,states,*,scope=None):
        with nullcontext(scope) if scope else self.db.read() as tx:
            return [r for state in states for r in self.docs.list(tx,'comparison_execution',state=state,limit=1000)]

    def put_execution(self,id,value,*,uow):
        prior=self.execution(id,scope=uow)
        data={k:deepcopy(v) for k,v in value.items() if k not in {'id','revision','recorded_at'}}
        if prior:return self.docs.update(uow,'comparison_execution',id,data,prior['revision'],state=data['state'],parent_id=id)
        return self.docs.insert(uow,'comparison_execution',id,data,state=data['state'],parent_id=id)

    def _comparison_view(self,row,*,scope,summary=False):
        result={k:deepcopy(row[k]) for k in SUMMARY_FIELDS}
        e=self.execution(row['comparison_id'],scope=scope)
        if e:
            result.update(state=e['state'],model_calls_sent=e['real_calls_sent'],arms=deepcopy(e['arms']),message=e['message'])
        if not summary:
            result.update(packet_text=row['packet_text'],request_id=row['request_id'],window_risks=deepcopy(row.get('window_risks',[])),
              execution={k:deepcopy(e[k]) for k in EXECUTION_FIELDS} if e else None)
        return result

    def deny_run(self,id):
        self.comparison(id)
        raise DomainError("COMPARISON_BUDGET_REQUIRED","本阶段只准备资料，剩余新授权0次；未发送、未读取密钥。",409)
