"""Send Google Chat webhook alerts for worsening/critical CDS events."""
from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_STATUS_EMOJI = {
    "critical":  "🔴",
    "worsening": "🟠",
    "stable":    "🟡",
    "improving": "🟢",
    "resolved":  "✅",
}


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
