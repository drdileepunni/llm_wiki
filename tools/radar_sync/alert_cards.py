"""
Build rich Google Chat cardsV2 alert cards for CDS problem-tracker alerts.

The card template lives here (alert domain); delivery goes through the generic
chat-microservice /send-cards endpoint. Button clicks are relayed back to this
service's /alert-feedback endpoint via the callback_url button parameter.

Add-on runtime rule: onClick.action.function must be the full HTTPS URL of the
Google Chat app's webhook — the semantic action travels in parameters.
"""
from __future__ import annotations

import copy
import logging

from tools.radar_sync.gchat_notifier import (
    _CHART_BASE,
    _STATUS_ARROW,
    _STATUS_EMOJI,
    _fetch_objective_line,
)

logger = logging.getLogger(__name__)

RATING_SECTION_HEADER = "Rate this alert"


def build_alert_card(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
    alert_id: str,
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
) -> list:
    """
    Build the cardsV2 payload for a single problem alert.
    Mirrors the sections of gchat_notifier._format_problem_payload.
    Returns the cardsV2 list (to be sent via chat-microservice /send-cards).
    """
    problem_name    = assessment.get("problem_name", "Unknown")
    clinical_status = assessment.get("clinical_status", "worsening")
    alert_reason    = assessment.get("alert_reason", "")
    suggestions     = assessment.get("suggestions") or []
    emoji           = _STATUS_EMOJI.get(clinical_status, "⚠️")

    sections: list[dict] = []

    # ── Patient overview: all problems as chips ──────────────────────────────
    all_problems: list[dict] = structured_summary.get("problems", [])
    if all_problems:
        chips = []
        for p in all_problems:
            name   = p.get("name", "?")
            arrow  = _STATUS_ARROW.get(p.get("status", "stable"), "→")
            chips.append({"label": f"{name} {arrow}"})
        sections.append({
            "header": "Patient problems",
            "widgets": [{"chipList": {"chips": chips}}],
        })

    # ── Observed (model narrative for this problem) ───────────────────────────
    current_state = ""
    for p in all_problems:
        if p.get("name") == problem_name:
            current_state = p.get("current_state", "")
            break
    if not current_state:
        current_state = assessment.get("addressed_evidence", "")
    if current_state:
        sections.append({
            "header": "Observed",
            "widgets": [{"textParagraph": {"text": current_state}}],
        })

    # ── Objective data — auto-fetched readings for the next_check item ────────
    nc = assessment.get("next_check") or {}
    nc_what = nc.get("key") or nc.get("what", "")
    nc_type = nc.get("type", "")
    if nc_what and nc_type:
        obj_line = _fetch_objective_line(cpmrn, encounter, nc_what, nc_type)
        if obj_line:
            widgets = [{"decoratedText": {
                "startIcon": {"materialIcon": {"name": "monitoring"}},
                "text": obj_line,
                "wrapText": True,
            }}]
            if nc_type == "vital":
                widgets.append({"textParagraph": {"text": (
                    "<i>⚠ Agent uses only human-verified vitals. Unverified "
                    "readings may exist — please confirm at bedside.</i>"
                )}})
            sections.append({"header": "Data", "widgets": widgets})

    # ── Why alerting ──────────────────────────────────────────────────────────
    if alert_reason:
        sections.append({
            "header": "Why alerting",
            "widgets": [{"textParagraph": {"text": alert_reason}}],
        })

    # ── Model reasoning (collapsed by default) ────────────────────────────────
    fp = assessment.get("reasoning_fingerprint")
    if isinstance(fp, dict):
        reasoning = fp.get("reasoning_chain", "")
    elif isinstance(fp, str):
        reasoning = fp
    else:
        reasoning = ""
    if reasoning:
        sections.append({
            "header": "🤖 Model reasoning",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{"textParagraph": {"text": reasoning}}],
        })

    # ── Suggested actions ─────────────────────────────────────────────────────
    if suggestions:
        sections.append({
            "header": "Suggested actions",
            "widgets": [
                {"decoratedText": {
                    "startIcon": {"materialIcon": {"name": "arrow_right"}},
                    "text": s,
                    "wrapText": True,
                }}
                for s in suggestions[:5]
            ],
        })

    # ── Rating form ───────────────────────────────────────────────────────────
    sections.append({
        "header": RATING_SECTION_HEADER,
        "widgets": [
            {"selectionInput": {
                "name": "rating",
                "label": "How appropriate is this alert?",
                "type": "RADIO_BUTTON",
                "items": [
                    {"text": "⭐ 1 — Not appropriate at all", "value": "1"},
                    {"text": "⭐⭐ 2 — Mostly inappropriate", "value": "2"},
                    {"text": "⭐⭐⭐ 3 — Borderline", "value": "3"},
                    {"text": "⭐⭐⭐⭐ 4 — Mostly appropriate", "value": "4"},
                    {"text": "⭐⭐⭐⭐⭐ 5 — Fully appropriate", "value": "5"},
                ],
            }},
            {"textInput": {
                "name": "feedback_text",
                "label": "Optional note about your rating",
                "type": "MULTIPLE_LINE",
            }},
            {"buttonList": {"buttons": [
                {
                    "text": "Submit feedback",
                    "type": "FILLED",
                    "icon": {"materialIcon": {"name": "send"}},
                    "onClick": {"action": {
                        "function": gchat_webhook_url,
                        "parameters": [
                            {"key": "action", "value": "alert_rating_submit"},
                            {"key": "action_id", "value": alert_id},
                            {"key": "callback_url", "value": callback_url},
                            {"key": "cb_token", "value": cb_token},
                        ],
                    }},
                },
                {
                    "text": "Open patient",
                    "icon": {"materialIcon": {"name": "open_in_new"}},
                    "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
                },
            ]}},
        ],
    })

    return [{
        "cardId": f"cds-alert-{alert_id}",
        "card": {
            "header": {
                "title": f"{emoji} {problem_name}",
                "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/clinical_notes/default/48px.svg",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }]


def replace_rating_section_with_status(cards_v2: list, status_text: str) -> list:
    """
    Return a copy of cardsV2 with the rating form replaced by a status line,
    so the card updates in place and can't be voted on twice.
    """
    updated = copy.deepcopy(cards_v2)
    for card_wrapper in updated:
        sections = card_wrapper.get("card", {}).get("sections", [])
        for i, section in enumerate(sections):
            if section.get("header") == RATING_SECTION_HEADER:
                sections[i] = {
                    "widgets": [
                        {"divider": {}},
                        {"decoratedText": {
                            "startIcon": {"materialIcon": {"name": "task_alt"}},
                            "text": status_text,
                            "wrapText": True,
                        }},
                    ]
                }
    return updated
