"""Historical v1 import/review scenarios keep their original request format.

The current 1.2 API, notification and restart flows live in tests/guidance.
"""
import pytest


@pytest.fixture(autouse=True)
def legacy_request_format(monkeypatch):
    monkeypatch.setattr("xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION", "1.0.0")
    monkeypatch.setattr("xau_next.domains.analysis.service.PROMPT_VERSION", "manual-analysis/1")
