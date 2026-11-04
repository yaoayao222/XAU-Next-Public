"""Versioned analysis preferences, shared as values rather than trading decisions."""

from copy import deepcopy

from .scalp import DEFAULT_SCALP_PREFERENCES

DEFAULT_ANALYSIS_PREFERENCES = {
    "enabled_styles": ["scalp", "intraday"],
    "scalp_refresh_minutes": 30,
    "intraday_refresh_minutes": 120,
    "desktop_notifications": True,
}


def analysis_preferences(values):
    return {**DEFAULT_ANALYSIS_PREFERENCES, **values.get("analysis_preferences", {})}


def new_analysis_preferences(values):
    preferences = deepcopy(analysis_preferences(values))
    preferences.setdefault("scalp_preferences", deepcopy(DEFAULT_SCALP_PREFERENCES))
    return preferences


def default_analysis_horizons(values):
    horizons = set(values["daily_horizons_minutes"])
    if "scalp" in analysis_preferences(values)["enabled_styles"]:
        horizons.update((30, 60))
    return sorted(horizons)
