"""
Date/time utilities for the patient audit timeline.

Stdlib-only — no pytz, no bson. Uses zoneinfo (Python 3.9+).
Shift boundaries: Night→Day at 07:25 IST, Day→Night at 19:25 IST.
"""
from __future__ import annotations

import re
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc

FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
]


def _unwrap_mongo(raw) -> str | None:
    """
    Handle the three MongoDB $date forms:
      1. {"$date": "2026-04-11T07:44:00.000Z"}  -> ISO string
      2. {"$date": {"$numberLong": "1744352640000"}} -> epoch ms -> ISO string
      3. plain ISO string (no wrapper)
    Returns a string that parse_timestamp can consume, or None.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        val = raw.get("$date")
        if val is None:
            return None
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            ms = val.get("$numberLong")
            if ms is not None:
                epoch_ms = int(ms)
                dt = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
                return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        if isinstance(val, (int, float)):
            dt = datetime.fromtimestamp(val / 1000, tz=UTC)
            return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return None


def parse_timestamp(raw) -> tuple[datetime | None, datetime | None, str | None]:
    """
    Parse any raw MongoDB timestamp value.
    Returns (utc_naive, ist_aware, shift_label). All None if unparseable.
    """
    s = _unwrap_mongo(raw)
    if s is None:
        return None, None, None

    for fmt in FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            # treat as naive UTC
            utc_naive = dt.replace(tzinfo=None)
            ist_aware = to_ist(dt)
            shift = get_shift(ist_aware)
            return utc_naive, ist_aware, shift
        except ValueError:
            continue

    return None, None, None


def to_ist(dt: datetime) -> datetime:
    """Convert naive (assumed UTC) or any tz-aware datetime to IST-aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def get_shift(ist_dt: datetime) -> str:
    """
    Return clinical shift label for an IST datetime.
    Night→Day boundary: 07:25 IST
    Day→Night boundary: 19:25 IST
    """
    hour, minute = ist_dt.hour, ist_dt.minute
    before_day_start = hour < 7 or (hour == 7 and minute <= 25)
    after_day_end = hour > 19 or (hour == 19 and minute > 25)

    if before_day_start:
        return (ist_dt - timedelta(days=1)).strftime("%d %b %Y Night")
    elif after_day_end:
        return ist_dt.strftime("%d %b %Y Night")
    else:
        return ist_dt.strftime("%d %b %Y Day")


# ── HTML stripping ────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&nbsp;": " ", "&quot;": '"', "&#39;": "'",
    "&apos;": "'", "&ndash;": "–", "&mdash;": "—",
}


def strip_html(html: str) -> str:
    """Strip HTML tags and decode common entities. Stdlib only."""
    if not html:
        return ""
    text = _TAG_RE.sub(" ", html)
    for entity, char in _ENTITY_MAP.items():
        text = text.replace(entity, char)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── JSON loading with MongoDB object_hook ─────────────────────────────────────

def _mongo_hook(d: dict):
    if "$oid" in d:
        return d["$oid"]
    if "$date" in d:
        return {"$date": d["$date"]}  # leave for parse_timestamp
    return d


def load_json(path) -> list | dict:
    """Load a JSON file exported from MongoDB (handles $oid and $date wrappers)."""
    with open(path, "r") as f:
        return json.load(f, object_hook=_mongo_hook)
