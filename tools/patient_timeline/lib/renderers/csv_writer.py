"""
Render a list of TimelineEvents to a flat CSV file.
"""

import csv
from pathlib import Path
from ..models import TimelineEvent

COLUMNS = [
    "timestamp_ist",
    "shift",
    "event_category",
    "event_type",
    "actor_name",
    "actor_role",
    "actor_type",
    "summary",
    "detail",
    "source_file",
    "source_id",
    "duration_minutes",
]


def render_csv(events: list[TimelineEvent], output_path: Path) -> None:
    """Write events to a CSV file at output_path."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()

        for e in events:
            ts_str = ""
            if e.timestamp_ist:
                ts_str = e.timestamp_ist.strftime("%Y-%m-%d %H:%M:%S%z")
                # Ensure +05:30 format (zoneinfo may emit +0530 without colon)
                if ts_str.endswith("+0530"):
                    ts_str = ts_str[:-5] + "+05:30"

            writer.writerow({
                "timestamp_ist": ts_str,
                "shift": e.shift or "",
                "event_category": e.event_category,
                "event_type": e.event_type,
                "actor_name": e.actor_name or "",
                "actor_role": e.actor_role or "",
                "actor_type": e.actor_type or "",
                "summary": e.summary or "",
                "detail": e.detail or "",
                "source_file": e.source_file or "",
                "source_id": e.source_id or "",
                "duration_minutes": f"{e.duration_minutes:.1f}" if e.duration_minutes is not None else "",
            })
