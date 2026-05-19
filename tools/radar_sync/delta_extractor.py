"""Extract what's new in a chart since the last snapshot timestamp."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _parse_ts(ts_val: Any) -> datetime | None:
    if ts_val is None:
        return None
    if isinstance(ts_val, datetime):
        return ts_val.replace(tzinfo=timezone.utc) if ts_val.tzinfo is None else ts_val
    try:
        import pandas as pd
        parsed = pd.to_datetime(ts_val, utc=True)
        return parsed.to_pydatetime()
    except Exception:
        return None


def extract_delta(chart: dict, last_snapshot_at: datetime | None) -> dict:
    """
    Compare the chart against last_snapshot_at and return only the new events.

    Returns:
        {
            "new_vitals": [...],      # vitals with timestamp > last_snapshot_at
            "new_labs": [...],        # lab documents with reportedAt > last_snapshot_at
            "delta_orders": {...},    # full active/pending orders (no good timestamp diff possible)
            "new_notes": [...],       # notes with timestamp > last_snapshot_at
            "io_last_24h": {...},     # total intake/output ml in last 24h window
        }
    """
    cutoff = last_snapshot_at

    return {
        "new_vitals":   _new_vitals(chart, cutoff),
        "new_labs":     _new_labs(chart, cutoff),
        "delta_orders": _current_orders(chart),
        "new_notes":    _new_notes(chart, cutoff),
        "io_last_24h":  _io_last_24h(chart),
    }


def _new_vitals(chart: dict, cutoff: datetime | None) -> list[dict]:
    vitals = chart.get("vitals") or []
    if not cutoff:
        return vitals[:6]  # first run: newest 6 (array is newest-first)
    result = []
    for v in vitals:
        ts = _parse_ts(v.get("timestamp"))
        if ts and ts > cutoff:
            result.append(v)
    return result


def _new_labs(chart: dict, cutoff: datetime | None) -> list[dict]:
    docs = [d for d in (chart.get("documents") or []) if d.get("category") == "labs"]
    if not cutoff:
        return docs[-10:]  # first run: newest 10 (labs array is oldest-first)
    result = []
    for d in docs:
        ts = _parse_ts(d.get("reportedAt"))
        if ts and ts > cutoff:
            result.append(d)
    return result


def _current_orders(chart: dict) -> dict:
    orders = chart.get("orders") or {}
    return {
        "active":    orders.get("active") or {},
        "pending":   orders.get("pending") or {},
        "completed": orders.get("completed") or {},
    }


def _new_notes(chart: dict, cutoff: datetime | None) -> list[dict]:
    """Return notes (content entries) newer than cutoff, or last 3 substantive ones on first run."""
    notes_obj = chart.get("notes") or {}
    all_notes: list[dict] = []
    for note in (notes_obj.get("finalNotes") or []):
        for content in (note.get("content") or []):
            ts_raw = content.get("timestamp") or note.get("createdTimestamp")
            ts = _parse_ts(ts_raw)
            text_parts = []
            for comp in (content.get("components") or []):
                if isinstance(comp, dict) and comp.get("value"):
                    import re
                    text_parts.append(re.sub(r"<[^>]+>", " ", comp["value"]).strip())
            text = " ".join(p for p in text_parts if p)
            if not text.strip():
                continue
            all_notes.append({
                "timestamp":  ts_raw,
                "note_type":  (content.get("noteType") or "") + " / " + (content.get("noteSubType") or ""),
                "author":     (content.get("author") or {}).get("name", "") if isinstance(content.get("author"), dict) else "",
                "text":       text,
            })
    if not cutoff:
        # First run: return last 3 substantive notes (len > 100 chars)
        substantive = [n for n in all_notes if len(n["text"]) > 100]
        return (substantive or all_notes)[-3:]  # newest 3 (notes array is oldest-first)
    return [n for n in all_notes if _parse_ts(n.get("timestamp")) and _parse_ts(n["timestamp"]) > cutoff]


def _io_last_24h(chart: dict) -> dict:
    """Compute total intake_ml, output_ml from io object (all recorded entries)."""
    intake_ml, output_ml = 0.0, 0.0
    io = chart.get("io") or {}
    for day in (io.get("days") or []):
        for hour_blk in (day.get("hours") or []):
            for min_blk in (hour_blk.get("minutes") or []):
                intake = min_blk.get("intake") or {}
                out    = min_blk.get("output") or {}
                # intake: meds infusion/bolus + feeds
                for inf in (intake.get("meds") or {}).get("infusion") or []:
                    amt = _safe_float(inf.get("amount"))
                    if amt:
                        intake_ml += amt
                for bol in (intake.get("meds") or {}).get("bolus") or []:
                    amt = _safe_float(bol.get("amount"))
                    if amt:
                        intake_ml += amt
                feeds = intake.get("feeds") or {}
                if isinstance(feeds, dict):
                    for fv in feeds.values():
                        if isinstance(fv, dict):
                            amt = _safe_float(fv.get("amount"))
                            if amt:
                                intake_ml += amt
                # output: drain, procedure, dialysis
                for items in (out.get("drain", []), out.get("procedure", []), out.get("dialysis", [])):
                    for item in items:
                        amt = _safe_float(item.get("amount") if isinstance(item, dict) else item)
                        if amt:
                            output_ml += amt
    return {"intake_ml": round(intake_ml, 1), "output_ml": round(output_ml, 1), "balance_ml": round(intake_ml - output_ml, 1)}


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
