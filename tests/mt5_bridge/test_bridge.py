import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from xau_mt5_bridge.protocol import BridgeFailure, now, stamp, utc, validate_request
from xau_next.domains.market.bridge import BridgeClient, BridgeError

ROOT = Path(__file__).resolve().parents[2]


def client():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "apps/mt5_bridge/src")}
    return BridgeClient([sys.executable, "-m", "xau_mt5_bridge", "--mode", "synthetic"], environment=env)


def test_ac02_protocol_roundtrip_and_read_only_allowlist():
    bridge = client()
    try:
        response = bridge.request("identity", {})
        schema = json.loads((ROOT / "contracts/mt5-bridge.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(response)
        assert response["source_identity"]["read_only"] is True
        with pytest.raises(BridgeError, match="只读"):
            bridge.request("order_send", {})
        request = {
            "protocol_version": "1",
            "request_id": "bad",
            "operation": "order_send",
            "params": {},
            "deadline": stamp(utc(now()) + timedelta(seconds=10)),
        }
        with pytest.raises(BridgeFailure, match="只读"):
            validate_request(request)
    finally:
        bridge.close()


def test_ac22_timeout_kills_only_bridge_and_next_request_restarts():
    bridge = BridgeClient([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.05)
    with pytest.raises(BridgeError) as failure:
        bridge.request("health", {})
    assert failure.value.code == "BRIDGE_TIMEOUT"
    assert bridge._process is None
    bridge.command = [sys.executable, "-m", "xau_mt5_bridge", "--mode", "synthetic"]
    bridge.environment = {**os.environ, "PYTHONPATH": str(ROOT / "apps/mt5_bridge/src")}
    bridge.timeout = 2
    try:
        assert bridge.request("health", {})["result"]["status"] == "ok"
    finally:
        bridge.close()
