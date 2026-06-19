"""
Lab staleness overrides — per-lab maximum age (hours) before a result is too old to alert on.

Loaded from GCS app_settings/lab_staleness_overrides.json.
Falls back to _DEFAULTS if the GCS document is absent or disabled.

The hard suppression gate in problem_tracker uses these values to kill alerts whose
underlying lab draw is older than the per-lab limit, regardless of what the model decided.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Shipped defaults — these are written to GCS by the seed script.
# If GCS has a doc, its values win. If GCS is absent, these apply.
_DEFAULTS: dict[str, int] = {
    "lactate": 12,
}


def load_overrides(db: Any) -> dict[str, int]:
    """
    Return per-lab staleness hour overrides.
    Keys are lowercase lab names (e.g. "lactate").
    Values are the maximum age in hours before the result is considered too stale to alert on.
    """
    try:
        doc = db["app_settings"].find_one({"_id": "lab_staleness_overrides"})
        if doc and doc.get("enabled", True) and doc.get("overrides"):
            return {k.lower(): int(v) for k, v in doc["overrides"].items()}
    except Exception:
        log.exception("lab_staleness_overrides: failed to load from GCS — using defaults")
    return dict(_DEFAULTS)
