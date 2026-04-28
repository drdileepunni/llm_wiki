"""
Extract timeline events from patient.notes.finalNotes[].

Two event types per signed note:
  - NOTE_SIGNED: always emitted for each signed clinical note
  - NOTE_EVENT:  Gemini-extracted discrete clinical events (batched, BATCH_SIZE notes per call)

Gemini is skipped silently if:
  - GEMINI_API_KEY is not in env (load_dotenv is called in the CLI entrypoint), OR
  - --no-gemini flag was passed (gemini_enabled=False)
"""

from __future__ import annotations

import os
import json
import logging
import math
from datetime import datetime, timezone, timedelta

from ..models import TimelineEvent, NOTE_SIGNED, NOTE_EVENT
from ..date_utils import parse_timestamp, strip_html, get_shift, IST

logger = logging.getLogger(__name__)

BATCH_SIZE = 10  # notes per Gemini call

_CLINICAL_ROLES = {
    "doctor", "physician", "intensivist",
    "nurse", "registered nurse", "critical care registered nurse",
    "ccrn", "consultant",
}


def _is_clinical_author(role: str) -> bool:
    return any(r in role.lower() for r in _CLINICAL_ROLES)


def _get_id(obj: dict) -> str:
    return str(obj.get("_id") or "")


def _concat_components(content_entry: dict) -> str:
    parts = []
    for comp in content_entry.get("components") or []:
        val = comp.get("value") or ""
        parts.append(strip_html(val))
    return " ".join(filter(None, parts))


def _safe_json_parse(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        inner = []
        for line in text.split("\n"):
            if line.startswith("```"):
                continue
            inner.append(line)
        text = "\n".join(inner).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse Gemini JSON: %s", text[:300])
        return None


def _resolve_event_timestamp(time_mentioned: str, note_ist: datetime) -> tuple[datetime, datetime, str]:
    """
    Given a "HH:MM" string and the note's IST datetime, return (utc_naive, ist_aware, shift).
    If the resulting time is >2h before the note, advance by 1 day (handles cross-midnight notes).
    Falls back to note's own timestamps on any parse error.
    """
    try:
        h, m = time_mentioned.split(":")[:2]
        candidate = note_ist.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if (note_ist - candidate).total_seconds() > 7200:
            candidate = candidate + timedelta(days=1)
        ev_ist = candidate
        ev_utc = ev_ist.astimezone(timezone.utc).replace(tzinfo=None)
        return ev_utc, ev_ist, get_shift(ev_ist)
    except (ValueError, AttributeError):
        ev_utc = note_ist.astimezone(timezone.utc).replace(tzinfo=None)
        return ev_utc, note_ist, get_shift(note_ist)


def _call_gemini_batch(note_batch: list[dict], api_key: str) -> list[list[dict]]:
    """
    Send a batch of notes to Gemini in a single call.
    note_batch: list of {index, note_type, author_role, timestamp_ist_str, plain_text}
    Returns a list (parallel to note_batch) of event-list dicts.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai not installed — skipping Gemini note extraction")
        return [[] for _ in note_batch]

    # Build the batched prompt
    notes_section = ""
    for item in note_batch:
        notes_section += (
            f"\n--- NOTE {item['index']} ---\n"
            f"Type: {item['note_type']} | "
            f"Author: {item['author_role']} | "
            f"Timestamp: {item['timestamp_ist_str']}\n"
            f"{item['plain_text']}\n"
        )

    prompt = f"""You are analyzing ICU clinical notes. For each note, extract discrete clinical events.

For each event:
- event_text: one-line description (≤100 chars)
- time_mentioned: "HH:MM" 24h if an explicit time is in the note, else null
- event_category: one of: vitals, medication, procedure, assessment, labs, clinical_finding, other

Use an empty events array for notes with no discrete events.
{notes_section}"""

    # Response schema enforces exact output structure
    response_schema = {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_index": {"type": "integer"},
                        "events": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "event_text":      {"type": "string"},
                                    "time_mentioned":  {"type": "string", "nullable": True},
                                    "event_category":  {"type": "string"},
                                },
                                "required": ["event_text", "time_mentioned", "event_category"],
                            },
                        },
                    },
                    "required": ["note_index", "events"],
                },
            },
        },
        "required": ["notes"],
    }

    try:
        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=response_schema,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            config=config,
        )
        # response.text raises if the response was blocked/cut; fall back gracefully
        try:
            raw = response.text or ""
        except Exception:
            raw = ""
            for part in (response.candidates or [{}])[0].get("content", {}).get("parts", []):
                raw += part.get("text", "")
        parsed = _safe_json_parse(raw)

        if not parsed or not isinstance(parsed.get("notes"), list):
            logger.warning("Unexpected Gemini response structure: %s", raw[:200])
            return [[] for _ in note_batch]

        # Build index → events map
        result_map: dict[int, list[dict]] = {}
        for note_result in parsed["notes"]:
            idx = note_result.get("note_index")
            evs = note_result.get("events") or []
            if isinstance(idx, int) and isinstance(evs, list):
                result_map[idx] = evs

        return [result_map.get(item["index"], []) for item in note_batch]

    except Exception as e:
        logger.warning("Gemini batch call failed: %s", e)
        return [[] for _ in note_batch]


def extract_note_events(patient: dict, gemini_enabled: bool = True) -> list[TimelineEvent]:
    from ... import config
    api_key = config.GEMINI_API_KEY
    use_gemini = gemini_enabled and bool(api_key)

    if gemini_enabled and not api_key:
        logger.warning("GEMINI_API_KEY not set — skipping Gemini note extraction")

    notes_section = patient.get("notes") or {}
    final_notes = notes_section.get("finalNotes") or []

    # ── Pass 1: collect valid signed notes and emit NOTE_SIGNED events ────────
    events: list[TimelineEvent] = []
    gemini_queue: list[dict] = []  # notes to send to Gemini

    for note_group in final_notes:
        content_list = note_group.get("content") or []
        note_group_id = _get_id(note_group)

        signed_entries = [c for c in content_list if c.get("pendOrSigned") == "signed"]
        if not signed_entries:
            continue

        signed_entries.sort(key=lambda c: str(c.get("timestamp") or ""))
        signed = signed_entries[-1]

        author = signed.get("author") or {}
        author_name = author.get("name") or "Unknown"
        author_role = author.get("role") or "Unknown"
        note_type = signed.get("noteType") or "Note"
        note_sub_type = signed.get("noteSubType") or ""

        if not _is_clinical_author(author_role):
            continue

        utc, ist, shift = parse_timestamp(signed.get("timestamp"))
        if utc is None:
            continue

        plain_text = _concat_components(signed)

        note_label = f"{note_type} note"
        if note_sub_type:
            note_label += f" ({note_sub_type})"

        events.append(TimelineEvent(
            timestamp_utc=utc,
            timestamp_ist=ist,
            shift=shift,
            source_file="patients.json",
            source_id=note_group_id,
            event_category="NOTE",
            event_type=NOTE_SIGNED,
            actor_name=author_name,
            actor_role=author_role,
            actor_type="human",
            summary=f"{note_label} signed by {author_name} ({author_role})",
            detail=plain_text[:500],
        ))

        if use_gemini and plain_text.strip():
            gemini_queue.append({
                "index": len(gemini_queue),
                "note_group_id": note_group_id,
                "note_type": note_type,
                "author_name": author_name,
                "author_role": author_role,
                "timestamp_ist_str": ist.strftime("%Y-%m-%d %H:%M IST"),
                "ist": ist,
                "plain_text": plain_text,
            })

    # ── Pass 2: batch Gemini calls ────────────────────────────────────────────
    if not gemini_queue:
        return events

    num_batches = math.ceil(len(gemini_queue) / BATCH_SIZE)
    logger.info("Calling Gemini for %d notes in %d batch(es) of up to %d …",
                len(gemini_queue), num_batches, BATCH_SIZE)

    for batch_num in range(num_batches):
        batch = gemini_queue[batch_num * BATCH_SIZE:(batch_num + 1) * BATCH_SIZE]
        logger.info("  Batch %d/%d: %d notes", batch_num + 1, num_batches, len(batch))

        # Re-index within the batch so Gemini gets 0-based indices per call
        for i, item in enumerate(batch):
            item["index"] = i

        batch_results = _call_gemini_batch(batch, api_key)

        for item, extracted_events in zip(batch, batch_results):
            for ev in extracted_events:
                event_text = (ev.get("event_text") or "").strip()
                time_mentioned = (ev.get("time_mentioned") or "").strip()
                ev_category = (ev.get("event_category") or "other").lower()

                if not event_text:
                    continue

                if time_mentioned and ":" in time_mentioned:
                    ev_utc, ev_ist, ev_shift = _resolve_event_timestamp(time_mentioned, item["ist"])
                else:
                    ev_ist = item["ist"]
                    ev_utc = ev_ist.astimezone(timezone.utc).replace(tzinfo=None)
                    ev_shift = get_shift(ev_ist)

                events.append(TimelineEvent(
                    timestamp_utc=ev_utc,
                    timestamp_ist=ev_ist,
                    shift=ev_shift,
                    source_file="patients.json",
                    source_id=item["note_group_id"],
                    event_category="NOTE",
                    event_type=NOTE_EVENT,
                    actor_name=item["author_name"],
                    actor_role=item["author_role"],
                    actor_type="human",
                    summary=f"[{ev_category}] {event_text}",
                    detail=f"extracted from {item['note_type']} note by {item['author_name']}",
                ))

    return events
