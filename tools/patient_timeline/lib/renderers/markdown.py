"""
Render a list of TimelineEvents to a shift-grouped Markdown document.
"""

from datetime import datetime, timezone
from collections import defaultdict
from ..models import TimelineEvent
from ..date_utils import IST, get_shift


_CATEGORY_WIDTH = 5  # ORDER | TASK | CHAT | NOTE

_SHIFT_SORT_KEY = {
    "Night": 1, "Day": 0,
}


def _shift_sort_key(shift_label: str) -> tuple:
    """Sort shifts chronologically: parse date + day/night."""
    try:
        parts = shift_label.rsplit(" ", 1)
        date_part = parts[0]
        time_part = parts[1] if len(parts) > 1 else "Day"
        dt = datetime.strptime(date_part, "%d %b %Y")
        # Night after Day on same date
        night_flag = 1 if time_part == "Night" else 0
        return (dt, night_flag)
    except (ValueError, IndexError):
        return (datetime.max, 0)


def render_markdown(
    events: list[TimelineEvent],
    patient: dict,
) -> str:
    """Render a Markdown audit timeline grouped by clinical shift."""
    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    name = patient.get("name") or patient.get("firstName") or ""
    last = patient.get("lastName") or ""
    full_name = " ".join(filter(None, [name, last])) or "Unknown"
    cpmrn = patient.get("CPMRN") or ""
    unit = patient.get("unitName") or ""
    bed = patient.get("bedNo") or ""
    hospital = patient.get("hospitalName") or ""

    admit_raw = patient.get("ICUAdmitDate")
    admit_str = ""
    if admit_raw:
        from ..date_utils import parse_timestamp
        _, admit_ist, _ = parse_timestamp(admit_raw)
        if admit_ist:
            admit_str = admit_ist.strftime("%d %b %Y %H:%M IST")

    now_ist = datetime.now(IST)
    generated_str = now_ist.strftime("%d %b %Y %H:%M IST")

    # Timeline span
    valid_ts = [e.timestamp_ist for e in events if e.timestamp_ist]
    span_str = ""
    if valid_ts:
        earliest = min(valid_ts)
        latest = max(valid_ts)
        delta = latest - earliest
        total_h = int(delta.total_seconds() // 3600)
        total_m = int((delta.total_seconds() % 3600) // 60)
        span_str = (f"{earliest.strftime('%d %b %Y %H:%M IST')} → "
                    f"{latest.strftime('%d %b %Y %H:%M IST')} ({total_h}h {total_m}m)")

    lines.append("# Patient Audit Timeline")
    lines.append("")
    lines.append(f"**Patient:** {full_name} &nbsp;|&nbsp; **CPMRN:** {cpmrn} &nbsp;|&nbsp; "
                 f"**Hospital:** {hospital} &nbsp;|&nbsp; **Unit:** {unit} Bed {bed}")
    if admit_str:
        lines.append(f"**ICU Admission:** {admit_str}")
    if span_str:
        lines.append(f"**Timeline span:** {span_str}")
    lines.append(f"**Generated:** {generated_str}")
    lines.append(f"**Total events:** {len(events)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Group by shift ────────────────────────────────────────────────────────
    by_shift: dict[str, list[TimelineEvent]] = defaultdict(list)
    for e in events:
        by_shift[e.shift or "Unknown"].append(e)

    for shift_label in sorted(by_shift.keys(), key=_shift_sort_key):
        shift_events = sorted(by_shift[shift_label], key=lambda e: e.timestamp_utc)

        lines.append(f"## {shift_label}")
        lines.append("")
        lines.append("| Time (IST) | Category | Event | Actor | Role |")
        lines.append("|:-----------|:---------|:------|:------|:-----|")

        for e in shift_events:
            time_str = e.timestamp_ist.strftime("%H:%M") if e.timestamp_ist else "—"
            summary = e.summary.replace("|", "\\|")
            actor = (e.actor_name or "—").replace("|", "\\|")
            role = (e.actor_role or "—").replace("|", "\\|")
            category = e.event_category
            lines.append(f"| {time_str} | {category} | {summary} | {actor} | {role} |")

        lines.append("")

    return "\n".join(lines)
