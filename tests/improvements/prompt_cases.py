"""Select a candidate when a synthetic request is created, never rewrite a saved packet."""
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def selected_prompt(flow, prompt_version):
    service = flow.w.analysis
    create = service.create_request

    def create_selected(*args, **kwargs):
        assert "prompt_version" not in kwargs, "Use this fixture only for the ordinary request route."
        return create(*args, prompt_version=prompt_version, **kwargs)

    with patch.object(service, "create_request", create_selected):
        yield
