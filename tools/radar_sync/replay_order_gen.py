"""
Inline order generation for CDS replay — shadow trial only.

Suppression windows prevent the agent re-suggesting the same order every snapshot.
Suggestions are stored in db.snapshot_suggestions and never reach the real EMR.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SUPPRESS_HOURS: dict[str, int] = {
    "antibiotic":    24,
    "antimicrobial": 24,
    "medication":    24,
    "infusion":      12,
    "ventilator":     8,
    "procedure":     12,
    "lab":            6,
    "fluid":          4,
    "blood":          4,
    "monitoring":   999,  # once per admission
}


def _suppress_hours(order_type: str) -> int:
    t = (order_type or "").lower()
    for key, hours in _SUPPRESS_HOURS.items():
        if key in t:
            return hours
    return 8


def _get_suppressed(db, cpmrn: str, encounter: int, snapshot_at: datetime) -> dict[str, str]:
    """Return {orderable_name: order_type} for orders still within their suppression window."""
    prior = list(db.snapshot_suggestions.find(
        {"CPMRN": cpmrn, "encounter": encounter, "snapshot_at": {"$lt": snapshot_at}},
        {"snapshot_at": 1, "orders": 1},
    ).sort("snapshot_at", 1))

    # latest suggestion time + type per orderable_name
    latest: dict[str, tuple[datetime, str]] = {}
    for doc in prior:
        snap_ts = doc["snapshot_at"]
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=timezone.utc)
        for order in (doc.get("orders") or []):
            name = order.get("orderable_name", "").strip()
            if not name:
                continue
            otype = order.get("order_type", "medication")
            if name not in latest or snap_ts > latest[name][0]:
                latest[name] = (snap_ts, otype)

    suppressed: dict[str, str] = {}
    for name, (last_ts, otype) in latest.items():
        if last_ts + timedelta(hours=_suppress_hours(otype)) > snapshot_at:
            suppressed[name] = otype
    return suppressed


def _build_active_orders(chart: dict, suppressed: dict[str, str]) -> dict:
    """
    Build active_orders dict in the format expected by run_order_generation:
    {"medications": [...], "labs": [...], "procedures": [...], "vents": [...]}

    Combines real chart meds + suppressed suggestions (so agent deduplicates both).
    """
    med_list = []
    orders_obj = chart.get("orders") or {}
    chart_meds = (orders_obj.get("active") or {}).get("medications") or []
    for m in chart_meds:
        name = m.get("name", "").strip()
        if name:
            med_list.append({
                "orderNo": "",
                "name": name,
                "quantity": m.get("dose", ""),
                "unit": m.get("unit", ""),
                "route": m.get("route", ""),
                "frequency": m.get("frequency", ""),
            })

    # Add suppressed suggestions so agent treats them as already active
    for name, otype in suppressed.items():
        t = otype.lower()
        entry = {"orderNo": "", "name": name, "quantity": "", "unit": "", "route": "", "frequency": ""}
        if "lab" in t:
            pass  # added to labs below
        else:
            med_list.append(entry)

    # Build separate lists for labs/procs from suppressed
    lab_list = []
    proc_list = []
    for name, otype in suppressed.items():
        t = otype.lower()
        if "lab" in t:
            lab_list.append({"orderNo": "", "investigation": name})
        elif "proc" in t or "procedure" in t:
            proc_list.append({"orderNo": "", "name": name})

    return {
        "medications": med_list,
        "labs":        lab_list,
        "procedures":  proc_list,
        "vents":       [],
    }


def run_replay_order_gen(
    db,
    cpmrn: str,
    encounter: int,
    snapshot_idx: int,
    snapshot_at: datetime,
    structured_summary: dict,
    chart: dict,
) -> list[dict]:
    """
    Run order generation for one replay snapshot.
    Applies suppression, stores in snapshot_suggestions, returns order list.
    Each order dict has an added 'suppressed' bool key.
    """
    suggested_actions = structured_summary.get("suggested_actions") or []
    if not suggested_actions:
        logger.info("replay_order_gen: no suggested_actions at idx=%d", snapshot_idx)
        return []

    if isinstance(snapshot_at, str):
        try:
            snapshot_at = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
        except Exception:
            snapshot_at = datetime.now(timezone.utc)
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)

    suppressed = _get_suppressed(db, cpmrn, encounter, snapshot_at)
    active_orders = _build_active_orders(chart, suppressed)

    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    try:
        from backend.services.order_gen_pipeline import run_order_generation
        from backend.config import MODEL

        result = run_order_generation(
            recommendations=suggested_actions,
            cpmrn=cpmrn,
            patient_type="icu",
            model=MODEL,
            kb=None,        # run_order_generation falls back to _default_kb() internally
            active_orders=active_orders,
            parent_run_id=f"replay_{cpmrn}_{encounter}_{snapshot_idx}",
        )
    except Exception:
        logger.exception("replay_order_gen: order generation failed at idx=%d", snapshot_idx)
        return []

    orders = result.get("orders") or []
    suppressed_names = set(suppressed.keys())
    for order in orders:
        order["suppressed"] = order.get("orderable_name", "") in suppressed_names

    doc = {
        "CPMRN":        cpmrn,
        "encounter":    encounter,
        "snapshot_idx": snapshot_idx,
        "snapshot_at":  snapshot_at,
        "stored_at":    datetime.now(timezone.utc),
        "orders":       orders,
        "cost_usd":     result.get("cost_usd"),
    }
    db.snapshot_suggestions.insert_one(doc)
    logger.info("replay_order_gen: stored %d orders at idx=%d", len(orders), snapshot_idx)
    return orders
