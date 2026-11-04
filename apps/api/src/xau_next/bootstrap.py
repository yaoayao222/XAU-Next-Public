"""Application composition, local authentication, and lifecycle."""

import hmac
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from xau_next import __version__
from xau_next.domains.analysis.public import AnalysisService, ModelCallStore, AnalysisLabService
from xau_next.domains.plans.public import PlansService
from xau_next.domains.review.public import ReviewService
from xau_next.domains.settings.public import ModelSettingsService, SettingsService
from xau_next.http.contracts import Contracts, problem
from xau_next.http.routes import register_routes
from xau_next.platform.credentials.public import WindowsCredentials
from xau_next.platform.db.public import Database
from xau_next.platform.events.public import EventStore
from xau_next.platform.files.public import AttachmentStore
from xau_next.platform.idempotency.public import IdempotencyStore
from xau_next.platform.jobs.public import JobStore
from xau_next.platform.model_gateway.public import Gateway
from xau_next.platform.notifications.public import NotificationStore
from xau_next.shared.errors import DomainError
from xau_next.shared.values import utc_now
from xau_next.workflows.model_api import ModelCoordinator
from xau_next.workflows.service import Workflows


def create_app(
    *,
    data_root=None,
    token=None,
    source=None,
    contracts_root=None,
    clock=utc_now,
    start_worker=True,
    bridge_command=None,
    notification_sink=None,
    allow_desktop_notifications=None,
    model_gateway=None,
    credential_store=None,
    research_collector=None,
):
    from xau_next.domains.market.public import MarketService
    from xau_next.domains.portfolio.public import PortfolioService

    root = Path(__file__).resolve().parents[4]
    data_root = Path(
        data_root
        or os.environ.get("XAU_NEXT_DATA_ROOT")
        or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "XAU-Next" / "data"
    )
    config_path = data_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    source = source or os.environ.get("XAU_NEXT_SOURCE") or config.get("source", "mt5")
    token = token or os.environ.get("XAU_NEXT_SESSION_TOKEN") or secrets.token_urlsafe(32)
    contracts = Contracts(
        Path(contracts_root or os.environ.get("XAU_NEXT_CONTRACT_ROOT") or root / "contracts")
    )
    db = Database(data_root, clock=clock)
    db.initialize()
    files = AttachmentStore(db)
    events = EventStore(db)
    jobs = JobStore(db, events)
    settings = SettingsService(db, clock)
    settings.initialize()
    analysis = AnalysisService(db, files, contracts.answer, clock)
    plans = PlansService(db, clock)
    market = MarketService(db, files, bridge_command, mode=source, clock=clock)
    portfolio = PortfolioService(db, clock=clock)
    workflows = Workflows(
        db=db,
        settings=settings,
        analysis=analysis,
        plans=plans,
        market=market,
        portfolio=portfolio,
        review=ReviewService(db, clock),
        jobs=jobs,
        events=events,
        idempotency=IdempotencyStore(db),
        contract_version=contracts.api["info"]["version"],
        notifications=NotificationStore(db, sink=notification_sink,
            allow_desktop=(source == "mt5") if allow_desktop_notifications is None else allow_desktop_notifications),
        model_api=ModelCoordinator(db=db, analysis=analysis,
            settings=ModelSettingsService(db,credential_store or WindowsCredentials(),clock),
            calls=ModelCallStore(db,files,clock),jobs=jobs,gateway=model_gateway or Gateway(),clock=clock),
        clock=clock,
    )
    from xau_next.workflows.analysis_lab import AnalysisLabCoordinator
    workflows.analysis_lab=AnalysisLabCoordinator(db=db,lab=AnalysisLabService(db,files,clock=clock),analysis=analysis,
        market=market,portfolio=portfolio,settings=settings,review=workflows.review,clock=clock,collector=research_collector)
    from xau_next.workflows.lab_comparison import ComparisonCoordinator
    workflows.analysis_lab.comparison=ComparisonCoordinator(lab=workflows.analysis_lab.lab,db=db,jobs=jobs,events=events,
        model_api=workflows.model_api,market=market,clock=clock,stopped=workflows._stop.is_set)
    from xau_next.workflows.daily_analysis import DailyAnalysisCoordinator
    workflows.daily=DailyAnalysisCoordinator(workflows)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            workflows.start()
        yield
        workflows.stop()
        db.close()

    app = FastAPI(title="XAU Next", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.workflows = workflows
    app.state.contracts = contracts
    origins = os.environ.get(
        "XAU_NEXT_WEB_ORIGINS", config.get("web_origins", "http://127.0.0.1:8766")
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID"],
    )
    bearer = HTTPBearer(auto_error=False)

    def require_session(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is None or not hmac.compare_digest(credentials.credentials, token):
            raise DomainError("UNAUTHORIZED", "会话已失效，请从本机启动入口重新打开工作台。", 401)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse(problem(exc), status_code=exc.status, media_type="application/problem+json")

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        error = DomainError(
            "SCHEMA_INVALID",
            "请求字段格式不符合规范。",
            422,
            [
                {
                    "path": "/" + "/".join(map(str, e["loc"])),
                    "code": "SCHEMA_INVALID",
                    "message": "字段类型、范围或必填项不符合规范。",
                }
                for e in exc.errors()[:30]
            ],
        )
        return JSONResponse(problem(error), status_code=422, media_type="application/problem+json")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logging.getLogger("xau_next").exception("Request failed at %s", request.url.path, exc_info=exc)
        error = DomainError("INTERNAL_ERROR", "本机服务遇到错误，请查看数据目录中的诊断日志。", 500)
        return JSONResponse(problem(error), status_code=500, media_type="application/problem+json")

    register_routes(app, workflows, contracts, require_session)
    return app
