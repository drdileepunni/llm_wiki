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
from datetime import datetime, timezone, timedelta

from tools.radar_sync.gchat_notifier import (
    _CHART_BASE,
    _STATUS_ARROW,
    _STATUS_EMOJI,
    _fetch_objective_line,
)

logger = logging.getLogger(__name__)

RATING_SECTION_HEADER = "Rate this alert"
_IST = timezone(timedelta(hours=5, minutes=30))


def _fmt_ist(dt_iso: str | None) -> str:
    """Format an ISO timestamp as 'Jun 15, 9:07 PM IST'. Returns '' if unparseable."""
    if not dt_iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(dt_iso).replace("Z", "+00:00"))
        ist = dt.astimezone(_IST)
        return ist.strftime("%-d %b, %-I:%M %p IST")
    except Exception:
        return ""


_FOLLOWUP_WINDOW_VITAL_IO = {"vital": "1 hour", "io": "1 hour"}
_FAST_LAB_KEYS_CARD = {
    "hb", "hemoglobin", "haemoglobin",
    "sodium", "na",
    "potassium", "k",
    "lactate", "lactic",
}
_VITAL_CARD_WARN_H = 2


def _lab_followup_label(lab_key: str) -> str:
    return "6 hours" if lab_key.lower() in _FAST_LAB_KEYS_CARD else "12 hours"


INSULIN_SECTION_HEADER = "💉 Insulin guidance"


def _build_insulin_sections(
    insulin_order: dict,
    gchat_webhook_url: str,
    order_callback_url: str,
    cb_token: str,
) -> list[dict]:
    """
    Build one or two card sections for a computed insulin recommendation:
      1. Advisory text (dose, algorithm, inputs used, clinical caveat).
      2. Checkbox + button for SC placeable orders (omitted for IV advisory-only).
    """
    reco = insulin_order.get("reco", {})
    sourced = insulin_order.get("sourced", {})
    current_grbs = insulin_order.get("current_grbs", 0)
    action_set_id = insulin_order.get("action_set_id")
    advisory_only = insulin_order.get("advisory_only", True)

    dose = reco.get("Suggested_insulin_dose", "?")
    unit = reco.get("unit", "IU")
    route_str = "IV infusion" if reco.get("Suggested_route") == "iv" else "SC injection"
    algo = reco.get("algorithm_used", "?")
    level = reco.get("level", "?")
    next_check_h = reco.get("next_grbs_after", "?")
    action_text = reco.get("action", "")

    lines: list[str] = []

    # ── Recommendation headline ───────────────────────────────────────────────
    lines.append(
        f"Recommendation: <b>{dose} {unit} {route_str}</b>"
        f" ({algo}, Level {level}) · next check in <b>{next_check_h}h</b>"
    )

    # ── Glucose trend ─────────────────────────────────────────────────────────
    grbs_info = sourced.get("grbs", {})
    readings = grbs_info.get("readings", [])
    if readings:
        lines.append("\n<b>Glucose trend (newest first):</b>")
        for r in readings:
            ts = f" — {r['ts_ist']}" if r.get("ts_ist") else ""
            lines.append(f"  <b>{int(r['value'])} mg/dL</b>{ts}")
    elif grbs_info.get("values"):
        lines.append(f"\n<b>Glucose:</b> {int(grbs_info['values'][0])} mg/dL (no timestamp)")

    # ── Prior insulin doses ───────────────────────────────────────────────────
    insulin_info = sourced.get("insulin", {})
    orders = insulin_info.get("orders", [])
    if orders:
        lines.append("\n<b>Prior insulin orders (from EMR):</b>")
        for o in orders:
            ts = f" — placed {o['ts_ist']}" if o.get("ts_ist") else ""
            by = f" by {o['by']}" if o.get("by") else ""
            lines.append(f"  <b>{o['dose']} {o['unit']} {o['route']}</b>{ts}{by}")
    else:
        lines.append("\n⚠ <b>Prior insulin:</b> none found — engine starting at Level 2")

    # ── Diet & dual inotropes ─────────────────────────────────────────────────
    diet_info = sourced.get("diet", {})
    diet_val = diet_info.get("value", "others")
    diet_flag = " ⚠ assumed" if diet_info.get("assumed") else ""
    lines.append(f"\n<b>Diet:</b> {diet_val}{diet_flag}")

    di_info = sourced.get("dual_inotropes", {})
    di_val = "Yes" if di_info.get("value") else "No"
    lines.append(f"<b>Dual inotropes:</b> {di_val}")

    if advisory_only and reco.get("Suggested_route") == "iv":
        lines.append(
            "\n<i>⚠ IV infusion — order placement not supported in v1. "
            "Please adjust infusion rate at bedside.</i>"
        )
    elif advisory_only:
        lines.append("\n<i>⚠ Order could not be prepared — please dose manually.</i>")

    lines.append(
        "\n<i>CDS decision support only — verify at bedside before administering.</i>"
    )

    sections: list[dict] = [
        {
            "header": INSULIN_SECTION_HEADER,
            "widgets": [{"textParagraph": {"text": "\n".join(lines)}}],
        }
    ]

    # SC placeable: add checkbox + submit button (default un-ticked — high-risk opt-in)
    if not advisory_only and action_set_id and order_callback_url and gchat_webhook_url:
        label = insulin_order.get("label", f"Regular Insulin {dose} {unit} SC")
        sections.append({
            "header": "Place insulin order",
            "widgets": [
                {
                    "selectionInput": {
                        "name": "new_actions",
                        "type": "CHECK_BOX",
                        "items": [
                            {
                                "text": label,
                                "value": "insulin:0",
                                "selected": False,  # un-ticked: clinician must consciously opt in
                            }
                        ],
                    }
                },
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "Place selected insulin order",
                                "type": "FILLED",
                                "icon": {"materialIcon": {"name": "medication"}},
                                "onClick": {
                                    "action": {
                                        "function": gchat_webhook_url,
                                        "parameters": [
                                            {"key": "action", "value": "order_recon_submit"},
                                            {"key": "action_set_id", "value": action_set_id},
                                            {"key": "recon_id", "value": ""},
                                            {"key": "callback_url", "value": order_callback_url},
                                            {"key": "cb_token", "value": cb_token},
                                        ],
                                    }
                                },
                            }
                        ]
                    }
                },
            ],
        })

    return sections


def _build_problem_body_sections(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    all_problems: list[dict],
    alert_id: str,
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
    *,
    order_callback_url: str = "",
    rating_label: str = RATING_SECTION_HEADER,
    include_open_patient: bool = True,
) -> list[dict]:
    """
    Build the content sections for one alerting problem.
    Does NOT include the patient-overview (chips) section — that's shared.
    Used by both build_alert_card (single) and build_batched_alert_card (multi).
    """
    problem_name      = assessment.get("problem_name", "Unknown")
    clinical_status   = assessment.get("clinical_status", "worsening")
    alert_reason      = assessment.get("alert_reason", "")
    note_vs_objective = assessment.get("note_vs_objective", "")
    snapshot_ts       = _fmt_ist(assessment.get("_snapshot_at"))
    nc                = assessment.get("next_check") or {}
    vital_age_hours   = assessment.get("_vital_age_hours")

    sections: list[dict] = []

    # ── Observed ──────────────────────────────────────────────────────────────
    current_state = ""
    for p in all_problems:
        if p.get("name") == problem_name:
            current_state = p.get("current_state", "")
            break
    if not current_state:
        current_state = assessment.get("addressed_evidence", "")
    if current_state:
        observed_text = current_state
        if snapshot_ts:
            observed_text += f" <i>({snapshot_ts})</i>"
        sections.append({
            "header": "Observed",
            "widgets": [{"textParagraph": {"text": observed_text}}],
        })

    # ── Objective data ────────────────────────────────────────────────────────
    nc_data_what = nc.get("key") or nc.get("what", "")
    nc_type = nc.get("type", "")
    if nc_data_what and nc_type:
        obj_line = _fetch_objective_line(cpmrn, encounter, nc_data_what, nc_type)
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

    # ── Insulin dosing guidance ───────────────────────────────────────────────
    insulin_order = assessment.get("_insulin_order")
    if insulin_order:
        sections.extend(_build_insulin_sections(
            insulin_order, gchat_webhook_url, order_callback_url, cb_token
        ))

    # ── Vital data staleness warning ──────────────────────────────────────────
    if vital_age_hours is not None and vital_age_hours > _VITAL_CARD_WARN_H:
        age_label = f"{vital_age_hours:.0f}h" if vital_age_hours >= 1 else f"{int(vital_age_hours * 60)}min"
        sections.append({
            "header": "⚠ Vital data may be outdated",
            "widgets": [{"decoratedText": {
                "startIcon": {"materialIcon": {"name": "history"}},
                "text": (
                    f"The most recent <b>verified</b> vital in our system is <b>{age_label} old</b>. "
                    f"More recent bedside readings may exist but are not yet verified in Radar. "
                    f"Please confirm current vitals at bedside before acting on this alert."
                ),
                "wrapText": True,
            }}],
        })

    # ── Lab upload lag warning ─────────────────────────────────────────────────
    _lab_lag = assessment.get("_lab_upload_lag")
    if _lab_lag:
        sections.append({
            "header": "⚠ Lab results uploaded late",
            "widgets": [{"decoratedText": {
                "startIcon": {"materialIcon": {"name": "schedule"}},
                "text": (
                    f"These lab results were reported at <b>{_lab_lag['reported_ist']} IST</b> "
                    f"but only uploaded to the chart at <b>{_lab_lag['created_ist']} IST</b> "
                    f"(<b>{_lab_lag['lag_hours']:.0f}h delay</b>). "
                    f"The alert could not fire until the results appeared in the chart."
                ),
                "wrapText": True,
            }}],
        })

    # ── Note vs. objective discordance ────────────────────────────────────────
    if note_vs_objective:
        sections.append({
            "header": "⚠ Note vs. objective",
            "widgets": [{"decoratedText": {
                "startIcon": {"materialIcon": {"name": "compare_arrows"}},
                "text": note_vs_objective,
                "wrapText": True,
            }}],
        })

    # ── Model reasoning (collapsed) ───────────────────────────────────────────
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

    # ── Context-gate reasoning (collapsed) ───────────────────────────────────
    ctx_gate = assessment.get("context_gate") or {}
    if ctx_gate and ctx_gate.get("verdict"):
        verdict = ctx_gate.get("verdict", "")
        verdict_label = {
            "permissive_active":     "🟢 Permissive window ACTIVE — alert suppressed",
            "permissive_breached":   "🟠 Permissive window BREACHED — alert firing",
            "permissive_ended":      "🔴 Permissive window ENDED — normal alert rules apply",
            "no_permissive_context": "⚪ No permissive context — normal alert rules apply",
        }.get(verdict, verdict)

        gate_lines: list[str] = [f"<b>{verdict_label}</b>"]
        scenario = ctx_gate.get("scenario", "")
        if scenario and scenario != "none":
            gate_lines.append(f"Scenario: <i>{scenario}</i>")
        band = ctx_gate.get("band_description", "")
        if band:
            gate_lines.append(f"Band: {band}")
        valid_until = ctx_gate.get("valid_until")
        if valid_until:
            valid_str = _fmt_ist(
                valid_until.isoformat() if hasattr(valid_until, "isoformat") else str(valid_until)
            )
            gate_lines.append(f"Valid until: {valid_str}")
        rationale = ctx_gate.get("rationale", "")
        if rationale:
            gate_lines.append(f"\n{rationale}")
        if verdict == "permissive_active":
            plan = ctx_gate.get("plan_if_permissive", "")
            if plan:
                gate_lines.append(f"\n<b>While permissive:</b> {plan}")
        plan_end = ctx_gate.get("plan_when_ended", "")
        if plan_end:
            gate_lines.append(f"<b>When window closes:</b> {plan_end}")

        sections.append({
            "header": "🔓 Permissive-window reasoning",
            "collapsible": True,
            "uncollapsibleWidgetsCount": 0,
            "widgets": [{"textParagraph": {"text": "\n".join(gate_lines)}}],
        })

    # ── Model follow-up ───────────────────────────────────────────────────────
    if nc.get("type"):
        nc_type_fu = nc["type"]
        nc_what = nc.get("label") or nc.get("vital_key") or nc.get("lab_name") or nc.get("key") or nc_type_fu
        if nc_type_fu == "lab":
            nc_window = _lab_followup_label(nc.get("lab_name") or nc.get("key") or "")
        else:
            nc_window = _FOLLOWUP_WINDOW_VITAL_IO.get(nc_type_fu, "next run")
        sections.append({
            "header": "Model follow-up",
            "widgets": [{"decoratedText": {
                "startIcon": {"materialIcon": {"name": "schedule"}},
                "text": f"The model will recheck <b>{nc_what}</b> in {nc_window}.",
                "wrapText": True,
            }}],
        })

    # ── Rating form ───────────────────────────────────────────────────────────
    rating_buttons = [
        {
            "text": "Submit feedback",
            "type": "FILLED",
            "icon": {"materialIcon": {"name": "send"}},
            "onClick": {"action": {
                "function": gchat_webhook_url,
                "parameters": [
                    {"key": "action",       "value": "alert_rating_submit"},
                    {"key": "action_id",    "value": alert_id},
                    {"key": "callback_url", "value": callback_url},
                    {"key": "cb_token",     "value": cb_token},
                ],
            }},
        },
    ]
    if include_open_patient:
        rating_buttons.append({
            "text": "Open patient",
            "icon": {"materialIcon": {"name": "open_in_new"}},
            "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
        })

    sections.append({
        "header": rating_label,
        "widgets": [
            {"selectionInput": {
                "name": "rating",
                "label": "How appropriate is this alert?",
                "type": "RADIO_BUTTON",
                "items": [
                    {"text": "⭐ 1 — Not appropriate at all",  "value": "1"},
                    {"text": "⭐⭐ 2 — Mostly inappropriate",   "value": "2"},
                    {"text": "⭐⭐⭐ 3 — Borderline",            "value": "3"},
                    {"text": "⭐⭐⭐⭐ 4 — Mostly appropriate",  "value": "4"},
                    {"text": "⭐⭐⭐⭐⭐ 5 — Fully appropriate", "value": "5"},
                ],
            }},
            {"textInput": {
                "name": "feedback_text",
                "label": "Optional note about your rating",
                "type": "MULTIPLE_LINE",
            }},
            {"buttonList": {"buttons": rating_buttons}},
        ],
    })

    return sections


def build_alert_card(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
    alert_id: str,
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
    order_callback_url: str = "",
) -> list:
    """
    Build the cardsV2 payload for a single problem alert.
    Returns the cardsV2 list (one card).
    """
    clinical_status = assessment.get("clinical_status", "worsening")
    emoji           = _STATUS_EMOJI.get(clinical_status, "⚠️")
    alert_title     = assessment.get("alert_title") or assessment.get("problem_name", "Unknown")

    all_problems: list[dict] = structured_summary.get("problems", [])
    sections: list[dict] = []

    # Patient overview chips
    if all_problems:
        chips = [
            {"label": f"{p.get('name', '?')} {_STATUS_ARROW.get(p.get('status', 'stable'), '→')}"}
            for p in all_problems
        ]
        sections.append({
            "header": "Patient problems",
            "widgets": [{"chipList": {"chips": chips}}],
        })

    # All problem-specific content (includes "Open patient" in rating)
    sections.extend(_build_problem_body_sections(
        cpmrn, encounter, assessment, all_problems, alert_id,
        gchat_webhook_url, callback_url, cb_token,
        order_callback_url=order_callback_url,
        include_open_patient=True,
    ))

    return [{
        "cardId": f"cds-alert-{alert_id}",
        "card": {
            "header": {
                "title": f"{emoji} {alert_title}",
                "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/clinical_notes/default/48px.svg",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }]


def build_batched_alert_card(
    cpmrn: str,
    encounter: int,
    alerts: list[tuple[dict, str]],
    structured_summary: dict,
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
    order_callback_url: str = "",
) -> list:
    """
    Build ONE combined cardsV2 card for multiple alerting problems on the same patient.
    Shared header + patient problems shown once; each problem gets its own labelled
    section group with an individual rating form.
    """
    all_problems: list[dict] = structured_summary.get("problems", [])

    # Determine worst status for the combined card header
    statuses = [a.get("clinical_status", "worsening") for a, _ in alerts]
    if "critical" in statuses:
        header_emoji = "🔴"
    else:
        header_emoji = "🟠"
    n = len(alerts)
    header_title = f"{header_emoji} {n} Alert{'s' if n > 1 else ''}"

    sections: list[dict] = []

    # ── Shared patient overview with "Open patient" button ────────────────────
    shared_widgets: list[dict] = []
    if all_problems:
        chips = [
            {"label": f"{p.get('name', '?')} {_STATUS_ARROW.get(p.get('status', 'stable'), '→')}"}
            for p in all_problems
        ]
        shared_widgets.append({"chipList": {"chips": chips}})
    shared_widgets.append({"buttonList": {"buttons": [{
        "text": "Open patient",
        "icon": {"materialIcon": {"name": "open_in_new"}},
        "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
    }]}})
    sections.append({"header": "Patient problems", "widgets": shared_widgets})

    # ── Per-problem sections ──────────────────────────────────────────────────
    for i, (assessment, alert_id) in enumerate(alerts):
        problem_name    = assessment.get("problem_name", "Unknown")
        clinical_status = assessment.get("clinical_status", "worsening")
        emoji           = _STATUS_EMOJI.get(clinical_status, "⚠️")
        alert_title     = assessment.get("alert_title") or problem_name

        # Section header acts as the problem title within the combined card.
        # hasDivider=True draws a line above (except for the first problem).
        problem_header_section: dict = {
            "header": f"{emoji} {alert_title}",
            "widgets": [],          # content comes in subsequent sections
        }
        if i > 0:
            problem_header_section["hasDivider"] = True
        sections.append(problem_header_section)

        # Problem body (no "Open patient" — already in shared section)
        body_sections = _build_problem_body_sections(
            cpmrn, encounter, assessment, all_problems, alert_id,
            gchat_webhook_url, callback_url, cb_token,
            order_callback_url=order_callback_url,
            rating_label=f"Rate: {problem_name}",
            include_open_patient=False,
        )
        sections.extend(body_sections)

    first_alert_id = alerts[0][1]
    return [{
        "cardId": f"cds-batch-{first_alert_id}",
        "card": {
            "header": {
                "title": header_title,
                "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/clinical_notes/default/48px.svg",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }]


def replace_rating_section_with_status(cards_v2: list, status_text: str) -> list:
    """
    Return a copy of cardsV2 with every rating form replaced by a status line,
    so the card updates in place and can't be voted on twice.
    Handles both single cards and batched cards (multiple rating sections).
    """
    updated = copy.deepcopy(cards_v2)
    for card_wrapper in updated:
        sections = card_wrapper.get("card", {}).get("sections", [])
        for i, section in enumerate(sections):
            hdr = section.get("header", "")
            if hdr == RATING_SECTION_HEADER or hdr.startswith("Rate:"):
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
