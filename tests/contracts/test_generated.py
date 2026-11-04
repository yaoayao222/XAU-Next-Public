import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from xau_next.http.contracts import Contracts
from xau_next.shared.errors import DomainError
from xau_next.transport.generated import models

ROOT = Path(__file__).resolve().parents[2]


def test_request_horizons_are_optional_but_explicit_null_remains_invalid():
    contracts = Contracts(ROOT / "contracts")
    for kind in ("new", "update"):
        command = {"kind": kind, "market_snapshot_id": "SYNTHETIC-market",
                   "forecast_id": None if kind == "new" else "SYNTHETIC-forecast",
                   "base_version_id": None if kind == "new" else "SYNTHETIC-version"}
        contracts.validate("RequestCreate", command)
        assert models.RequestCreate.model_validate(command).model_dump(mode="json", exclude_unset=True) == command
        with pytest.raises(DomainError):
            contracts.validate("RequestCreate", {**command, "horizon_minutes": None})


def test_ac20_external_refs_nullable_decimal_and_unions():
    c = Contracts(ROOT / "contracts")
    raw = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    # The HTTP boundary preserves absent extension fields instead of injecting nulls into v1 answers.
    assert models.AnalysisAnswer.model_validate(raw).model_dump(mode="json", exclude_unset=True) == raw
    c.validate("AnalysisAnswer", raw)
    broken = copy.deepcopy(raw)
    broken["plans"][0]["entry"]["price"] = 100.0
    with pytest.raises((ValidationError, TypeError)):
        models.AnalysisAnswer.model_validate(broken)
    with pytest.raises(DomainError):
        c.validate("AnalysisAnswer", broken)
    broken = copy.deepcopy(raw)
    del broken["base_version_id"]
    with pytest.raises(ValidationError):
        models.AnalysisAnswer.model_validate(broken)
    with pytest.raises(DomainError):
        c.validate("AnalysisAnswer", broken)
    broken = copy.deepcopy(raw)
    broken["market_cutoff"] = "2026-99-99T02:00:00Z"
    with pytest.raises(DomainError):
        c.validate("AnalysisAnswer", broken)


def test_ac20_examples_and_nested_errors():
    c = Contracts(ROOT / "contracts")
    for example in json.loads((ROOT / "fixtures/api-examples.json").read_text(encoding="utf-8")):
        c.validate(example["schema"], example["value"])
        model = getattr(models, example["schema"])
        assert (
            model.model_validate(example["value"]).model_dump(mode="json", exclude_unset=True)
            == example["value"]
        )


def test_guided_answer_extension_is_required_only_for_v11():
    c = Contracts(ROOT / "contracts")
    raw = json.loads((ROOT / "fixtures/analysis-answer.guided.json").read_text(encoding="utf-8"))
    assert models.AnalysisAnswer.model_validate(raw).model_dump(mode="json", exclude_unset=True) == raw
    c.validate("AnalysisAnswer", raw)
    missing = copy.deepcopy(raw)
    del missing["analysis_basis"]
    with pytest.raises(DomainError):
        c.validate("AnalysisAnswer", missing)
    old = json.loads((ROOT / "fixtures/analysis-answer.valid.json").read_text(encoding="utf-8"))
    c.validate("AnalysisAnswer", old)


def test_inline_cache_preserves_ref_siblings_and_strict_nested_values():
    c = Contracts(ROOT / "contracts")
    c.api["components"]["schemas"]["TestDecimalBound"] = {
        "$ref": "#/components/schemas/Decimal",
        "maxLength": 3,
    }
    c.validate("TestDecimalBound", "1.2")
    with pytest.raises(DomainError):
        c.validate("TestDecimalBound", "12.0")
    with pytest.raises(DomainError):
        c.validate("TestDecimalBound", 1.2)
    c.api["components"]["schemas"]["TestDecimalBound2"] = {
        "$ref": "#/components/schemas/Decimal",
        "pattern": "^1",
    }
    with pytest.raises(DomainError):
        c.validate("TestDecimalBound2", "2")
    assert c.validate("TestDecimalBound", "1.2") == "1.2"


def test_v12_generated_boundary_preserves_candidate_assessments_and_requires_them():
    c = Contracts(ROOT / "contracts")
    raw = json.loads((ROOT / "fixtures/analysis-answer.candidates.json").read_text(encoding="utf-8"))
    assert models.AnalysisAnswer.model_validate(raw).model_dump(mode="json", exclude_unset=True) == raw
    c.validate("AnalysisAnswer", raw)
    raw.pop("candidate_assessments")
    with pytest.raises(DomainError):
        c.validate("AnalysisAnswer", raw)
