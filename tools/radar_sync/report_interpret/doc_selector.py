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

    # Collect all matching image documents first, then group by (name, reportedAt-minute)
    # because Radar stores multi-image reports (e.g. ECHO) as one document per image file,
    # all with the same name and reportedAt but different file keys. Without grouping,
    # each image would be interpreted and alerted on independently.
    from collections import defaultdict
    candidates: list[dict] = []
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
        if reported is None or reported <= cutoff_dt:
            continue
        file_key = d.get(key_field)
        if not file_key:
            logger.warning("doc_selector: matched report %r has no %s field", d.get("name"), key_field)
            continue
        name = d.get("name", "") or d.get("label", "")
        # Minute-precision timestamp for grouping — sub-minute differences are the same exam
        ts_minute = str(d.get("reportedAt", ""))[:16]
        candidates.append({
            "file_key": file_key,
            "reported_at": d.get("reportedAt"),
            "name": name,
            "category": d.get("category", ""),
            "selection_reason": f"matched:{matched}",
            "_group_key": f"{name.lower()}|{ts_minute}",
        })

    # Group candidates by (name, reported-minute) → one report entry per group
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        groups[c["_group_key"]].append(c)

    out: list[dict] = []
    for group_key, docs in groups.items():
        primary = docs[0]
        all_file_keys = [d["file_key"] for d in docs]
        if len(docs) > 1:
            logger.info(
                "doc_selector: grouped %d images into one report '%s' (reported_at=%s)",
                len(docs), primary["name"], primary["reported_at"],
            )
        out.append({
            "file_key": primary["file_key"],       # primary image key (backwards compat)
            "all_file_keys": all_file_keys,        # all image keys for this report group
            "reported_at": primary["reported_at"],
            "name": primary["name"],
            "category": primary["category"],
            "doc_id": group_key,                   # stable across runs: name+timestamp-minute
            "selection_reason": primary["selection_reason"],
        })
    return out
