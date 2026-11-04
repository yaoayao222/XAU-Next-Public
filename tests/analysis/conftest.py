"""Keep the original import regression suite on frozen pre-1.2 request metadata.

New request/answer behavior is exercised through the public API in tests/guidance.
"""
import pytest


@pytest.fixture(autouse=True)
def legacy_request_format(monkeypatch, request):
    from xau_next.domains.analysis.service import AnalysisService
    monkeypatch.setattr("xau_next.domains.analysis.service.ANSWER_SCHEMA_VERSION", "1.0.0")
    monkeypatch.setattr("xau_next.domains.analysis.service.PROMPT_VERSION", "manual-analysis/1")
    # create_request has an explicit production default, independent of the
    # module constant. Pin the legacy test inputs as well, never production.
    if request.module.__name__.endswith("test_analysis"):
        monkeypatch.setitem(AnalysisService.create_request.__kwdefaults__, "prompt_version", "manual-analysis/1")
