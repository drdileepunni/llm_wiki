"""
Extract timeline events from patient.documents[].
Each document (lab report) creates one LAB_RESULT event using reportedAt (fallback: createdAt).
"""

from ..models import TimelineEvent, LAB_RESULT
from ..date_utils import parse_timestamp


def _get_lab_name(doc: dict) -> str:
    if doc.get("name"):
        return doc["name"]
    tags = doc.get("tags") or []
    return " / ".join(t for t in tags[:2] if t) or "Lab result"


def _detect_new_schema(attr_dict: dict) -> bool:
    return any(isinstance(v, dict) and "value" in v for v in attr_dict.values())


def _summarize_attributes(attr_dict) -> str:
    if not attr_dict or isinstance(attr_dict, list):
        return f"{len(attr_dict)} result(s)" if isinstance(attr_dict, list) else ""
    new_schema = _detect_new_schema(attr_dict)
    items = []
    for k, v in attr_dict.items():
        val = v.get("value") if new_schema and isinstance(v, dict) else v
        if val is not None and str(val).strip() != "":
            items.append(f"{k}={val}")
    return ", ".join(items) if items else f"{len(attr_dict)} result(s)"


def extract_document_events(patient: dict) -> list[TimelineEvent]:
    events = []
    for doc in patient.get("documents") or []:
        if not isinstance(doc, dict):
            continue

        ts_raw = doc.get("reportedAt") or doc.get("createdAt")
        utc, ist, shift = parse_timestamp(ts_raw)
        if utc is None:
            continue

        lab_name = _get_lab_name(doc)
        attr_dict = doc.get("attributes") or {}
        attr_summary = _summarize_attributes(attr_dict)
        is_inactive = doc.get("isInactive", False)

        inactive_tag = " [inactive]" if is_inactive else ""
        summary = f"Lab result: {lab_name}{inactive_tag} — {attr_summary}"
        detail = f"lab={lab_name}, inactive={is_inactive}, results={attr_summary}"

        events.append(TimelineEvent(
            timestamp_utc=utc,
            timestamp_ist=ist,
            shift=shift,
            source_file="patients.json",
            source_id=str(doc.get("_id") or ""),
            event_category="LAB",
            event_type=LAB_RESULT,
            actor_name="System",
            actor_role="System (RADAR)",
            actor_type="system",
            summary=summary,
            detail=detail,
        ))

    return events
