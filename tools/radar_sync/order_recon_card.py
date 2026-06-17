"""
Build the Google Chat cardsV2 "medication reconciliation" card.

The card lists proposed order changes grouped into three checkbox sections —
**edit**, **discontinue**, **create** — and a single Submit button. The clinician
ticks whichever changes they approve and submits once; the webhook
(`main.py:/order-action`) then executes exactly the ticked actions.

The card cannot carry full order payloads, so the proposed action set is stored
in GCS (`proposed_order_actions/{action_set_id}`) and each checkbox value is the
action's key (e.g. "edit:lYqfwiYN", "new:0"). On submit the webhook loads the set
and filters to the selected keys.

Mirrors the conventions in alert_cards.py:
  * onClick.action.function = full HTTPS gchat webhook URL; intent in parameters.
  * a replace_*_with_results() helper updates the card in place after submit.
"""
from __future__ import annotations

import copy

_CHART_BASE = "https://cloudphysicianworld.com/patient"

_EDIT_HEADER = "✏️ Edit these orders"
_DISCONTINUE_HEADER = "🛑 Discontinue these orders"
_NEW_HEADER = "➕ Create these orders"
_ACTION_HEADERS = (_EDIT_HEADER, _DISCONTINUE_HEADER, _NEW_HEADER)


def _checkbox_section(header: str, name: str, items: list[dict]) -> dict | None:
    """
    One CHECK_BOX selectionInput section. `items` is a list of
    {"key": <action key>, "label": <human text>}. All boxes default-ticked so
    "approve everything + submit" is a single click. Returns None if no items.
    """
    if not items:
        return None
    return {
        "header": header,
        "widgets": [{
            "selectionInput": {
                "name": name,
                "type": "CHECK_BOX",
                "items": [
                    {"text": it["label"], "value": it["key"], "selected": True}
                    for it in items
                ],
            }
        }],
    }


def build_order_recon_card(
    cpmrn: str,
    encounter: int,
    action_set_id: str,
    edits: list[dict],
    discontinues: list[dict],
    news: list[dict],
    gchat_webhook_url: str,
    callback_url: str,
    cb_token: str,
) -> list:
    """
    Build the cardsV2 list (one card). `edits`/`discontinues`/`news` are lists of
    {"key": ..., "label": ...} describing each proposed change.
    """
    n = len(edits) + len(discontinues) + len(news)
    sections: list[dict] = []

    # Intro
    sections.append({
        "widgets": [{"textParagraph": {"text": (
            f"Found <b>{n}</b> medication discrepanc{'y' if n == 1 else 'ies'} between the "
            f"paper chart and active EMR orders. Select the changes to apply and submit once."
        )}}]
    })

    for section in (
        _checkbox_section(_EDIT_HEADER, "edit_actions", edits),
        _checkbox_section(_DISCONTINUE_HEADER, "discontinue_actions", discontinues),
        _checkbox_section(_NEW_HEADER, "new_actions", news),
    ):
        if section:
            sections.append(section)

    # Submit + Open patient
    sections.append({
        "widgets": [{"buttonList": {"buttons": [
            {
                "text": "Apply selected changes",
                "type": "FILLED",
                "icon": {"materialIcon": {"name": "send"}},
                "onClick": {"action": {
                    "function": gchat_webhook_url,
                    "parameters": [
                        {"key": "action",        "value": "order_recon_submit"},
                        {"key": "action_set_id", "value": action_set_id},
                        {"key": "callback_url",  "value": callback_url},
                        {"key": "cb_token",      "value": cb_token},
                    ],
                }},
            },
            {
                "text": "Open patient",
                "icon": {"materialIcon": {"name": "open_in_new"}},
                "onClick": {"openLink": {"url": f"{_CHART_BASE}/{cpmrn}/{encounter}"}},
            },
        ]}}]
    })

    return [{
        "cardId": f"cds-order-recon-{action_set_id}",
        "card": {
            "header": {
                "title": "🩺 Medication reconciliation",
                "subtitle": f"{cpmrn}  ·  Encounter {encounter}",
                "imageUrl": "https://fonts.gstatic.com/s/i/short-term/release/googlesymbols/medication/default/48px.svg",
                "imageType": "CIRCLE",
            },
            "sections": sections,
        },
    }]


def replace_actions_with_results(cards_v2: list, results: list[dict]) -> list:
    """
    Return a copy of cardsV2 with the three checkbox sections + the submit button
    replaced by a per-action results block, so the card updates in place and can't
    be submitted twice.

    `results` is the list returned by order_actions.apply_order_actions:
    {"kind", "name", "ok", "status", "error"}.
    """
    _KIND_VERB = {"edit": "Edited", "discontinue": "Discontinued", "new": "Created"}

    lines: list[str] = []
    ok_count = 0
    for r in results:
        verb = _KIND_VERB.get(r.get("kind"), r.get("kind", "?"))
        if r.get("ok"):
            ok_count += 1
            lines.append(f"✅ {verb}: <b>{r.get('name', '')}</b>")
        else:
            err = r.get("error") or f"HTTP {r.get('status')}"
            lines.append(f"⚠️ {verb} failed: <b>{r.get('name', '')}</b> — <i>{err}</i>")

    summary = f"<b>{ok_count}/{len(results)} change{'s' if len(results) != 1 else ''} applied</b>"
    results_widgets = [
        {"divider": {}},
        {"decoratedText": {
            "startIcon": {"materialIcon": {"name": "task_alt"}},
            "text": summary,
            "wrapText": True,
        }},
        {"textParagraph": {"text": "<br>".join(lines)}},
    ]

    updated = copy.deepcopy(cards_v2)
    for card_wrapper in updated:
        sections = card_wrapper.get("card", {}).get("sections", [])
        # Drop the checkbox sections and the trailing submit button section,
        # then append the results block once.
        kept = []
        for section in sections:
            hdr = section.get("header", "")
            if hdr in _ACTION_HEADERS:
                continue
            if _is_submit_section(section):
                continue
            kept.append(section)
        kept.append({"header": "Result", "widgets": results_widgets})
        card_wrapper["card"]["sections"] = kept

    return updated


def _is_submit_section(section: dict) -> bool:
    """True if a section contains the order_recon_submit button."""
    for w in section.get("widgets", []):
        for btn in w.get("buttonList", {}).get("buttons", []):
            params = btn.get("onClick", {}).get("action", {}).get("parameters", [])
            if any(p.get("key") == "action" and p.get("value") == "order_recon_submit" for p in params):
                return True
    return False
