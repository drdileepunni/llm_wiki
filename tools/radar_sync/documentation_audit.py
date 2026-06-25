"""
Documentation audit module — checks whether required clinical documentation
was completed within a protocol-defined window after first problem detection.

Called by the hourly audit sweep in scheduler._run_documentation_audits.
Uses a focused LLM call over patient notes to detect which required items
are documented; writes one row to the documentation_audit BigQuery table.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_AUDIT_MODEL = "gemini-3.1-flash-lite"

_AUDIT_SYSTEM = """\
You are a clinical documentation auditor. You will be given a list of required
documentation items and a set of clinical notes written since a problem was first detected.

For each required item, determine whether a note since detection explicitly documents
that item. Return only the list of items that are clearly documented.

Be strict: a required item is only documented if a note explicitly addresses it
(a documented plan, assessment, or finding related to that item). Do not infer
from indirect mentions.

Call submit_audit_result with your findings."""

_SUBMIT_TOOL = {
    "name": "submit_audit_result",
    "description": "Submit documentation audit findings.",
    "input_schema": {
        "type": "object",
        "properties": {
            "documented_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items from required_items that are clearly documented in the notes.",
            },
            "note_count": {
                "type": "integer",
                "description": "Number of notes reviewed.",
            },
            "summary": {
                "type": "string",
                "description": "One sentence summary of documentation status.",
            },
        },
        "required": ["documented_items", "note_count"],
    },
}


def _fetch_notes_since(cpmrn: str, encounter: int, since: datetime, db: Any) -> list[dict]:
    """Fetch clinical notes written after `since` for this patient."""
    try:
        from tools.radar_sync.status_classifier import _get_notes as _gn
        notes = _gn(cpmrn, encounter) or []
        result = []
        for n in notes:
            ts = n.get("reportedAt") or n.get("createdAt") or n.get("updatedAt")
            if not ts:
                continue
            try:
                if isinstance(ts, str):
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                elif isinstance(ts, datetime):
                    dt = ts
                else:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if since.tzinfo is None:
                    since_utc = since.replace(tzinfo=timezone.utc)
                else:
                    since_utc = since
                if dt >= since_utc:
                    result.append(n)
            except Exception:
                continue
        return result
    except Exception:
        logger.exception("documentation_audit: failed to fetch notes for %s enc=%d", cpmrn, encounter)
        return []


def _format_notes(notes: list[dict]) -> str:
    lines = []
    for n in notes[:20]:  # cap at 20 notes to stay within context
        ts = n.get("reportedAt") or n.get("createdAt") or ""
        author = n.get("author") or n.get("createdBy") or "unknown"
        text = n.get("text") or n.get("content") or n.get("note") or ""
        if text:
            lines.append(f"[{ts}] {author}:\n{text[:800]}")
    return "\n\n".join(lines) if lines else "(no notes found)"


def check_documentation(
    cpmrn: str,
    encounter: int,
    audit_spec: dict,
    detected_at: datetime,
    client: Any,
    db: Any,
) -> dict:
    """
    Run a focused LLM audit for one (problem, protocol) pair.

    Returns a result dict suitable for insert_documentation_audit:
      documented_items, missing_items, note_count, verdict, summary
    """
    required = audit_spec.get("required_documentation") or []
    record_when_none = audit_spec.get("record_when_none", "inadequate_documentation")

    notes = _fetch_notes_since(cpmrn, encounter, detected_at, db)
    notes_text = _format_notes(notes)

    if not notes:
        return {
            "documented_items": [],
            "missing_items": required,
            "note_count": 0,
            "verdict": record_when_none,
            "summary": "No notes found since detection.",
        }

    required_list = "\n".join(f"  - {item}" for item in required)
    user_msg = (
        f"Required documentation items:\n{required_list}\n\n"
        f"Clinical notes since detection ({len(notes)} note(s)):\n\n{notes_text}\n\n"
        "Which of the required items are explicitly documented in these notes?"
    )

    try:
        resp = client.create_message(
            messages=[{"role": "user", "content": user_msg}],
            tools=[_SUBMIT_TOOL],
            system=_AUDIT_SYSTEM,
            max_tokens=1024,
            force_tool=True,
        )

        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and block.name == "submit_audit_result":
                inp = block.input
                documented = inp.get("documented_items") or []
                note_count = inp.get("note_count") or len(notes)
                summary = inp.get("summary") or ""
                missing = [item for item in required if item not in documented]
                verdict = "adequate_documentation" if not missing else record_when_none
                return {
                    "documented_items": documented,
                    "missing_items": missing,
                    "note_count": note_count,
                    "verdict": verdict,
                    "summary": summary,
                }

    except Exception:
        logger.exception(
            "documentation_audit: LLM call failed for %s enc=%d", cpmrn, encounter
        )

    # Fallback if LLM failed
    return {
        "documented_items": [],
        "missing_items": required,
        "note_count": len(notes),
        "verdict": record_when_none,
        "summary": "Audit LLM call failed — marking as incomplete.",
    }
