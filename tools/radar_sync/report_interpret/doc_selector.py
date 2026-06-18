"""
Select NEW diagnostic-report documents from a chart since the last interpretation
watermark.

Mirrors med_recon/doc_selector but targets `category=="scans"` documents — the imaging
/ diagnostic reports (echo, ECG, X-ray, CT, MRI, ultrasound, doppler, angiogram). The
category strings, name patterns, exclude patterns, and media file-key field are configured
in `app_settings.report_interpret_config` with constant fallbacks.

Discovery (live snapshots): category ∈ {documents, labs, scans}. All diagnostic reports
are `category=="scans"` with names like "ECHO Report", "ECG", "CT", "MRI",
"XR Chest PA View", "Ultrasound abdomen", "CEREBRAL ANGIOGRAM", "Doppler/Vascular".
Treatment charts / progress notes / consent live under `documents` (med_recon's domain) and
are intentionally NOT selected here. The media key lives in field `key`; timestamp is
`reportedAt`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Only the imaging/diagnostic category — `documents` is med_recon's domain (treatment
# charts, progress notes) plus non-diagnostic items (consent, I/O, admission notes).
_DEFAULT_CATEGORIES = ("scans",)
# Allowlist of report-name substrings (case-insensitive). Covers the observed `scans` names.
_DEFAULT_TYPE_PATTERNS = (
    "echo", "ecg", "ekg", "ct", "mri", "xr", "x-ray", "xray",
    "ultrasound", "usg", "doppler", "angiogram", "radiograph", "scan",
)
_DEFAULT_EXCLUDE_PATTERNS = ()  # nothing to exclude within `scans`
_DEFAULT_KEY_FIELD = "key"


def load_config(db) -> dict:
    """Read app_settings.report_interpret_config with constant fallbacks; never raises."""
    cfg = None
    try:
        cfg = db["app_settings"].find_one({"_id": "report_interpret_config"})
    except Exception:
        logger.exception("doc_selector: failed to read report_interpret_config")
    cfg = cfg or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "categories": [str(c).lower() for c in (cfg.get("categories") or _DEFAULT_CATEGORIES)],
        "type_patterns": [str(p).lower() for p in (cfg.get("type_patterns") or _DEFAULT_TYPE_PATTERNS)],
        "exclude_patterns": [str(p).lower() for p in (cfg.get("exclude_patterns") or _DEFAULT_EXCLUDE_PATTERNS)],
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


def select_new_reports(chart: dict, cutoff, config: dict | None = None) -> list[dict]:
    """
    Return diagnostic-report documents with reportedAt > cutoff, normalized to
    {file_key, reported_at, name, category, doc_id, selection_reason}.

    `selection_reason` records which type pattern matched (for audit).

    On first run (cutoff is None) returns [] — we only advance the watermark and
    never bulk-interpret historical reports on enrollment.
    """
    cfg = config or {
        "categories": list(_DEFAULT_CATEGORIES),
        "type_patterns": list(_DEFAULT_TYPE_PATTERNS),
        "exclude_patterns": list(_DEFAULT_EXCLUDE_PATTERNS),
        "key_field": _DEFAULT_KEY_FIELD,
    }
    cutoff_dt = _parse_ts(cutoff)
    if cutoff_dt is None:
        return []  # first run: watermark-only

    cats = set(cfg["categories"])
    patterns = cfg["type_patterns"]
    excludes = cfg.get("exclude_patterns") or []
    key_field = cfg["key_field"]

    out: list[dict] = []
    for d in (chart.get("documents") or []):
        if (d.get("category") or "").lower() not in cats:
            continue
        haystack = f"{d.get('name', '')} {d.get('label', '')}".lower()
        if excludes and any(p in haystack for p in excludes):
            continue
        matched = next((p for p in patterns if p in haystack), None)
        if matched is None:
            continue
        reported = _parse_ts(d.get("reportedAt"))
        if reported is None or reported <= cutoff_dt:  # strict > : at-most-once per doc
            continue
        file_key = d.get(key_field)
        if not file_key:
            logger.warning("doc_selector: matched report %r has no %s field", d.get("name"), key_field)
            continue
        out.append({
            "file_key": file_key,
            "reported_at": d.get("reportedAt"),
            "name": d.get("name", "") or d.get("label", ""),
            "category": d.get("category", ""),
            "doc_id": file_key,  # file key is a stable identity
            "selection_reason": f"matched:{matched}",
        })
    return out
