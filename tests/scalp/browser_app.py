import hmac
import os
from pathlib import Path
from threading import Lock
from typing import Literal

import httpx
from fastapi import Header, HTTPException
from pydantic import BaseModel

from xau_next.bootstrap import create_app
from xau_next.platform.model_gateway.public import Gateway
from xau_next.shared.values import parse_utc


assert os.environ.get("XAU_NEXT_SOURCE") == "synthetic"
DATA_ROOT = Path(os.environ["XAU_NEXT_DATA_ROOT"]).resolve()
assert ("browser-r16" in DATA_ROOT.parts or DATA_ROOT.name.startswith("xau-next-e2e-") or
        ("scalp-browser" in DATA_ROOT.parts and any(p.startswith("quality-path-alignment-r17-") for p in DATA_ROOT.parts)))
TOKEN = os.environ["XAU_NEXT_SESSION_TOKEN"]
LOCK = Lock()


class Clock:
    value = "2026-10-12T02:00:00Z"

    def __call__(self):
        return self.value


class SyntheticCredentials:
    def configured(self, reference):
        return False

    def read(self, reference):
        raise AssertionError("No model calls are authorized in scalp browser tests")

    def write(self, reference, key):
        raise AssertionError("No credentials are written in scalp browser tests")


def reject_model_call(request):
    raise AssertionError("Scalp browser tests must not call a model")


clock = Clock()
app = create_app(data_root=DATA_ROOT, source="synthetic", clock=clock, start_worker=False,
    model_gateway=Gateway(httpx.MockTransport(reject_model_call)), credential_store=SyntheticCredentials())
workflows = app.state.workflows
source_state = {"bid": "99.98", "ask": "100.00", "position": False}
original_request = workflows.market._bridge.request


def source_request(operation, params):
    response = original_request(operation, params)
    if operation == "snapshot":
        result = response["result"]
        result["market"]["quote"].update(bid=source_state["bid"], ask=source_state["ask"])
        if source_state["position"]:
            result["portfolio"]["orders"] = [{
                "order": {"account_scope_id": "synthetic-account", "kind": "position", "broker_id": "SYNTHETIC-existing-position"},
                "symbol": "XAUUSD", "side": "long", "volume_lots": "0.01", "entry_price": "101.00",
                "stop_loss": "100.50", "take_profit": "104.00", "event_time": "2026-10-12T01:50:00Z",
                "captured_at": clock.value, "plan_id": None,
            }]
        result["raw"]["scalp_browser_source"] = {"data_kind": "SYNTHETIC", **source_state}
    return response


workflows.market._bridge.request = source_request


class SourceUpdate(BaseModel):
    at: str
    bid: str
    ask: str
    position: bool = False


class ProcessRequest(BaseModel):
    kind: Literal["market_refresh", "answer_import"]


def authorize(authorization):
    if not authorization or not hmac.compare_digest(authorization, "Bearer " + TOKEN):
        raise HTTPException(status_code=401, detail="Synthetic session required")


@app.post("/__synthetic/source")
def update_source(body: SourceUpdate, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with LOCK:
        if parse_utc(body.at) < parse_utc(clock.value):
            raise HTTPException(status_code=422, detail="Synthetic clock cannot move backwards")
        clock.value = body.at
        source_state.update(bid=body.bid, ask=body.ask, position=body.position)
        return {"data_kind": "SYNTHETIC", "at": clock.value, **source_state}


@app.post("/__synthetic/process")
def process_pending(body: ProcessRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    with LOCK:
        processed = workflows.process_once(kind=body.kind)
        workflows.guidance.tick()
        return {"data_kind": "SYNTHETIC", "processed": processed, "workspace": workflows.workspace()}
