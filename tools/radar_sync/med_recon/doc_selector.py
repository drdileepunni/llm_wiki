"""
Select NEW treatment-chart / progress-note documents from a chart since the last
reconciliation watermark.

Mirrors delta_extractor's reportedAt-cutoff pattern, but for `category=="documents"`
docs whose `name`/`label` matches a treatment-chart/progress-note pattern. The
category strings, name patterns, and media file-key field are configured in
`app_settings.med_recon_config` (seeded during discovery) with constant fallbacks.

Discovery (INTSHYD456101/1): category ∈ {documents, labs, scans}; treatment charts &
progress notes are `category=="documents"` with name "Treatment chart"/"Progress notes";
the media key lives in field `key` (e.g. "images/<id>:<iso>.JPEG"); timestamp is `reportedAt`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_CATEGORIES = ("documents",)
_DEFAULT_NAME_PATTERNS = ("treatment chart", "progress note")
_DEFAULT_KEY_FIELD = "key"


def load_config(db) -> dict:
    """Read app_settings.med_recon_config with constant fallbacks; never raises."""
    cfg = None
    try:
        cfg = db["app_settings"].find_one({"_id": "med_recon_config"})
    except Exception:
        logger.exception("doc_selector: failed to read med_recon_config")
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "categories": [str(c).lower() for c in (cfg.get("categories") or _DEFAULT_CATEGORIES)],
        "name_patterns": [str(p).lower() for p in (cfg.get("name_patterns") or _DEFAULT_NAME_PATTERNS)],
        "key_field": cfg.get("key_field") or _DEFAULT_KEY_FIELD,
    }


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def select_new_treatment_docs(chart: dict, cutoff, config: dict | None = None) -> list[dict]:
    """
    Return treatment-chart / progress-note documents with reportedAt > cutoff,
    normalized to {file_key, reported_at, name, category, doc_id}.

    On first run (cutoff is None) returns [] — we only advance the watermark and
    never bulk-transcribe historical charts on enrollment.
    """
    cfg = config or {
        "categories": list(_DEFAULT_CATEGORIES),
        "name_patterns": list(_DEFAULT_NAME_PATTERNS),
        "key_field": _DEFAULT_KEY_FIELD,
    }
    cutoff_dt = _parse_ts(cutoff)
    if cutoff_dt is None:
        return []  # first run: watermark-only

    cats = set(cfg["categories"])
    patterns = cfg["name_patterns"]
    key_field = cfg["key_field"]

    out: list[dict] = []
    for d in (chart.get("documents") or []):
        if (d.get("category") or "").lower() not in cats:
            continue
        haystack = f"{d.get('name', '')} {d.get('label', '')}".lower()
        if not any(p in haystack for p in patterns):
            continue
        reported = _parse_ts(d.get("reportedAt"))
        if reported is None or reported <= cutoff_dt:  # strict > : at-most-once per doc
            continue
        file_key = d.get(key_field)
        if not file_key:
            logger.warning("doc_selector: matched doc %r has no %s field", d.get("name"), key_field)
            continue
        out.append({
            "file_key": file_key,
            "reported_at": d.get("reportedAt"),
            "name": d.get("name", ""),
            "category": d.get("category", ""),
            "doc_id": file_key,  # file key is a stable identity
        })
    return out
