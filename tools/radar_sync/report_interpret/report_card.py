"""
Build the Google Chat cardsV2 "new diagnostic report(s) resulted" card.

Each newly resulted report gets its own section group: a header (report type +
relative timing, doubling as the Findings chip list when findings exist), a
collapsed accordion with the verbatim description/transcription followed by
the model's interpretation (folded into the same text block — no separate
"Interpretation" section), and a collapsed accordion with the report image(s).
No per-report rating — these cards are for quick visual triage, not scored
model-output review.

Google Chat caps a card at ~10 sections, so `build_report_interpret_cards()`
packs as many reports as fit into one card and starts a new card (a separate
Chat message) once the budget is exhausted — e.g. 5 small reports might become
2-3 messages instead of 5.

Mirrors the conventions in alert_cards.py / order_recon_card.py:
  * collapsible accordions use {"collapsible": True, "uncollapsibleWidgetsCount": 0}.

`replace_rating_section_with_status()` / `_section_report_id()` are kept even
though new cards no longer carry a rating section — they're still needed to
process Submit clicks on any older report card already delivered to Chat
before this change (see /report-feedback in main.py).
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta

_CHART_BASE = "https://cloudphysicianworld.com/patient"
_IST = timezone(timedelta(hours=5, minutes=30))

_RATING_PREFIX = "Rate report:"
_MAX_SECTIONS_PER_CARD = 10

_TYPE_DISPLAY = {
    "xray":       "X-ray",
    "echo":       "Echo",
    "ecg":        "ECG",
    "ekg":        "ECG",
    "ct":         "CT scan",
    "mri":        "MRI",
    "ultrasound": "Ultrasound",
    "usg":        "Ultrasound",
    "doppler":    "Doppler",
    "angiogram":  "Angiogram",
}


def _display_type(report_type: str, report_name: str) -> str:
    """Return a human-friendly report type label, e.g. 'X-ray', 'Echo', 'CT scan'."""
    key = (report_type or "").lower().strip()
    if key in _TYPE_DISPLAY:
        return _TYPE_DISPLAY[key]
    # Fallback: derive from the raw report name
    name_lc = (report_name or "").lower()
    for k, v in _TYPE_DISPLAY.items():
        if k in name_lc:
            return v
    return "Diagnostic report"


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


def _report_sections(report: dict) -> list[dict]:
    """
    Build the section group for one report: header, Findings chips (if any),
    description/transcription, and image. No interpretation, no rating.
    """
    rtype      = (report.get("report_type") or "other").lower()
    name       = report.get("report_name") or rtype.upper()
    ts         = _fmt_ist(report.get("reported_at"))
    image_urls = report.get("image_urls") or []
    is_xray    = rtype == "xray"

    sections: list[dict] = []

    # ── Report header, merged with Findings chips when present ─────────────────
    # Always carry at least one widget — Google Chat silently drops a section
    # (header included) when its widgets list is empty. Folding Findings into
    # the header section (rather than a separate "Findings" section) saves one
    # section per report, letting more reports fit under the 10-section cap.
    title = f"🩻 {name}"
    if ts:
        title += f"  ·  {ts}"
    findings = report.get("findings") or []
    if findings and not is_xray:
        chips = []
        for f in findings:
            sev = (f.get("severity") or "routine").lower()
            emoji = _SEVERITY_EMOJI.get(sev, "•")
            chips.append({"label": f"{emoji} {f.get('label', '')}"})
        header_widgets = [{"chipList": {"chips": chips}}]
    else:
        header_widgets = [{"divider": {}}]
    sections.append({"header": title, "widgets": header_widgets})

    if is_xray:
        # X-rays: image only — no findings or description to show
        if image_urls:
            sections.append({
                "header": "🖼️ X-ray image",
                "widgets": [
                    {"image": {"imageUrl": url, "onClick": {"openLink": {"url": url}}}}
                    for url in image_urls
                ],
            })
        return sections

    description = report.get("description") or "(no description produced)"
    interpretation = (report.get("interpretation") or "").strip()

    # Interpretation folds into the same accordion as the raw transcription
    # instead of getting its own section — no separate "Interpretation"
    # section, but the model's read is still one tap away.
    body_text = description
    if interpretation:
        body_text += f"\n\n<b>Interpretation:</b>\n{interpretation}"

    sections.append({
        "header": "📄 Description / transcription",
        "collapsible": True,
        "uncollapsibleWidgetsCount": 0,
        "widgets": [{"textParagraph": {"text": body_text}}],
    })

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

    return sections


def build_report_interpret_cards(
    cpmrn: str,
    encounter: int,
    batch_id: str,
    reports: list[dict],
    patient_narrative: str = "",
) -> list[list]:
    """
    Build one or more cardsV2 payloads covering all `reports`, packing as many
    reports as fit into each card under Google Chat's ~10-section limit.

    Returns a list of cardsV2 payloads — send each as a separate Chat message.
    A batch of 5 small reports typically becomes 2-3 messages instead of 5.

    `report` dicts: {report_id, report_type, report_name, reported_at,
     description, findings, image_urls}. `patient_narrative` is the running
    clinical summary shown once per card in the Patient section.
    """
    if not reports:
        return []

    per_report_sections = [(_report_sections(r), r) for r in reports]

    shared_section_count = 1  # the "Patient" section, once per card
    budget = max(_MAX_SECTIONS_PER_CARD - shared_section_count, 1)

    groups: list[list[tuple[list[dict], dict]]] = []
    current: list[tuple[list[dict], dict]] = []
    current_count = 0
    for sections, r in per_report_sections:
        n = len(sections)
        if current and current_count + n > budget:
            groups.append(current)
            current, current_count = [], 0
        current.append((sections, r))
        current_count += n
    if current:
        groups.append(current)

    blurb = patient_narrative.strip() or "New diagnostic report(s) resulted."
    total = len(groups)
    cards: list[list] = []
    for gi, group in enumerate(groups):
        group_reports = [r for _, r in group]
        if len(group_reports) == 1:
            rtype = (group_reports[0].get("report_type") or "").lower()
            rname = group_reports[0].get("report_name") or ""
            title = f"🩻 New {_display_type(rtype, rname)} resulted"
        else:
            title = f"🩻 {len(group_reports)} new reports resulted"
        if total > 1:
            title += f"  ({gi + 1}/{total})"

        sections: list[dict] = [{
            "header": "Patient",
            "widgets": [
                {"textParagraph": {"text": blurb}},
                {"buttonList": {"buttons": [{
                    "text": "Open patient",
                    "icon": {"materialIcon": {"name": "open_in_new"}},
                    "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
                }]}},
            ],
        }]
        for report_sections, _ in group:
            sections.extend(report_sections)

        first_report_id = group_reports[0].get("report_id", batch_id)
        cards.append([{
            "cardId": f"cds-report-interpret-{batch_id}-{gi}-{first_report_id}",
            "card": {
                "header": {
                    "title": title,
                    "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                    "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/document_scanner/default/48px.svg",
                    "imageType": "CIRCLE",
                },
                "sections": sections,
            },
        }])

    return cards


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
