"""Local preparation timings only: no prompts, credentials or market values."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
from time import perf_counter

from .values import new_id, parse_utc, utc_now

_active = ContextVar("analysis_preparation_timing", default=None)
OPERATIONS = frozenset({"getDailyAnalysisSource", "prepareDailyAnalysis", "startDailyAnalysis"})
_FIELDS = frozenset({"group_id", "market_snapshot_id", "market_cutoff", "source_age_ms",
                     "models_count", "package_kind", "source_age_at_dispatch_ms"})


@contextmanager
def preparation_trace(operation):
    if operation not in OPERATIONS or _active.get() is not None:
        yield _active.get()
        return
    trace = {"format": "analysis-preparation-timing/1", "trace_id": new_id(),
             "operation": operation, "started_at": utc_now(), "stages_ms": {}, "stage_calls": {}}
    token = _active.set(trace)
    started = perf_counter()
    try:
        yield trace
        trace["outcome"] = "completed"
    except BaseException as error:
        trace["outcome"] = "failed"
        trace["error_code"] = getattr(error, "code", type(error).__name__)
        raise
    finally:
        trace["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        if trace.get("market_cutoff"):
            trace["source_age_at_end_ms"] = round(
                (parse_utc(utc_now()) - parse_utc(trace["market_cutoff"])).total_seconds() * 1000, 3)
        _active.reset(token)
        logging.getLogger("uvicorn.error").info("analysis_preparation_timing %s", json.dumps(trace))


def preparation_fact(**fields):
    trace = _active.get()
    if trace is not None:
        trace.update({k: v for k, v in fields.items() if k in _FIELDS})


@contextmanager
def preparation_span(name):
    trace = _active.get()
    if trace is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        trace["stages_ms"][name] = round(trace["stages_ms"].get(name, 0) +
                                        (perf_counter() - started) * 1000, 3)
        trace["stage_calls"][name] = trace["stage_calls"].get(name, 0) + 1


@contextmanager
def preparation_writer_lock(lock):
    with preparation_span("writer_queue"):
        lock.acquire()
    try:
        yield
    finally:
        lock.release()
