"""Conditional candidates are model proposals; READY remains a current code decision."""
import copy
import json
from pathlib import Path

import pytest
from xau_next.domains.analysis.validation import validate_answer
from xau_next.shared.errors import DomainError

ROOT = Path(__file__).resolve().parents[2]


def candidate_case():
    answer = json.loads((ROOT / "fixtures/analysis-answer.guided.json").read_text(encoding="utf-8"))
    answer["schema_version"] = "1.2.0"
    answer["candidate_assessments"] = [
        {"trading_style": "scalp", "outcome": "no_candidate", "plan_key": None,
         "rationale": "合成：尚无法定义有依据的短线止损。", "missing_conditions": ["缺少短线结构止损依据。"]},
        {"trading_style": "intraday", "outcome": "candidate", "plan_key": "primary",
         "rationale": "合成：结构完整，等待未来触发及回踩。", "missing_conditions": []},
    ]
    request = {k: answer[k] for k in ("request_id", "forecast_id", "base_version_id", "market_snapshot_id", "market_cutoff")}
    request.update(window_start=answer["market_cutoff"], window_end="2026-10-12T14:00:00Z",
                   horizon_minutes=[180, 360, 720], answer_schema_version="1.2.0",
                   analysis_preferences={"enabled_styles": ["scalp", "intraday"]})
    market = next(e["value"] for e in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8")) if e["schema"] == "MarketSnapshot")
    context = {"market": market, "portfolio": {"orders": []}, "evidence_refs": answer["evidence_refs"]}
    schema = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
    return answer, schema, request, context


def test_untriggered_candidate_is_valid_without_claiming_current_readiness():
    values = candidate_case()
    before = copy.deepcopy(values[0])
    validate_answer(*values)
    assert values[0] == before


@pytest.mark.parametrize("defect", ["missing_style", "duplicate_style", "wrong_key", "contradiction", "empty_reason", "whitespace", "fake_ready", "downgrade"])
def test_assessments_must_cover_enabled_styles_and_match_actual_candidates(defect):
    answer, schema, request, context = candidate_case()
    rows = answer["candidate_assessments"]
    if defect == "missing_style": rows.pop()
    if defect == "duplicate_style": rows[1] = copy.deepcopy(rows[0])
    if defect == "wrong_key": rows[1]["plan_key"] = "nonexistent"
    if defect == "contradiction": rows[1].update(outcome="no_candidate", plan_key=None, missing_conditions=["合成矛盾"])
    if defect == "empty_reason": rows[0]["missing_conditions"] = []
    if defect == "whitespace": rows[0]["missing_conditions"] = ["  \n "]
    if defect == "fake_ready": rows[1]["outcome"] = "READY"
    if defect == "downgrade":
        answer["schema_version"] = "1.1.0"
        answer.pop("candidate_assessments")
    with pytest.raises(DomainError):
        validate_answer(answer, schema, request, context)


def test_both_styles_can_have_no_candidate_and_old_answers_remain_unchanged():
    answer, schema, request, context = candidate_case()
    answer["plans"] = []
    answer["candidate_assessments"][1].update(outcome="no_candidate", plan_key=None,
        rationale="合成：日内目标空间没有依据。", missing_conditions=["缺少支持日内目标空间的事实。"])
    validate_answer(answer, schema, request, context)
    assert answer["plans"] == []
    answer.pop("candidate_assessments")
    answer["schema_version"] = request["answer_schema_version"] = "1.1.0"
    before = copy.deepcopy(answer)
    validate_answer(answer, schema, request, context)
    assert answer == before and "candidate_assessments" not in answer


def test_only_frozen_enabled_styles_are_assessed():
    answer, schema, request, context = candidate_case()
    request["analysis_preferences"]["enabled_styles"] = ["intraday"]
    answer["candidate_assessments"] = answer["candidate_assessments"][1:]
    validate_answer(answer, schema, request, context)
