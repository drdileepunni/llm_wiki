"""
Extract timeline events from the orders section of patients.json.

Each order object can yield up to 4 events:
  - PLACED   (always, from createdAt)
  - SIGNED   (labs only, from signedAt + signed)
  - COMPLETED (from completedAt + completedBy)
  - DISCONTINUED (from discontinueAt + discontinueBy)
"""

from __future__ import annotations

from datetime import timedelta
from ..models import (
    TimelineEvent,
    ORDER_MED_PLACED, ORDER_MED_COMPLETED, ORDER_MED_DISCONTINUED,
    ORDER_LAB_PLACED, ORDER_LAB_SIGNED,
    ORDER_DIET_PLACED, ORDER_DIET_DISCONTINUED,
    ORDER_PROCEDURE_PLACED, ORDER_VENT_PLACED, ORDER_BLOOD_PLACED,
)
from ..date_utils import parse_timestamp

_SYSTEM_KEYWORDS = {"radar", "ambient", "ambient_bot", "system", "bot"}


def _classify_actor(name_or_email: str) -> tuple[str, str]:
    """Returns (actor_role, actor_type) from a name/email string."""
    val = (name_or_email or "").lower()
    if "ambient" in val or "ambient_bot" in val:
        return "System (Ambient Bot)", "system"
    if "radar" in val or val.startswith("system"):
        return "System (RADAR)", "system"
    return "Clinician", "human"


def _get_id(order: dict) -> str:
    return str(order.get("orderNo") or order.get("_id") or "")


def _format_frequency(order: dict) -> str:
    freq = order.get("frequency") or {}
    ftype = freq.get("fType", "")
    if ftype == "continuous":
        return "continuous"
    if ftype == "every":
        hours = freq.get("hours")
        return f"every {hours}h" if hours else "every"
    if ftype == "once":
        return "once"
    return ftype or ""


def _make_event(order, event_type, ts_raw, actor_name, source_id, summary, detail, category) -> TimelineEvent | None:
    utc, ist, shift = parse_timestamp(ts_raw)
    if utc is None:
        return None
    actor_name = (actor_name or "Unknown").strip()
    actor_role, actor_type = _classify_actor(actor_name)
    return TimelineEvent(
        timestamp_utc=utc,
        timestamp_ist=ist,
        shift=shift,
        source_file="patients.json",
        source_id=source_id,
        event_category="ORDER",
        event_type=event_type,
        actor_name=actor_name,
        actor_role=actor_role,
        actor_type=actor_type,
        summary=summary,
        detail=detail,
    )


# ── Per-type extractors ───────────────────────────────────────────────────────

def _med_events(order: dict, state: str) -> list[TimelineEvent]:
    events = []
    name = order.get("name") or "Unknown"
    qty = order.get("quantity", "")
    unit = order.get("unit", "")
    route = order.get("route", "")
    freq = _format_frequency(order)
    dose = f"{qty} {unit} {route}".strip()
    sid = _get_id(order)
    detail_base = f"state={state}, freq={freq}, instructions={str(order.get('instructions',''))[:120]}"

    e = _make_event(order, ORDER_MED_PLACED, order.get("createdAt"),
                    order.get("createdBy"),
                    sid, f"Medication ordered: {name} {dose} ({freq})", detail_base, state)
    if e: events.append(e)

    if order.get("completedAt") and order.get("completedBy"):
        e = _make_event(order, ORDER_MED_COMPLETED, order.get("completedAt"),
                        order.get("completedBy"), sid,
                        f"Medication completed: {name}", detail_base, state)
        if e: events.append(e)

    if order.get("discontinueAt") and order.get("discontinueBy"):
        e = _make_event(order, ORDER_MED_DISCONTINUED, order.get("discontinueAt"),
                        order.get("discontinueBy"), sid,
                        f"Medication discontinued: {name} — {order.get('discontinueReason','')[:80]}",
                        detail_base, state)
        if e: events.append(e)

    return events


def _lab_events(order: dict, state: str) -> list[TimelineEvent]:
    events = []
    investigation = order.get("investigation") or "Unknown"
    urgency = order.get("urgency") or ""
    urgency_tag = f" [{urgency}]" if urgency else ""
    sid = _get_id(order)
    detail_base = (f"state={state}, discipline={order.get('discipline','')}, "
                   f"specimen={order.get('specimenType','')}, "
                   f"info={str(order.get('additionalInformation',''))[:120]}")

    e = _make_event(order, ORDER_LAB_PLACED, order.get("createdAt"),
                    order.get("createdBy"), sid,
                    f"Lab ordered: {investigation}{urgency_tag}", detail_base, state)
    if e: events.append(e)

    if order.get("signedAt") and order.get("signed"):
        e = _make_event(order, ORDER_LAB_SIGNED, order.get("signedAt"),
                        order.get("signed"), sid,
                        f"Lab signed: {investigation}{urgency_tag}", detail_base, state)
        if e: events.append(e)

    return events


def _diet_events(order: dict, state: str) -> list[TimelineEvent]:
    events = []
    name = order.get("name") or "Diet"
    instructions = str(order.get("instructions") or "")
    summary = f"Diet ordered: {name} — {instructions[:80]}" if instructions else f"Diet ordered: {name}"
    sid = _get_id(order)
    detail = f"state={state}, freq={_format_frequency(order)}, instructions={instructions[:200]}"

    e = _make_event(order, ORDER_DIET_PLACED, order.get("createdAt"),
                    order.get("createdBy"), sid, summary, detail, state)
    if e: events.append(e)

    if order.get("discontinueAt") and order.get("discontinueBy"):
        e = _make_event(order, ORDER_DIET_DISCONTINUED, order.get("discontinueAt"),
                        order.get("discontinueBy"), sid,
                        f"Diet discontinued: {name}", detail, state)
        if e: events.append(e)

    return events


def _procedure_events(order: dict, state: str) -> list[TimelineEvent]:
    events = []
    ptype = order.get("pType") or order.get("name") or "Procedure"
    site = order.get("site") or ""
    lat = order.get("laterality") or ""
    location = ", ".join(filter(None, [site, lat]))
    summary = f"Procedure ordered: {ptype}" + (f" (site: {location})" if location else "")
    sid = _get_id(order)
    detail = f"state={state}, instructions={str(order.get('instructions',''))[:150]}"

    e = _make_event(order, ORDER_PROCEDURE_PLACED, order.get("createdAt"),
                    order.get("createdBy"), sid, summary, detail, state)
    if e: events.append(e)
    return events


def _generic_events(order: dict, state: str, event_type: str, label: str) -> list[TimelineEvent]:
    name = order.get("name") or label
    sid = _get_id(order)
    e = _make_event(order, event_type, order.get("createdAt"),
                    order.get("createdBy"), sid,
                    f"{label} ordered: {name}",
                    f"state={state}", state)
    return [e] if e else []


# ── Main entry point ──────────────────────────────────────────────────────────

def extract_order_events(patient: dict) -> list[TimelineEvent]:
    events = []
    orders = patient.get("orders") or {}

    for state, state_data in orders.items():
        if not isinstance(state_data, dict):
            continue

        for order in state_data.get("medications") or []:
            events.extend(_med_events(order, state))

        for order in state_data.get("labs") or []:
            events.extend(_lab_events(order, state))

        for order in state_data.get("diets") or []:
            events.extend(_diet_events(order, state))

        for order in state_data.get("procedures") or []:
            events.extend(_procedure_events(order, state))

        for order in state_data.get("vents") or []:
            events.extend(_generic_events(order, state, ORDER_VENT_PLACED, "Ventilation"))

        for order in state_data.get("bloods") or state_data.get("bloodProducts") or []:
            events.extend(_generic_events(order, state, ORDER_BLOOD_PLACED, "Blood product"))

    return events
