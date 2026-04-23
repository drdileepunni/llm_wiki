"""
Step 2 – Filter the full timeline CSV to clinically meaningful rows.

Keeps:
  VITAL  – only abnormal readings
  LAB    – only results that contain actual values
  NOTE   – all (note_signed and note_event rows)
  TASK   – only SBAR, ABNORMAL_VITALS, and REVIEW_NEW_ADMISSION_FILE tasks
  CHAT   – only chat_message events (not deletions or file uploads)
  ORDER  – all

Output columns: timestamp_ist, event_category, event_type,
                actor_name, actor_role, summary
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

_KEEP_COLUMNS = [
    "timestamp_ist",
    "event_category",
    "event_type",
    "actor_name",
    "actor_role",
    "summary",
]

_MEANINGFUL_TASK_TYPES = [
    "REVIEW_SBAR_FOR_INVESTIGATION",
    "ABNORMAL_VITALS",
    "REVIEW_NEW_ADMISSION_FILE",
]


def _keep_row(row: pd.Series) -> bool:
    cat     = str(row.get("event_category", ""))
    etype   = str(row.get("event_type", ""))
    summary = str(row.get("summary", ""))
    detail  = str(row.get("detail", ""))

    if cat == "VITAL":
        if "no values" in summary:
            return False
        return "ABNORMAL" in summary

    if cat == "LAB":
        results_part = ""
        if "results=" in detail:
            results_part = detail.split("results=", 1)[1]
        values = re.findall(r"[\w\s]+=([^,]+)", results_part)
        return any(v.strip() for v in values)

    if cat == "NOTE":
        return True

    if cat == "TASK":
        return any(t in detail for t in _MEANINGFUL_TASK_TYPES)

    if cat == "CHAT":
        return etype == "chat_message"

    if cat == "ORDER":
        return True

    return False


def truncate(input_csv: Path, output_csv: Path) -> pd.DataFrame:
    """
    Read input_csv, filter to clinical rows, write output_csv, return DataFrame.
    """
    df = pd.read_csv(input_csv, dtype=str, on_bad_lines="skip")
    df.columns = df.columns.str.strip()

    mask    = df.apply(_keep_row, axis=1)
    df_out  = df[mask].copy()

    cols = [c for c in _KEEP_COLUMNS if c in df_out.columns]
    df_out = df_out[cols]

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(output_csv, index=False)

    return df_out
