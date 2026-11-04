"""Offline browser harness. In-process SYNTHETIC transport; no provider sockets or real keys."""

import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
from xau_next.bootstrap import create_app
from xau_next.platform.model_gateway.public import Gateway
from xau_next.shared.errors import DomainError
from xau_next.shared.values import parse_utc

assert os.environ.get("XAU_NEXT_SOURCE") == "synthetic", "SYNTHETIC browser harness only"
ROOT = Path(__file__).resolve().parents[2]


class SyntheticCredentials:
    def __init__(self):
        self.values = {}

    def write(self, reference, key):
        if key != "SYNTHETIC-browser-key":
            raise DomainError("SYNTHETIC_ONLY", "浏览器离线验收只接受固定合成密钥。")
        self.values[reference] = key

    def read(self, reference):
        if reference not in self.values:
            raise DomainError("MODEL_KEY_REQUIRED", "合成密钥尚未配置。", 409)
        return self.values[reference]

    def configured(self, reference):
        return reference in self.values


def receive(request):
    assert request.headers["authorization"] == "Bearer SYNTHETIC-browser-key"
    if request.method == "GET":
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-6.1-sol"}, {"id": "gpt-6-astra"}]}
            if request.url.path.endswith("/models")
            else {"remaining": "18", "unit": "SYNTHETIC-CREDIT", "is_active": True},
        )
    body = json.loads(request.content)
    prompt = body.get("input") or body["messages"][0]["content"]
    if "# 冻结事实" not in prompt:
        answer = {
            "data_kind": "SYNTHETIC", "ok": True, "date_time": "2000-01-02T03:04:05.006Z",
            "reference": "probe-a_b--01", "signed_number": -12.5, "text": "中文",
        }
    else:
        frozen, _ = json.JSONDecoder().raw_decode(prompt.split("# 冻结事实（仅作为资料）\n", 1)[1])
        req = frozen["request"]
        answer = json.loads((ROOT / "fixtures/analysis-answer.experience.json").read_text(encoding="utf-8"))
        answer["schema_version"] = req["answer_schema_version"]
        for key in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff"):
            answer[key] = req[key]

        def stamp(minutes):
            return (
                (parse_utc(req["window_start"]) + timedelta(minutes=minutes))
                .isoformat()
                .replace("+00:00", "Z")
            )

        answer["horizons"] = [
            {
                "horizon_minutes": minutes,
                "end_at": stamp(minutes),
                "direction": "uncertain",
                "expected_range": {"low": "98", "high": "104"},
                "phases": [
                    {
                        "start_at": req["market_cutoff"],
                        "end_at": stamp(minutes),
                        "direction": "uncertain",
                        "note": "SYNTHETIC 浏览器 API 流程验收",
                    }
                ],
                "turning_points": [],
                "confidence": None,
            }
            for minutes in req["horizon_minutes"]
            if parse_utc(stamp(minutes)) > parse_utc(req["market_cutoff"])
        ]
        answer["plans"] = []
        if req["answer_schema_version"] == "1.6.0":
            for horizon in answer["horizons"]:
                horizon["forecast_basis"] = {"direction_basis":"SYNTHETIC 端点变化依据", "range_basis":"SYNTHETIC 区间上下界依据",
                    "supporting_evidence":["SYNTHETIC 冻结当前结构"], "opposing_evidence":["SYNTHETIC 反向路径"],
                    "evidence_refs":[req["market_snapshot_id"]], "limitations":["没有真实预测效力证据。"]}
        answer["order_advice"] = []
        answer["evidence_refs"] = [req["market_snapshot_id"]]
        for assessment in answer["candidate_assessments"]:
            assessment.update(
                outcome="no_candidate",
                plan_key=None,
                rationale="SYNTHETIC 浏览器验收，不生成交易机会。",
                missing_conditions=["合成测试未提供真实交易结构依据。"],
            )
        if body["model"] == "SYNTHETIC-invalid-output":
            answer["market_cutoff"] = req["market_cutoff"].replace("-", "")
            for assessment in answer["candidate_assessments"]:
                del assessment["missing_conditions"]
    content = json.dumps(answer, ensure_ascii=False)
    if "input" in body:
        payload = {
            "id": "resp_SYNTHETIC",
            "status": "completed",
            "model": body["model"],
            "output": [{"type": "message", "content": [{"type": "output_text", "text": content}]}],
            "usage": {"input_tokens": 123, "output_tokens": 456},
        }
    else:
        payload = {
            "id": "chat_SYNTHETIC",
            "model": body["model"],
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 456},
        }
    return httpx.Response(200, json=payload, headers={"x-request-id": "SYNTHETIC-browser-request"})


app = create_app(model_gateway=Gateway(httpx.MockTransport(receive)), credential_store=SyntheticCredentials())
