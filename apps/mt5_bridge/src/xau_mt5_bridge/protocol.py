from datetime import UTC, datetime
from uuid import uuid4

OPERATIONS = frozenset({"health", "identity", "snapshot", "history", "profit", "margin"})


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("时间必须为 UTC Z 格式")
    return datetime.fromisoformat(value)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def id() -> str:
    return uuid4().hex


def status(at: str, state="available", reasons=(), observed_at=None) -> dict:
    return {
        "state": state,
        "observed_at": observed_at if observed_at is not None else at,
        "captured_at": at,
        "reasons": list(reasons),
    }


class BridgeFailure(Exception):
    def __init__(self, code: str, detail: str, retryable: bool = False):
        super().__init__(detail)
        self.code, self.detail, self.retryable = code, detail, retryable


def validate_request(request: dict) -> None:
    if not isinstance(request, dict) or set(request) != {
        "protocol_version",
        "request_id",
        "operation",
        "params",
        "deadline",
    }:
        raise BridgeFailure("INVALID_REQUEST", "桥接请求字段不符合协议")
    if request["protocol_version"] != "1" or request["operation"] not in OPERATIONS:
        raise BridgeFailure("OPERATION_NOT_ALLOWED", "此桥仅允许明确列出的只读操作")
    if not isinstance(request["params"], dict) or not isinstance(request["request_id"], str):
        raise BridgeFailure("INVALID_REQUEST", "桥接请求参数格式无效")
    if utc(request["deadline"]) <= utc(now()):
        raise BridgeFailure("DEADLINE_EXCEEDED", "读取请求已经超时", True)
