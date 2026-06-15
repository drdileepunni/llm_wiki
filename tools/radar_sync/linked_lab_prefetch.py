"""
Linked-lab co-prefetch — when a problem triggers a critical-lab concern, automatically
fetch physiologically related context labs so the model can check cross-lab plausibility.

Classic example: lactate 20 mmol/L should always coexist with metabolic acidosis
(pH < 7.30, low bicarb). If pH is near-normal, the lactate is likely a lab error.
Without pH in context the model can't make this call.

## Extending
Add entries to LINKED_LAB_GROUPS below. Each group has:
  - name:             short identifier (for logging)
  - trigger_keywords: list of lowercase substrings; if ANY problem name contains ANY
                      keyword → group fires
  - context_labs:     list of {lab, reason} — labs to co-fetch
                      lab = key passed to _get_lab_trend (same as problem tracker tools)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Config — add more groups here as new linked-lab pairs are identified ──────

LINKED_LAB_GROUPS: list[dict] = [
    {
        "name": "lactate_acid_base",
        "trigger_keywords": ["lactic", "lactate", "acidosis"],
        "context_labs": [
            {
                "lab": "ph",
                "label": "pH",
                "reason": (
                    "pH must be low if lactate is truly critical. "
                    "Lactate > 8 with pH > 7.30 is physiologically implausible — "
                    "flag as likely lab error and recommend recheck."
                ),
            },
            {
                "lab": "bicarb",
                "label": "Bicarb",
                "reason": "Bicarb context for acid-base interpretation alongside lactate.",
            },
        ],
    },
    # Future examples (commented out — uncomment and fill in when needed):
    #
    # {
    #     "name": "hyperkalemia_ecg",
    #     "trigger_keywords": ["hyperkalemia", "potassium"],
    #     "context_labs": [
    #         {"lab": "k", "label": "Potassium", "reason": "Confirm with repeat K+"},
    #     ],
    #     # Note: ECG findings come from notes (query_patient_notes), not labs.
    # },
    #
    # {
    #     "name": "troponin_ecg",
    #     "trigger_keywords": ["acs", "stemi", "nstemi", "troponin", "myocardial infarction"],
    #     "context_labs": [
    #         {"lab": "troponin", "label": "Troponin", "reason": "Serial troponin for rise/fall pattern"},
    #     ],
    # },
]


def get_triggered_context_labs(problems: list[dict]) -> list[dict]:
    """
    Scan the patient's problem list for trigger keywords.
    Returns a deduplicated list of context-lab dicts to co-fetch.
    """
    problem_text = " ".join(p.get("name", "").lower() for p in problems)
    triggered: list[dict] = []
    seen_labs: set[str] = set()

    for group in LINKED_LAB_GROUPS:
        if any(kw in problem_text for kw in group["trigger_keywords"]):
            for ctx in group["context_labs"]:
                lab_key = ctx["lab"]
                if lab_key not in seen_labs:
                    seen_labs.add(lab_key)
                    triggered.append({**ctx, "_group": group["name"]})
            logger.debug(
                "linked_lab_prefetch: group '%s' triggered for problems: %s",
                group["name"], [p.get("name") for p in problems],
            )

    return triggered


def build_linked_lab_block(
    cpmrn: str,
    encounter: int,
    context_labs: list[dict],
    existing_prefetch: str = "",
) -> str:
    """
    Fetch each context lab and return a formatted block for injection into the
    prefetch section. Skips labs whose label already appears in existing_prefetch
    (avoids duplicating data already fetched via stored problem states).
    """
    if not context_labs:
        return ""

    from tools.radar_sync.status_classifier import _get_lab_trend

    fetched: list[str] = []
    for ctx in context_labs:
        lab_key = ctx["lab"]
        label   = ctx.get("label", lab_key)
        reason  = ctx.get("reason", "")

        # Skip if this lab's data is already present in the existing prefetch block
        if label.lower() in existing_prefetch.lower():
            logger.debug("linked_lab_prefetch: '%s' already in prefetch, skipping", label)
            continue

        try:
            trend = _get_lab_trend(cpmrn, encounter, lab_key, n=4)
            if trend and not trend.startswith("No ") and not trend.startswith("Unknown"):
                fetched.append(f"{label} (plausibility context): {trend}")
                if reason:
                    fetched.append(f"  ⚠ {reason}")
                fetched.append("")
        except Exception:
            logger.exception("linked_lab_prefetch: _get_lab_trend failed for '%s' %s", lab_key, cpmrn)

    if not fetched:
        return ""

    lines = [
        "--- Linked lab context (cross-lab plausibility) ---",
        "Check these values for physiological consistency with the critical lab above.",
    ] + fetched

    return "\n".join(lines)
