from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ── Order event types ─────────────────────────────────────────────────────────
ORDER_MED_PLACED        = "medication_placed"
ORDER_MED_COMPLETED     = "medication_completed"
ORDER_MED_DISCONTINUED  = "medication_discontinued"
ORDER_LAB_PLACED        = "lab_placed"
ORDER_LAB_SIGNED        = "lab_signed"
ORDER_DIET_PLACED       = "diet_placed"
ORDER_DIET_DISCONTINUED = "diet_discontinued"
ORDER_PROCEDURE_PLACED  = "procedure_placed"
ORDER_VENT_PLACED       = "vent_placed"
ORDER_BLOOD_PLACED      = "blood_placed"

# ── Task event types ──────────────────────────────────────────────────────────
TASK_CREATED        = "task_created"
TASK_LOCKED         = "task_locked"
TASK_COMPLETED      = "task_completed"
TASK_HISTORY_ACTION = "task_history_action"

# ── Chat event types ──────────────────────────────────────────────────────────
CHAT_MESSAGE     = "chat_message"
CHAT_FILE_UPLOAD = "chat_file_upload"
CHAT_ACKNOWLEDGED = "chat_acknowledged"
CHAT_DELETED     = "chat_deleted"

# ── Note event types ──────────────────────────────────────────────────────────
NOTE_SIGNED = "note_signed"
NOTE_EVENT  = "note_event"

# ── Vital event types ─────────────────────────────────────────────────────────
VITAL_RECORDED = "vital_recorded"

# ── Lab result event types ────────────────────────────────────────────────────
LAB_RESULT = "lab_result"


@dataclass
class TimelineEvent:
    timestamp_utc: datetime           # naive UTC — used for sorting
    timestamp_ist: datetime           # tz-aware IST — used for display
    shift: str                        # e.g. "11 Apr 2026 Day"
    source_file: str                  # "patients.json" | "tasks.json" | "chat.json"
    source_id: str                    # orderNo / task _id / note _id / chat _id
    event_category: str               # ORDER | TASK | CHAT | NOTE
    event_type: str
    actor_name: str
    actor_role: str                   # "Clinician" | "System (RADAR)" | etc.
    actor_type: str                   # "human" | "system"
    summary: str                      # one-liner for Markdown table
    detail: str                       # full text for CSV
    duration_minutes: float | None = None  # TASK_COMPLETED only
