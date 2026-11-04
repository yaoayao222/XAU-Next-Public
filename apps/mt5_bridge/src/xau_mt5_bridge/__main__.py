import argparse
import json
import sys

from .protocol import BridgeFailure, now, validate_request
from .synthetic import SyntheticSource


def serve(source) -> None:
    for line in sys.stdin:
        request = {}
        try:
            if len(line) > 2 * 1024 * 1024:
                raise BridgeFailure("REQUEST_TOO_LARGE", "桥接请求超过大小限制")
            request = json.loads(line)
            validate_request(request)
            result = source.handle(request["operation"], request["params"])
            response = {
                "protocol_version": "1",
                "request_id": request["request_id"],
                "captured_at": now(),
                "source_identity": source.identity(),
                "result": result,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 -- the protocol boundary must isolate SDK failures per request.
            failure = (
                exc
                if isinstance(exc, BridgeFailure)
                else BridgeFailure("BRIDGE_ERROR", "只读桥读取失败：" + str(exc)[:500], True)
            )
            response = {
                "protocol_version": "1",
                "request_id": request.get("request_id", "invalid-request")
                if isinstance(request, dict)
                else "invalid-request",
                "captured_at": now(),
                "source_identity": None,
                "result": None,
                "error": {"code": failure.code, "detail": failure.detail, "retryable": failure.retryable},
            }
        sys.stdout.write(
            json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        )
        sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="XAU Next 只读桥")
    parser.add_argument("--mode", choices=("mt5", "synthetic"), default="mt5")
    parser.add_argument("--terminal-path")
    args = parser.parse_args()
    if args.mode == "synthetic":
        source = SyntheticSource()
    else:
        from .native import MT5Source

        source = MT5Source(args.terminal_path)
    try:
        serve(source)
    finally:
        if hasattr(source, "close"):
            source.close()


if __name__ == "__main__":
    main()
