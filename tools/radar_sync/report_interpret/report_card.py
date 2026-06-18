"""
Build the Google Chat cardsV2 "new diagnostic report(s) resulted" card.

ONE batched card per patient per cycle. Each newly resulted report gets its own
section group: a header (report type + relative timing), the clinical interpretation
shown inline, a collapsed accordion with the verbatim description/transcription, a
collapsed accordion with the report image(s), and a per-report 1–5 feedback rating.

Mirrors the conventions in alert_cards.py / order_recon_card.py:
  * onClick.action.function = full HTTPS gchat webhook URL; intent travels in parameters.
  * collapsible accordions use {"collapsible": True, "uncollapsibleWidgetsCount": 0}.
  * a replace_rating_section_with_status() helper updates the card in place after submit.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta

_CHART_BASE = "https://cloudphysicianworld.com/patient"
_IST = timezone(timedelta(hours=5, minutes=30))

_RATING_PREFIX = "Rate report:"


def _fmt_ist(dt_iso) -> str:
    """Format an ISO/datetime timestamp as 'Jun 15, 9:07 PM IST'. Returns '' if unparseable."""
    if not dt_iso:
        return ""
    try:
        if isinstance(dt_iso, datetime):
            dt = dt_iso if dt_iso.tzinfo else dt_iso.replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(dt_iso).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_IST).strftime("%-d %b, %-I:%M %p IST")
    except Exception:
        return ""


_SEVERITY_EMOJI = {"critical": "🔴", "notable": "🟠", "routine": "🟢"}


def _report_sections(report: dict, gchat_webhook_url: str, callback_url: str,
                     cb_token: str, has_divider: bool) -> list[dict]:
    """Build the section group for one report."""
    report_id   = report["report_id"]
    rtype       = (report.get("report_type") or "report").upper()
    name        = report.get("report_name") or rtype
    ts          = _fmt_ist(report.get("reported_at"))
    interp      = report.get("interpretation") or "(no interpretation produced)"
    description = report.get("description") or "(no description produced)"
    image_urls  = report.get("image_urls") or []
    findings    = report.get("findings") or []

    sections: list[dict] = []

    # ── Report header section (acts as title within the batched card) ──
    title = f"🩻 {name}"
    if ts:
        title += f"  ·  {ts}"
    header_section: dict = {"header": title, "widgets": []}
    if has_divider:
        header_section["hasDivider"] = True
    sections.append(header_section)

    # ── Interpretation (inline, prominent) ──
    sections.append({
        "header": "Interpretation",
        "widgets": [{"decoratedText": {
            "startIcon": {"materialIcon": {"name": "clinical_notes"}},
            "text": interp,
            "wrapText": True,
        }}],
    })

    # ── Key findings chips (inline) — only when present ──
    if findings:
        chips = []
        for f in findings:
            sev = (f.get("severity") or "routine").lower()
            emoji = _SEVERITY_EMOJI.get(sev, "•")
            chips.append({"label": f"{emoji} {f.get('label', '')}"})
        sections.append({
            "header": "Findings",
            "widgets": [{"chipList": {"chips": chips}}],
        })

    # ── Description / transcription (collapsed accordion) ──
    sections.append({
        "header": "📄 Description / transcription",
        "collapsible": True,
        "uncollapsibleWidgetsCount": 0,
        "widgets": [{"textParagraph": {"text": description}}],
    })

    # ── Report image(s) (collapsed accordion) ──
    if image_urls:
        sections.append({
            "header": "🖼️ Report image",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [
                {"image": {"imageUrl": url, "onClick": {"openLink": {"url": url}}}}
                for url in image_urls
            ],
        })

    # ── Per-report feedback rating ──
    sections.append({
        "header": f"{_RATING_PREFIX} {name}",
        "widgets": [
            {"selectionInput": {
                "name": "rating",
                "label": "How accurate / useful is this interpretation?",
                "type": "RADIO_BUTTON",
                "items": [
                    {"text": "⭐ 1 — Not useful at all",    "value": "1"},
                    {"text": "⭐⭐ 2 — Mostly not useful",   "value": "2"},
                    {"text": "⭐⭐⭐ 3 — Borderline",          "value": "3"},
                    {"text": "⭐⭐⭐⭐ 4 — Mostly useful",     "value": "4"},
                    {"text": "⭐⭐⭐⭐⭐ 5 — Very useful",     "value": "5"},
                ],
            }},
            {"textInput": {
                "name": "feedback_text",
                "label": "Optional note about your rating",
                "type": "MULTIPLE_LINE",
            }},
            {"buttonList": {"buttons": [{
                "text": "Submit feedback",
                "type": "FILLED",
                "icon": {"materialIcon": {"name": "send"}},
                "onClick": {"action": {
                    "function": gchat_webhook_url,
                    "parameters": [
                        {"key": "action",       "value": "report_feedback_submit"},
                        {"key": "report_id",    "value": report_id},
                        {"key": "callback_url", "value": callback_url},
                        {"key": "cb_token",     "value": cb_token},
                    ],
                }},
            }]}},
        ],
    })

    return sections


def build_report_interpret_card(
    cpmrn: str,
    encounter: int,
    batch_id: str,
    report: dict,
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
) -> list:
    """
    Build the cardsV2 list for a single report. Google Chat caps sections at 10 per card;
    callers must send one card per report rather than batching multiple reports into one card.

    `report` is a dict: {report_id, report_type, report_name, reported_at, interpretation,
     description, findings, image_urls}.
    """
    sections: list[dict] = []

    # ── Intro + Open patient ──
    sections.append({
        "header": "Patient",
        "widgets": [
            {"textParagraph": {"text": (
                "New diagnostic report resulted. "
                "Interpretation in context below; expand for the source image."
            )}},
            {"buttonList": {"buttons": [{
                "text": "Open patient",
                "icon": {"materialIcon": {"name": "open_in_new"}},
                "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
            }]}},
        ],
    })

    sections.extend(_report_sections(
        report, gchat_webhook_url, callback_url, cb_token, has_divider=False,
    ))

    report_id = report.get("report_id", batch_id)
    return [{
        "cardId": f"cds-report-interpret-{report_id}",
        "card": {
            "header": {
                "title": "🩻 New report resulted",
                "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/document_scanner/default/48px.svg",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }]


def replace_rating_section_with_status(cards_v2: list, report_id: str, status_text: str) -> list:
    """
    Return a copy of cardsV2 with the feedback section for `report_id` replaced by a
    status line, so that report's rating can't be submitted twice. Only the matching
    report's rating section is replaced (other reports in the batch keep theirs).
    """
    updated = copy.deepcopy(cards_v2)
    for card_wrapper in updated:
        sections = card_wrapper.get("card", {}).get("sections", [])
        for i, section in enumerate(sections):
            if not section.get("header", "").startswith(_RATING_PREFIX):
                continue
            if _section_report_id(section) != report_id:
                continue
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


def _section_report_id(section: dict) -> str | None:
    """Extract the report_id carried by a rating section's Submit button."""
    for w in section.get("widgets", []):
        for btn in w.get("buttonList", {}).get("buttons", []):
            for p in btn.get("onClick", {}).get("action", {}).get("parameters", []):
                if p.get("key") == "report_id":
                    return p.get("value")
    return None
