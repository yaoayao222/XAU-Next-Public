"""Separate holding opinions from entry forecasts, preserving every raw answer."""

from xau_next.shared.holding import holding_key, holding_fingerprint
from xau_next.shared.values import parse_utc


class HoldingReviews:
    def holding_review(self, order, opened_at, *, scope):
        return self._docs.get(scope, "holding_review", holding_key(order, opened_at))

    def pending_holding_review(self, order, opened_at, *, scope):
        pointer = self._docs.get(scope, "holding_pending", holding_key(order, opened_at))
        if not pointer or pointer["fingerprint"] != holding_fingerprint(order, opened_at):
            return None
        row = self._docs.get(scope, "request", pointer["request_id"])
        if not row:
            return None
        result = self._request_snapshot(scope, row).to_dict()
        if result["state"] != "awaiting_answer":
            return None
        prefs = result["analysis_preferences"]
        from datetime import timedelta
        fresh_until = parse_utc(result["market_cutoff"]) + timedelta(minutes=min(
            prefs["scalp_refresh_minutes"], prefs["intraday_refresh_minutes"]))
        return result if parse_utc(self._clock()) < fresh_until else None

    def _remember_holding_request(self, request, context, scope):
        value = {"request_id": request["request_id"], "fingerprint": context["fingerprint"]}
        key = context["holding_key"]
        old = self._docs.get(scope, "holding_pending", key)
        if old:
            self._docs.update(scope, "holding_pending", key, value, old["revision"])
        else:
            self._docs.insert(scope, "holding_pending", key, value)

    def _commit_holding_review(self, request, version, scope):
        holding = request["context"]["holding_review"]
        advice = version["answer"]["order_advice"][0]
        ends = [parse_utc(request["window_end"])]
        if advice["valid_until"]:
            ends.append(parse_utc(advice["valid_until"]))
        if holding["deadline"]:
            ends.append(parse_utc(holding["deadline"]))
        value = {"fingerprint": holding["fingerprint"], "covered_events": holding.get("covered_events", []),
            "view": {"request_id": request["request_id"], "version_id": version["version_id"],
                "market_cutoff": request["market_cutoff"], "available_at": version["available_at"],
                "valid_until": min(ends).isoformat().replace("+00:00", "Z"),
                "current": False, "stale_reasons": [], "advice": advice}}
        key = holding["holding_key"]
        pending = self._docs.get(scope, "holding_pending", key)
        if pending and pending["request_id"] != request["request_id"]:
            return  # Preserve raw/version, but never promote a superseded request's opinion.
        old = self._docs.get(scope, "holding_review", key)
        # An older slow response must not replace a more recent frozen assessment.
        if old and parse_utc(old["view"]["market_cutoff"]) > parse_utc(request["market_cutoff"]):
            return
        if old:
            self._docs.update(scope, "holding_review", key, value, old["revision"], available_at=version["available_at"])
        else:
            self._docs.insert(scope, "holding_review", key, value, available_at=version["available_at"])
