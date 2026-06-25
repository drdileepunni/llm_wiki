"""
clinical_timeline.py — Two-zone patient event timeline.

The timeline is maintained by the problem_tracker's reasoning call (cost-neutral —
no extra LLM call) and read by both status_classifier and problem_tracker on the
next run.

Storage model (field `clinical_timeline` on patient_contexts):
  {
    "prior_course": "<compressed prose — settled history, ≤1500 chars>",
    "recent_events": [
      {
        "t":       "2026-06-23T14:00:00Z",
        "event":   "NPO ordered for suspected SBO",
        "problem": "Subacute Intestinal Obstruction",
        "kind":    "order",                     # order | finding | intervention | status_change
        "source":  "note@2026-06-23T14:00"      # anti-hallucination: must cite real data
      }
    ]
  }

Windowing (fold-on-eviction):
  An event stays in recent_events while both:
    - within last RECENT_DAYS (7 days), AND
    - within last RECENT_MAX_EVENTS (30 events)
  Anything failing either test is evicted → folded into prior_course.
  Roll-up only runs when something actually ages out (rare, append-mostly).

No-data-loss guarantee: events are dropped from recent_events only when
  apply_updates() receives a non-None emitted_prior (the model successfully
  generated an updated prior_course fold). If emitted_prior is None,
  to_evict events remain in recent_events and retry next run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

RECENT_DAYS            = 7
RECENT_MAX_EVENTS      = 30
PRIOR_COURSE_MAX_CHARS = 1500

# ── Tool schema fragment (embedded into set_all_assessments) ───────────────────

TIMELINE_EVENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "t": {
            "type": "string",
            "description": "ISO 8601 UTC timestamp of the event (e.g. '2026-06-25T09:20:00Z').",
        },
        "event": {
            "type": "string",
            "description": "One concise sentence describing what happened.",
        },
        "problem": {
            "type": "string",
            "description": (
                "Problem name this event relates to, or 'general' for cross-cutting events."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["order", "finding", "intervention", "status_change"],
            "description": (
                "order — a management order was placed or changed; "
                "finding — a new clinical finding or result; "
                "intervention — a procedure or treatment was performed; "
                "status_change — the patient's clinical state changed."
            ),
        },
        "source": {
            "type": "string",
            "description": (
                "The data item this event was derived from. Format: "
                "'note@<ISO-timestamp>', 'lab@<name>:<value>', or 'vital@<name>:<value>'. "
                "REQUIRED — do not emit an event without a source."
            ),
        },
    },
    "required": ["t", "event", "problem", "kind", "source"],
}


# ── Core data helpers ──────────────────────────────────────────────────────────

def empty_timeline() -> dict:
    return {"prior_course": "", "recent_events": []}


class _Partition(NamedTuple):
    kept: list[dict]
    to_evict: list[dict]


def _parse_event_time(ev: dict, now: datetime) -> datetime:
    try:
        t = ev.get("t", "")
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception:
        return now  # malformed timestamp → treat as recent, don't evict


def partition(timeline: dict, now: datetime) -> _Partition:
    """
    Split recent_events into kept (within window) and to_evict (outside window).
    Window: last RECENT_DAYS days AND last RECENT_MAX_EVENTS events.
    Failing either constraint evicts the event.
    """
    recent = list(timeline.get("recent_events") or [])
    if not recent:
        return _Partition(kept=[], to_evict=[])

    cutoff_time = now - timedelta(days=RECENT_DAYS)
    if now.tzinfo is None:
        cutoff_time = cutoff_time.replace(tzinfo=timezone.utc)
        now = now.replace(tzinfo=timezone.utc)

    sorted_events = sorted(recent, key=lambda ev: _parse_event_time(ev, now))

    # Count cap: keep only the last RECENT_MAX_EVENTS
    if len(sorted_events) > RECENT_MAX_EVENTS:
        evict_by_count = sorted_events[: len(sorted_events) - RECENT_MAX_EVENTS]
        sorted_events  = sorted_events[len(sorted_events) - RECENT_MAX_EVENTS :]
    else:
        evict_by_count = []

    # Time cap: evict anything older than cutoff
    evict_by_time = [ev for ev in sorted_events if _parse_event_time(ev, now) < cutoff_time]
    kept          = [ev for ev in sorted_events if _parse_event_time(ev, now) >= cutoff_time]

    # Dedupe (an event may appear in both cap lists)
    seen: set[str] = set()
    deduped_evict: list[dict] = []
    for ev in evict_by_count + evict_by_time:
        key = f"{ev.get('t', '')}\x1f{ev.get('event', '')}"
        if key not in seen:
            seen.add(key)
            deduped_evict.append(ev)

    return _Partition(kept=kept, to_evict=deduped_evict)


def _fmt_event(ev: dict) -> str:
    t   = (ev.get("t") or "?")[:16].replace("T", " ")
    prb = ev.get("problem", "")
    knd = ev.get("kind", "")
    msg = ev.get("event", "")
    src = ev.get("source", "")
    return f"  {t}  [{prb}] {knd} — {msg}  (src: {src})"


def render(timeline: dict) -> str:
    """
    Read-only rendered string for injection into status_classifier prefetch.
    Returns empty string if the timeline has no content.
    """
    if not timeline:
        return ""
    prior  = (timeline.get("prior_course") or "").strip()
    recent = timeline.get("recent_events") or []
    if not prior and not recent:
        return ""

    lines = ["== CLINICAL TIMELINE =="]
    if prior:
        lines.append(f"Prior course: {prior}")
        lines.append("")
    if recent:
        lines.append("Recent events (oldest → newest):")
        for ev in recent:
            lines.append(_fmt_event(ev))
    return "\n".join(lines)


def render_for_tracker(
    timeline: dict,
    now: datetime,
) -> tuple[str, list[dict], str]:
    """
    Render timeline for the problem_tracker user message.
    Returns (rendered_str, to_evict, fold_instruction).

    fold_instruction is non-empty only when there are events to evict.
    The tracker should emit `prior_course` only when fold_instruction is present.
    """
    part     = partition(timeline, now)
    to_evict = part.to_evict
    kept     = part.kept
    prior    = (timeline.get("prior_course") or "").strip()

    lines = ["== CLINICAL TIMELINE =="]
    if prior:
        lines.append(f"Prior course: {prior}")
        lines.append("")
    if kept:
        lines.append("Recent events (oldest → newest):")
        for ev in kept:
            lines.append(_fmt_event(ev))
    else:
        lines.append("(No events recorded yet — seed from current summary and notes.)")

    rendered = "\n".join(lines)

    fold_instruction = ""
    if to_evict:
        evict_text = "\n".join(_fmt_event(ev) for ev in to_evict)
        fold_instruction = (
            "TIMELINE FOLD REQUIRED — the following events have aged out of the recent "
            f"window ({RECENT_DAYS}d / {RECENT_MAX_EVENTS} events) and must be compressed "
            f"into prior_course:\n{evict_text}\n\n"
            "Emit `prior_course` in set_all_assessments: preserve the existing prior_course "
            "text verbatim, then append the aged-out events as brief clauses. "
            f"Keep the result under {PRIOR_COURSE_MAX_CHARS} chars; if it would exceed the "
            "cap, condense only the OLDEST portion of the existing prior_course prose."
        )

    return rendered, to_evict, fold_instruction


def apply_updates(
    timeline: dict,
    new_events: list[dict],
    emitted_prior: str | None,
    to_evict: list[dict],
) -> dict:
    """
    Assemble the post-run timeline.

    - Appends new_events to recent_events (deduped by t+event).
    - If emitted_prior is not None (fold succeeded): drops to_evict from
      recent_events and updates prior_course.
    - If emitted_prior is None (fold absent): to_evict events stay in
      recent_events — no data loss, retried next run.
    """
    prior  = (timeline.get("prior_course") or "")
    recent = list(timeline.get("recent_events") or [])

    if emitted_prior is not None:
        evict_keys = {
            f"{ev.get('t', '')}\x1f{ev.get('event', '')}" for ev in to_evict
        }
        recent = [
            ev for ev in recent
            if f"{ev.get('t', '')}\x1f{ev.get('event', '')}" not in evict_keys
        ]
        prior = emitted_prior.strip()[:PRIOR_COURSE_MAX_CHARS]

    existing_keys = {f"{ev.get('t', '')}\x1f{ev.get('event', '')}" for ev in recent}
    for ev in (new_events or []):
        key = f"{ev.get('t', '')}\x1f{ev.get('event', '')}"
        if key not in existing_keys:
            recent.append(ev)
            existing_keys.add(key)

    return {"prior_course": prior, "recent_events": recent}
