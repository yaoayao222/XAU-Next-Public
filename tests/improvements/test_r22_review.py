"""SYNTHETIC end-to-end: frozen policy selects the new method without model calls."""

from tests.guidance.test_workflow import Flow, TOKEN
from tests.scalp.test_integration import facts


def test_new_version_uses_frozen_basis_after_live_settings_change(tmp_path):
    f = Flow(tmp_path)
    try:

        def no_candidates(a):
            a["plans"] = []
            for row in a["candidate_assessments"]:
                row.update(
                    outcome="no_candidate",
                    plan_key=None,
                    rationale="合成：本次仅核对预测评分。",
                    missing_conditions=["合成：不生成交易方案。"],
                )

        req, _ = f.initial(mutate=no_candidates)
        current = f.get("/v1/forecasts/" + req["forecast_id"])["current_version"]
        basis = f.w.analysis.review_basis(current["version_id"]).to_dict()
        frozen = facts(req["packet_text"])
        assert basis["method_version"] == "prediction_review/3"
        assert basis["market"]["quote"] == frozen["market"]["quote"]
        assert basis["max_age_ms"] == frozen["prediction_policy"]["endpoint_max_age_ms"]
        settings = f.get("/v1/settings")
        response = f.client.put(
            "/v1/settings",
            json={
                "expected_revision": settings["revision"],
                "values": {**settings["values"], "quote_max_age_ms": basis["max_age_ms"] + 1000},
            },
            headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "changed-after-freeze"},
        )
        assert response.status_code == 200, response.text
        assert f.w.analysis.review_basis(current["version_id"]).to_dict() == basis
        f.clock.value = "2026-10-12T14:04:00Z"
        job = f.post(
            "/v1/reviews/refresh", {"forecast_id": current["forecast_id"], "through": f.clock()}, 202
        )
        assert f.w.process_once(kind="review_refresh")
        assert f.get("/v1/jobs/" + job["job_id"])["state"] == "succeeded"
        rows = f.get("/v1/reviews?target_kind=forecast")["items"]
        assert len(rows) == 3
        assert all(r["method_version"] == "prediction_review/3" for r in rows)
        assert all(any(m["name"] == "anchor_price" for m in r["metrics"]) for r in rows)
        detail = f.get("/v1/reviews/" + rows[0]["review_id"])
        assert detail["diagnostics"]["direction_basis"] == "frozen_bid"
        f.w.market.capture = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("finished review recaptured")
        )
        next_job = f.post(
            "/v1/reviews/refresh", {"forecast_id": current["forecast_id"], "through": f.clock()}, 202
        )
        assert f.w.process_once(kind="review_refresh")
        assert "处理 0 项" in f.get("/v1/jobs/" + next_job["job_id"])["message"]
    finally:
        f.close()
