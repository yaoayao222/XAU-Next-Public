"""Detached pure calculations and the sole review persistence/query service.

The pure functions accept dictionaries or shared Snapshot values and can run in
a ProcessPool. They never access a database or a trading terminal. Pass their
prepared Snapshot to ReviewService.commit_review inside the API transaction.
"""

from .actual import compute_actual
from .prediction import compute_prediction
from .reference import compute_reference
from .service import ReviewService
from .diagnostics import diagnose_review
from .selection import score_selection_window

__all__ = ["ReviewService", "compute_actual", "compute_prediction", "compute_reference", "diagnose_review"]
