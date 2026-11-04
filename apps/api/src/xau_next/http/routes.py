"""HTTP transport mapped to generated DTOs; public JSON Schema remains authoritative."""

import asyncio
import inspect
from typing import Literal

from fastapi import Body, Depends, Header, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from xau_next.shared.errors import DomainError
from xau_next.shared.preparation_timing import preparation_trace, preparation_span
from xau_next.transport.generated import models


def register_routes(app, workflows, contracts, require_session):
    for path, methods in contracts.api["paths"].items():
        for method, definition in methods.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            if definition["operationId"] == "streamEvents":
                continue
            _register(app, path, method, definition, workflows, contracts, require_session)

    @app.get(
        "/v1/events",
        operation_id="streamEvents",
        dependencies=[Depends(require_session)],
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
        response_class=StreamingResponse,
    )
    async def events(
        request: Request,
        after: int = Query(0, ge=0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID", pattern=r"^[0-9]+$"),
    ):
        first = await run_in_threadpool(workflows.events.replay, after, last_event_id=last_event_id)

        async def stream():
            cursor = int(last_event_id) if last_event_id is not None else after
            pending = first
            heartbeat = 0
            while not await request.is_disconnected():
                for event in pending:
                    contracts.validate("Event", event)
                    yield workflows.events.format_sse(event)
                    cursor = event["id"]
                    if event["type"] == "resync.required":
                        return
                await asyncio.sleep(0.5)
                heartbeat += 1
                if heartbeat >= 20:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
                pending = await run_in_threadpool(workflows.events.replay, cursor)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _register(app, path, method, definition, workflows, contracts, require_session):
    operation = definition["operationId"]
    success = next(k for k in definition["responses"] if k.startswith("2"))
    response_schema = definition["responses"][success]["content"]["application/json"]["schema"]["$ref"].split(
        "/"
    )[-1]
    request_schema = None
    if "requestBody" in definition:
        request_schema = definition["requestBody"]["content"]["application/json"]["schema"]["$ref"].split(
            "/"
        )[-1]
    signature = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request)]
    if request_schema:
        signature.append(
            inspect.Parameter(
                "body",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=getattr(models, request_schema),
                default=Body(...),
            )
        )
    for parameter in definition.get("parameters", []):
        schema = parameter["schema"]
        if "$ref" in schema:
            schema = contracts.api["components"]["schemas"][schema["$ref"].split("/")[-1]]
        # Id is an external reference; semantic validation additionally runs in the boundary validator.
        typ = {"integer": int, "boolean": bool}.get(schema.get("type"), str)
        if "enum" in schema:
            typ = Literal[tuple(schema["enum"])]
        required = parameter.get("required", False)
        default = ... if required else schema.get("default", None)
        if default is None:
            typ = typ | None
        kwargs = {
            name: schema[key]
            for key, name in [
                ("minimum", "ge"),
                ("maximum", "le"),
                ("minLength", "min_length"),
                ("maxLength", "max_length"),
                ("pattern", "pattern"),
            ]
            if key in schema
        }
        loc = {"path": Path, "query": Query, "header": Header}[parameter["in"]]
        signature.append(
            inspect.Parameter(
                parameter["name"].lower().replace("-", "_"),
                inspect.Parameter.KEYWORD_ONLY,
                annotation=typ,
                default=loc(default, alias=parameter["name"], **kwargs),
            )
        )

    async def endpoint(request: Request, **kwargs):
        with preparation_trace(operation):
            raw = await request.json() if request_schema else None
            if request_schema:
                try:
                    contracts.validate(request_schema, raw)
                except DomainError:
                    if operation == "setModelCredential":
                        raise DomainError("SCHEMA_INVALID", "密钥输入格式不符合要求。", 422) from None
                    raise
            params = {k: v for k, v in kwargs.items() if k != "body" and v is not None}
            key = params.pop("idempotency_key", None)
            def dispatch():
                with preparation_span("worker_execution"):
                    return workflows.dispatch(operation, raw, params, key, str(request.url.path))
            with preparation_span("worker_wait_and_execution"):
                status, result = await run_in_threadpool(dispatch)
            contracts.validate(response_schema, result)
            # Explicit generated DTO mapping runs at both request and response boundaries.
            result = (
                getattr(models, response_schema)
                .model_validate(result)
                .model_dump(mode="json", exclude_unset=True)
            )
            return JSONResponse(result, status_code=status)

    endpoint.__signature__ = inspect.Signature(signature)
    endpoint.__name__ = operation
    app.add_api_route(
        path,
        endpoint,
        methods=[method.upper()],
        operation_id=operation,
        response_model=getattr(models, response_schema),
        status_code=int(success),
        dependencies=[] if operation == "getHealth" else [Depends(require_session)],
        responses={
            "default": {
                "model": models.Problem,
                "description": "规范化错误",
                "content": {"application/problem+json": {}},
            }
        },
    )
