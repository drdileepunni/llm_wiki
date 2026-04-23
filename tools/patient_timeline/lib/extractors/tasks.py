"""
Extract timeline events from tasks.json.

Each task yields up to 4 events:
  - TASK_CREATED    (always, from createdAt)
  - TASK_LOCKED     (if lockInfo.lockedAt present)
  - TASK_COMPLETED  (from history[] completed entry OR completedAt/completedBy)
  - TASK_HISTORY_ACTION (each non-completed history entry)
"""

from __future__ import annotations

from ..models import (
    TimelineEvent,
    TASK_CREATED, TASK_LOCKED, TASK_COMPLETED, TASK_HISTORY_ACTION,
)
from ..date_utils import parse_timestamp

_TASK_TYPE_LABELS = {
    "REVIEW_NEW_ADMISSION_FILE_DOCTOR": "New admission review triggered for Physician [URGENT]",
    "REVIEW_NEW_ADMISSION_FILE_NURSE":  "New admission review triggered for Nurse [URGENT]",
    "ABNORMAL_VITALS":                  "Abnormal vitals alert",
    "HERA_PROCESS":                     "HERA AI document queued",
    "HERA_SKIPPED_RECORD":              "HERA skipped record — manual review needed",
    "REVIEW_PENDED_ORDER":              "Pending order flagged for review",
    "REVIEW_SBAR_FOR_INVESTIGATION":    "SBAR raised for investigation",
    "SIGNED_NOTES":                     "Note signing event",
    "CUSTOM_TASK":                      "Custom task",
}

_SYSTEM_ACTORS = {"radar", "radar@radar.com", "system"}


def _classify_actor(name_or_email: str) -> tuple[str, str]:
    val = (name_or_email or "").lower()
    if "radar" in val or val in _SYSTEM_ACTORS:
        return "System (RADAR)", "system"
    return "", "human"


def _get_id(task: dict) -> str:
    return str(task.get("_id") or "")


def _task_summary(task: dict, event: str) -> str:
    task_type = task.get("taskType", "")
    label = _TASK_TYPE_LABELS.get(task_type, task.get("title") or task_type)

    if task_type == "ABNORMAL_VITALS":
        freq = task.get("recurringFrequency")
        if freq:
            label += f" (recurring every {freq}min)"

    if task_type == "HERA_PROCESS":
        meta = task.get("metadata") or {}
        doc_type = meta.get("type") or meta.get("name") or ""
        if doc_type:
            label += f": {doc_type}"

    if task_type == "HERA_SKIPPED_RECORD":
        meta = task.get("metadata") or {}
        fname = meta.get("name") or ""
        if fname:
            label += f": {fname}"

    if task_type in ("REVIEW_PENDED_ORDER", "REVIEW_SBAR_FOR_INVESTIGATION", "SIGNED_NOTES"):
        desc = (task.get("description") or "")[:80]
        if desc:
            label += f": {desc}"

    if task_type == "CUSTOM_TASK":
        title = task.get("title") or ""
        desc = (task.get("description") or "")[:60]
        label = f"{title}: {desc}" if desc else title

    return label


def _make_event(task, event_type, ts_raw, actor_name, actor_role, actor_type, summary, detail) -> TimelineEvent | None:
    utc, ist, shift = parse_timestamp(ts_raw)
    if utc is None:
        return None
    return TimelineEvent(
        timestamp_utc=utc,
        timestamp_ist=ist,
        shift=shift,
        source_file="tasks.json",
        source_id=_get_id(task),
        event_category="TASK",
        event_type=event_type,
        actor_name=actor_name or "",
        actor_role=actor_role or "",
        actor_type=actor_type,
        summary=summary,
        detail=detail,
    )


def extract_task_events(tasks: list[dict]) -> list[TimelineEvent]:
    events = []

    for task in tasks:
        task_type = task.get("taskType", "")
        history = task.get("history") or []
        detail_base = (f"taskType={task_type}, priority={task.get('priority','')}, "
                       f"status={task.get('status','')}, "
                       f"isRecurring={task.get('isRecurring', False)}")

        # ── TASK_CREATED ──────────────────────────────────────────────────────
        assigned_by = task.get("assignedBy") or {}
        creator_name = assigned_by.get("userName") or assigned_by.get("userEmail") or "RADAR"
        creator_role, creator_type = _classify_actor(creator_name)
        summary = _task_summary(task, TASK_CREATED)

        e = _make_event(task, TASK_CREATED, task.get("createdAt"),
                        creator_name, creator_role, creator_type, summary, detail_base)
        if e:
            events.append(e)

        # ── TASK_LOCKED ───────────────────────────────────────────────────────
        lock = task.get("lockInfo") or {}
        if lock.get("lockedAt"):
            locked_by_obj = lock.get("lockedBy") or {}
            locker = locked_by_obj.get("name") or locked_by_obj.get("userName") or "Unknown"
            locker_role = locked_by_obj.get("role") or ""
            e = _make_event(task, TASK_LOCKED, lock["lockedAt"],
                            locker, locker_role, "human",
                            f"Task locked: {summary[:60]}", detail_base)
            if e:
                events.append(e)

        # ── TASK_COMPLETED (from history or completedAt) ───────────────────
        completed_at_raw = task.get("completedAt")
        history_completed = [h for h in history if h.get("action") == "completed"]

        if history_completed:
            # use the history completion entry
            h = history_completed[-1]
            changed_by = h.get("changedBy") or {}
            comp_name = changed_by.get("userName") or "Unknown"
            comp_role = changed_by.get("userRole") or ""
            _, _, _ = parse_timestamp(task.get("createdAt"))
            utc_created, _, _ = parse_timestamp(task.get("createdAt"))
            utc_completed, _, _ = parse_timestamp(h.get("timestamp"))
            duration = None
            if utc_created and utc_completed:
                duration = (utc_completed - utc_created).total_seconds() / 60

            e = _make_event(task, TASK_COMPLETED, h.get("timestamp"),
                            comp_name, comp_role, "human",
                            f"Task completed: {summary[:60]}"
                            + (f" ({duration:.0f}min)" if duration is not None else ""),
                            detail_base)
            if e:
                e.duration_minutes = duration
                events.append(e)

        elif completed_at_raw:
            completed_by = task.get("completedBy") or {}
            comp_name = completed_by.get("userName") or "Unknown"
            comp_role = completed_by.get("userRole") or ""
            utc_created, _, _ = parse_timestamp(task.get("createdAt"))
            utc_completed, _, _ = parse_timestamp(completed_at_raw)
            duration = None
            if utc_created and utc_completed:
                duration = (utc_completed - utc_created).total_seconds() / 60

            e = _make_event(task, TASK_COMPLETED, completed_at_raw,
                            comp_name, comp_role, "human",
                            f"Task completed: {summary[:60]}"
                            + (f" ({duration:.0f}min)" if duration is not None else ""),
                            detail_base)
            if e:
                e.duration_minutes = duration
                events.append(e)

        # ── TASK_HISTORY_ACTION (non-completed entries) ───────────────────────
        for h in history:
            if h.get("action") == "completed":
                continue
            changed_by = h.get("changedBy") or {}
            h_name = changed_by.get("userName") or "Unknown"
            h_role = changed_by.get("userRole") or ""
            action = h.get("action") or "update"
            e = _make_event(task, TASK_HISTORY_ACTION, h.get("timestamp"),
                            h_name, h_role, "human",
                            f"Task {action}: {summary[:50]}", detail_base)
            if e:
                events.append(e)

    return events
