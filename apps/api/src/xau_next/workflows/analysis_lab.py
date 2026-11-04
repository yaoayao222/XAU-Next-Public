"""Compose public research and analysis preparation, never model execution."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta

from xau_next.domains.analysis.public import economic_for_bundle
from xau_next.platform.economic_calendar.multi_source import fetch_multi_source_research
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc
from xau_next.shared.preparation_timing import preparation_span, preparation_fact


class AnalysisLabCoordinator:
    def __init__(self,*,db,lab,analysis,market,portfolio,settings,review,clock,collector=None):
        self.db,self.lab,self.analysis,self.market,self.portfolio,self.settings,self.review=(db,lab,analysis,market,portfolio,settings,review)
        self.clock=clock
        self.collector=collector or (lambda month:fetch_multi_source_research(month=month,clock=clock))

    def capture(self,month):
        return self.collector(month)

    def prepare(self,body,*,command=None,scope=None,daily=False):
        with nullcontext(scope) if scope else self.db.read() as scope:
            with preparation_span("frozen_market_read"):
                market=self.market.get_snapshot(body["market_snapshot_id"],scope=scope)
            if market is None:raise DomainError("SNAPSHOT_NOT_FOUND","请先取得行情快照。",404)
            preparation_fact(market_snapshot_id=body["market_snapshot_id"],market_cutoff=market.to_dict()["market_cutoff"])
            with preparation_span("capture_binding_read"):
                binding=self.market.capture_binding(body["market_snapshot_id"],scope=scope)
            if not binding:raise DomainError("CAPTURE_BINDING_UNAVAILABLE","缺少同批账户资料，未准备比较。",409)
            with preparation_span("portfolio_and_settings_read"):
                portfolio=self.portfolio.get_snapshot(binding["portfolio_snapshot_id"],scope=scope)
                settings=self.settings.get_snapshot(scope=scope)
            command=command or {"kind":"new","market_snapshot_id":body["market_snapshot_id"],"forecast_id":None,"base_version_id":None,
                     "horizon_minutes":[30,60,180]}
            with preparation_span("experience_aggregate"):
                experience=self.review.get_context(market.to_dict()["market_cutoff"],scope=scope,include_candidates=True)
            with preparation_span("experience_predictions"):
                predictions=self.analysis.experience_predictions(experience,scope=scope)
            with preparation_span("experience_selection"):
                experience=self.review.enrich_context(experience,predictions,
                    selection_target=self.analysis.experience_target(command,market,settings,scope=scope))
            research=None
            if body["package_kind"] in {"improved","candidate24","candidate25","candidate26","candidate27","candidate28"}:
                if not body.get("bundle_id"):raise DomainError("RESEARCH_REQUIRED","改进包需要先取得宏观资料；缺失来源会明确保留。",409)
                with preparation_span("research_bundle_read"):
                    bundle=self.lab.bundle(body["bundle_id"],scope=scope)
                expected="SYNTHETIC" if market.to_dict()["source_kind"]=="synthetic" else "REAL"
                if bundle["data_kind"]!=expected:raise DomainError("RESEARCH_KIND_MISMATCH","真实和合成资料不能混用。",409)
                economic=economic_for_bundle(bundle)
                at=parse_utc(market.to_dict()["market_cutoff"])
                # Keep the complete month in the saved bundle; only this bounded
                # window and the two official historical reference rows enter a packet.
                economic["events"]=[e for e in economic["events"] if
                    at-timedelta(hours=6)<=parse_utc(e["scheduled_at"])<=at+timedelta(minutes=max(command.get('horizon_minutes') or [720]))
                    or (economic.get("actual_supplement") and "UoM" in e["title"])]
                research={"economic_context":economic,"research_policy":{"mode":"shared_calendar"},"macro_research":deepcopy(bundle)}
            with preparation_span("common_context_build"):
                return self.analysis.create_request(command,market=market,portfolio=portfolio,settings=settings,experience=experience,
                capture_binding=binding,uow=scope,prompt_version="manual-analysis/28" if body["package_kind"]=="candidate28" else "manual-analysis/27" if body["package_kind"]=="candidate27" else "manual-analysis/26" if body["package_kind"]=="candidate26" else "manual-analysis/25" if body["package_kind"]=="candidate25" else "manual-analysis/24" if body["package_kind"]=="candidate24" else "manual-analysis/23" if research else "manual-analysis/14",
                research_context=research,persist=False,daily=daily).to_dict()
