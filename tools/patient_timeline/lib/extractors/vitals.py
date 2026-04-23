"""
Extract timeline events from patient.vitals[].
Each vital entry creates one VITAL_RECORDED event.
"""

from ..models import TimelineEvent, VITAL_RECORDED
from ..date_utils import parse_timestamp


def _format_vitals(v: dict) -> str:
    parts = []
    if v.get("daysHR"):   parts.append(f"HR={v['daysHR']}")
    if v.get("daysRR"):   parts.append(f"RR={v['daysRR']}")
    if v.get("daysBP"):   parts.append(f"BP={v['daysBP']}")
    if v.get("daysMAP"):  parts.append(f"MAP={v['daysMAP']}")
    if v.get("daysSpO2"): parts.append(f"SpO2={v['daysSpO2']}%")
    return ", ".join(parts) if parts else "no values"


def extract_vital_events(patient: dict) -> list[TimelineEvent]:
    events = []
    for vital in patient.get("vitals") or []:
        if not isinstance(vital, dict):
            continue

        utc, ist, shift = parse_timestamp(vital.get("timestamp"))
        if utc is None:
            continue

        data_by = vital.get("dataBy") or "Unknown"
        is_verified = vital.get("isVerified", False)
        abnormal_list = vital.get("abnormal_list") or []
        vitals_str = _format_vitals(vital)

        actor_role = "System (Netra)" if "netra" in data_by.lower() else "Clinician"
        actor_type = "system" if actor_role != "Clinician" else "human"

        abnormal_tag = f" [ABNORMAL: {', '.join(str(a) for a in abnormal_list)}]" if abnormal_list else ""
        verified_tag = " (verified)" if is_verified else ""
        summary = f"Vitals recorded by {data_by}{verified_tag}: {vitals_str}{abnormal_tag}"
        detail = f"dataBy={data_by}, verified={is_verified}, {vitals_str}, abnormal={abnormal_list}"

        events.append(TimelineEvent(
            timestamp_utc=utc,
            timestamp_ist=ist,
            shift=shift,
            source_file="patients.json",
            source_id=str(vital.get("_id") or ""),
            event_category="VITAL",
            event_type=VITAL_RECORDED,
            actor_name=data_by,
            actor_role=actor_role,
            actor_type=actor_type,
            summary=summary,
            detail=detail,
        ))

    return events
