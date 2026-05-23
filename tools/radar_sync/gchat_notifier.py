"""Send Google Chat webhook alerts for worsening/critical CDS events."""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_CHART_BASE = "https://cloudphysicianworld.com/patient"

_STATUS_EMOJI = {
    "critical":  "🔴",
    "worsening": "🟠",
    "stable":    "🟡",
    "improving": "🟢",
    "resolved":  "✅",
}

_STATUS_ARROW = {
    "critical":  "↑↑",
    "worsening": "↑",
    "stable":    "→",
    "improving": "↓",
    "resolved":  "✓",
}


def _fetch_objective_line(cpmrn: str, encounter: int, what: str, nc_type: str) -> str:
    """
    Fetch the last 3 real readings for a vital or lab and format as a compact
    one-liner for the alert card. Empty/gap readings are noted but not listed.

    Examples:
      "BP: 176/101 at 10:34 AM, 185/112 at 09:24 AM  [+7 monitor gap reading(s)]"
      "WBC: 9,200 at 06:11 AM (May 21)"
    """
    try:
        from tools.radar_sync.status_classifier import _get_vital_trend, _get_lab_trend, _get_io

        if nc_type == "vital":
            raw = _get_vital_trend(cpmrn, encounter, what, 3)
        elif nc_type == "io":
            raw = _get_io(cpmrn, encounter, n_hours=6)
        else:
            raw = _get_lab_trend(cpmrn, encounter, what, 3)

        if not raw or raw.startswith("Unknown") or raw.startswith("No "):
            # Pass through the gap note if present
            if "monitor gap" in raw or "no recorded value" in raw.lower():
                return raw.split("\n")[0]  # first line has the summary
            return raw

        # I/O data: prefer the daily total line for the most recent day,
        # then fall back to the TOTAL (hourly window) summary line.
        if nc_type == "io":
            # Look for "Day N:" lines — pick the most recent (first one found)
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("Day ") and "In " in line and "UO " in line:
                    return line
            # Fallback: hourly TOTAL line
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if line.startswith("TOTAL"):
                    return line
            return raw.splitlines()[0]

        # Parse "  [timestamp] value" lines — skip the header
        reading_lines = [l.strip() for l in raw.splitlines() if l.strip().startswith("[")]
        note_lines    = [l.strip() for l in raw.splitlines() if l.strip().startswith("Note:")]

        if not reading_lines:
            return ""

        parts = [f"{what}: " + "  →  ".join(reading_lines[:3])]
        if note_lines:
            parts.append(f"({note_lines[0]})")
        return "  ".join(parts)

    except Exception:
        logger.exception("gchat_notifier: _fetch_objective_line failed for %s %s", cpmrn, what)
        return ""


def _format_problem_payload(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
) -> dict:
    """
    Build a focused, structured alert for a single problem flagged by the problem tracker.

    Sections:
      Header   — CPMRN + problem name + status emoji
      Patient  — all active problems as a one-liner (quick context)
      Observed — current_state for this problem from the structured summary
      Alert    — alert_reason (why the model is raising this now)
      Actions  — bulleted suggestions
      Link     — chart URL
    """
    problem_name    = assessment.get("problem_name", "Unknown")
    clinical_status = assessment.get("clinical_status", "worsening")
    alert_reason    = assessment.get("alert_reason", "")
    suggestions     = assessment.get("suggestions") or []

    emoji = _STATUS_EMOJI.get(clinical_status, "⚠️")

    # ── Header ────────────────────────────────────────────────────────────────
    lines = [f"{emoji} *{cpmrn}* — {problem_name}  |  Encounter {encounter}"]
    lines.append("")

    # ── Patient overview: all problems as a one-liner ─────────────────────────
    all_problems: list[dict] = structured_summary.get("problems", [])
    if all_problems:
        parts = []
        for p in all_problems:
            name   = p.get("name", "?")
            status = p.get("status", "stable")
            arrow  = _STATUS_ARROW.get(status, "→")
            # Bold the alerting problem so it stands out
            if name == problem_name:
                parts.append(f"*{name} {arrow}*")
            else:
                parts.append(f"{name} {arrow}")
        lines.append("*Patient:*  " + "  |  ".join(parts))
        lines.append("")

    # ── What was observed (model narrative) ──────────────────────────────────
    current_state = ""
    for p in all_problems:
        if p.get("name") == problem_name:
            current_state = p.get("current_state", "")
            break
    if not current_state:
        current_state = assessment.get("addressed_evidence", "")
    if current_state:
        lines.append(f"*Observed:*  {current_state}")
        lines.append("")

    # ── Objective data — auto-fetched readings for the next_check item ────────
    nc = assessment.get("next_check") or {}
    # Support new schema (key/label) and old schema (what) for backward compat
    nc_what  = nc.get("key")  or nc.get("what", "")   # machine fetch argument
    nc_label = nc.get("label") or nc_what              # human-readable display text
    nc_type  = nc.get("type", "")
    if nc_what and nc_type:
        obj_line = _fetch_objective_line(cpmrn, encounter, nc_what, nc_type)
        if obj_line:
            lines.append(f"*Data:*  {obj_line}")
            if nc_type == "vital":
                lines.append(
                    "_⚠ Agent uses only human-verified vitals. "
                    "Unverified readings may exist — please confirm at bedside._"
                )
            lines.append("")

    # ── Why alerting ─────────────────────────────────────────────────────────
    if alert_reason:
        lines.append(f"*Why alerting:*  {alert_reason}")
        lines.append("")

    # ── Note citations ────────────────────────────────────────────────────────
    cited_notes: list[dict] = assessment.get("cited_notes") or []
    if cited_notes:
        lines.append("*Evidence from notes:*")
        for c in cited_notes[:3]:   # cap at 3 to keep card readable
            ts        = c.get("timestamp", "")
            note_type = c.get("note_type", "Note")
            author    = c.get("author", "")
            quote     = c.get("quote", "").strip()
            meta = f"📄 {note_type}"
            if author:
                meta += f"  |  {author}"
            if ts:
                meta += f"  |  {ts}"
            lines.append(meta)
            if quote:
                display_quote = quote[:160] + ("…" if len(quote) > 160 else "")
                lines.append(f'_"{display_quote}"_')
        lines.append("")

    # ── Suggested actions ─────────────────────────────────────────────────────
    if suggestions:
        lines.append("*Suggested actions:*")
        for s in suggestions[:5]:
            lines.append(f"• {s}")
        lines.append("")

    # ── Chart link ────────────────────────────────────────────────────────────
    lines.append(f"🔗 {_CHART_BASE}/{cpmrn}/{encounter}")

    return {"text": "\n".join(lines)}


def send_problem_alert(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
    webhook_url: str,
) -> bool:
    """
    Send a structured Google Chat alert for a single problem from the problem tracker.
    Uses assessment + structured_summary directly — no extra LLM calls needed.
    """
    payload = _format_problem_payload(cpmrn, encounter, assessment, structured_summary)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info(
            "gchat_notifier: problem alert sent for %s enc=%d problem=%s (HTTP %d)",
            cpmrn, encounter, assessment.get("problem_name", "?"), resp.status_code,
        )
        return True
    except Exception:
        logger.exception(
            "gchat_notifier: failed to send problem alert for %s enc=%d",
            cpmrn, encounter,
        )
        return False


def _format_payload(cpmrn: str, encounter: int, triggering_problems: list[str],
                    structured_summary: dict, cds_result: dict) -> dict:
    """Build a Google Chat text payload (reliable across all webhook versions)."""
    problems: list[dict] = structured_summary.get("problems", [])

    lines = [f"*⚠️ CDS Alert — {cpmrn}* (Encounter {encounter})"]
    lines.append("")

    # Worsening/critical problems
    for p in problems:
        if p.get("status") not in ("worsening", "critical"):
            continue
        emoji = _STATUS_EMOJI.get(p["status"], "⚠️")
        lines.append(f"{emoji} *{p['name']}* [{p['status'].upper()}]")
        state = p.get("current_state", "")
        if state:
            lines.append(f"   {state}")

    # CDS suggestions
    immediate = cds_result.get("immediate_actions") or []
    next_steps = cds_result.get("immediate_next_steps") or []
    all_actions = (immediate + next_steps)[:6]
    if all_actions:
        lines.append("")
        lines.append("*Suggested actions:*")
        for a in all_actions:
            lines.append(f"• {a}")

    # Narrative snippet
    narrative = structured_summary.get("narrative", "")
    if narrative:
        snippet = narrative[:350] + ("…" if len(narrative) > 350 else "")
        lines.append("")
        lines.append(f"_{snippet}_")

    return {"text": "\n".join(lines)}


def send_gchat_alert(
    cpmrn: str,
    encounter: int,
    triggering_problems: list[str],
    structured_summary: dict,
    cds_result: dict,
    webhook_url: str,
) -> bool:
    """
    POST a Google Chat card alert. Returns True on success.
    """
    payload = _format_payload(cpmrn, encounter, triggering_problems, structured_summary, cds_result)
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("gchat_notifier: alert sent for %s enc=%d (HTTP %d)", cpmrn, encounter, resp.status_code)
        return True
    except Exception:
        logger.exception("gchat_notifier: failed to send alert for %s enc=%d", cpmrn, encounter)
        return False


def send_test_message(webhook_url: str) -> bool:
    """Send a simple test ping to verify the webhook URL."""
    payload = {"text": "✅ LLM Wiki alert webhook connected successfully."}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("gchat_notifier: test message failed")
        return False
