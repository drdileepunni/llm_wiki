"""Category-specific prompt blocks for the problem tracker — injected only when
a patient actually has a problem of that category.

WHY THIS EXISTS
The problem-tracker system prompt grew into a ~390-line monolith. Every rule was
sent on every run, for every patient, regardless of relevance. A patient with no
neurological problem still received the full GCS-delta rule; a patient with no
respiratory problem still received the FiO2 / SF-ratio rules. That volume dilutes
the model's attention and was implicated in missed findings (e.g. a flagged
Troponin elevation that the tracker never assessed because it raced through the
visible problem list).

This module holds the rule blocks that are ONLY relevant to a specific clinical
domain. They are removed from the always-on core prompt and injected conditionally,
following the exact same load → filter → format pattern already used by
lab_alert_rules.py and symptom_alert_rules.py.

SAFETY ASYMMETRY (read before editing the keyword lists)
Over-injecting an irrelevant block costs a little attention dilution — harmless.
UNDER-injecting a block that was needed means the model never sees a safety rule —
a potential missed alert. The two errors are NOT symmetric. Therefore:
  • Keyword lists bias toward inclusion. When unsure, match.
  • The dangerous-if-missed blocks (GCS especially) carry generous keyword lists.
  • The always-on CORE in problem_tracker.py keeps every universal rule. The blocks
    here are domain REFINEMENTS, never the baseline. A problem that matches no
    category still gets the full core prompt.
"""
from __future__ import annotations

from tools.radar_sync.clinical_rules import RESPIRATORY_SF_RULE, OLIGURIA_CHARTING_RULE

# ── Category-specific rule blocks ─────────────────────────────────────────────
# Each block is the verbatim text previously inlined in problem_tracker._SYSTEM.
# Moving it here must not change wording — only WHEN it is shown.

_NEURO_BLOCK = (
    "GCS DELTA — for any problem related to GCS, consciousness, or neurological status: do NOT "
    "alert unless GCS has dropped ≥ 2 points within the last 6 hours. Call "
    "get_vital_trend('GCS', n=6) and compare the most recent reading against the reading from 6 "
    "hours ago. If the delta is < 2 (stable or improving), should_alert=False regardless of the "
    "absolute GCS value, of whether a plan note exists, and of the treatment-inadequate override. "
    "A chronically low GCS is NOT a reason to alert. Only a negative delta ≥ 2 within the 6-hour "
    "window justifies an alert."
)

_RESPIRATORY_BLOCK = (
    f"- {RESPIRATORY_SF_RULE}\n"
    "- FiO2 RULE — FiO2 is a clinician-controlled ventilator setting, not a patient parameter. Do "
    "NOT set clinical_status=\"worsening\"/\"critical\" and do NOT alert based on FiO2 changes "
    "alone. FiO2 increases are intentional clinical interventions — alerting on them is circular. "
    "To assess oxygenation use SpO2 or SF ratio (both via get_vital_trend('SpO2')). If SpO2 is "
    "maintained ≥92% despite high FiO2, oxygenation is being managed — do not alert."
)

_RENAL_BLOCK = (
    "- For AKI, oliguria, anuria, or fluid-balance problems: ALWAYS call get_io before concluding "
    "output is absent — the structured summary may not reflect the latest I/O data. get_io shows "
    "DAILY TOTALS first, then hourly detail. If the hourly window shows 0 ml but the daily total "
    "is non-zero, I/O is charted as a daily batch entry — do NOT interpret as anuria; use the "
    "daily total to assess fluid balance.\n"
    f"- {OLIGURIA_CHARTING_RULE} Do NOT escalate AKI, alert for anuria, or conclude oliguria on "
    "the basis of 0 ml charting alone."
)

_SYMPTOM_BLOCK = (
    "SUBJECTIVE SYMPTOM RULE — do NOT set clinical_status=\"worsening\"/\"critical\" and do NOT "
    "alert for problems whose primary evidence is a patient-reported symptom (pain, nausea, "
    "dizziness, fatigue, reported breathlessness, reported chest tightness) without corroborating "
    "objective evidence. \"Patient reports severe pain\", \"patient complains of nausea\", or "
    "\"patient feels breathless\" alone is NOT sufficient to alert. Objective evidence means at "
    "least ONE of:\n"
    "  • A validated numeric score meeting a documented threshold (e.g. NRS/VAS pain score ≥ 7/10 "
    "explicitly recorded)\n"
    "  • A physiological correlate that itself breaches the VITAL SIGN ALERT FLOORS (new "
    "tachycardia, hypotension, hypoxia, etc.) AND is plausibly caused by the symptom\n"
    "  • An imaging or lab finding showing objective worsening of the underlying cause\n"
    "If none are present, classify the symptom-based problem as stable and should_alert=False. The "
    "adequacy of the current treatment plan is the clinician's call — do NOT alert purely because "
    "you judge the prescribed analgesic or antiemetic insufficient."
)

# CAUSAL / SECONDARY is injected on a deterministic trigger (any problem carries a
# `cause`), not on keywords — see has_secondary_problem() below.
_CAUSAL_BLOCK = (
    "CAUSAL / SECONDARY PROBLEMS\n"
    "When a problem is marked secondary (has a cause), apply this reasoning:\n"
    "- If the primary driver (the cause) is being_addressed=True and its clinical_status is NOT "
    "\"critical\" or \"worsening\", the secondary problem should NOT generate an independent alert "
    "solely because its own parameters remain abnormal. Rationale: secondary organ dysfunction "
    "(AKI, coagulopathy, thrombocytopaenia) lags behind the primary problem by 24–72h. Treating "
    "the cause IS the treatment.\n"
    "- Exception — DO alert for the secondary problem if ANY of the following are present "
    "regardless of the primary driver's status:\n"
    "    • A rapid step-change worsening (e.g. creatinine rises >50% from last snapshot)\n"
    "    • A value in a life-threatening range (K+ ≥ 6.0, pH < 7.20, bicarb < 12)\n"
    "    • A clinical sign requiring independent intervention (RRT indication, dialysis)\n"
    "- If the primary driver is NOT being_addressed, assess the secondary problem normally."
)

# ── Category registry ─────────────────────────────────────────────────────────
# category → (keyword triggers matched against the joined problem names, block text).
# Keyword lists bias toward inclusion (see SAFETY ASYMMETRY above).
_CATEGORIES: dict[str, tuple[list[str], str]] = {
    "neuro": (
        ["gcs", "consciousness", "conscious", "encephalopath", "coma", "comatose",
         "seizure", "sensorium", "neuro", "altered mental", "obtunded", "drowsy"],
        _NEURO_BLOCK,
    ),
    "respiratory": (
        ["hypox", "spo2", "sp02", "desaturat", "tachypn", "respiratory", "ards",
         "oxygen", "ventilat", "fio2", "hypercapn", "pneumon", "weaning"],
        _RESPIRATORY_BLOCK,
    ),
    "renal": (
        ["aki", "kidney", "oliguri", "anuri", "creatinine", "renal", "fluid overload",
         "fluid balance", "rrt", "dialysis"],
        _RENAL_BLOCK,
    ),
    "symptom": (
        ["pain", "nausea", "dizzin", "breathless", "fatigue", "chest tightness",
         "discomfort", "vomit"],
        _SYMPTOM_BLOCK,
    ),
}


def _problem_text(problems: list[dict]) -> str:
    """Join all signals we match keywords against: problem names + cause annotations."""
    parts: list[str] = []
    for p in problems:
        parts.append(str(p.get("name", "")))
        if p.get("cause"):
            parts.append(str(p.get("cause")))
    return " ".join(parts).lower()


def has_secondary_problem(problems: list[dict]) -> bool:
    """True if any problem carries a `cause` — the trigger for the causal/secondary block."""
    return any(p.get("cause") for p in problems)


def filter_blocks_for_patient(problems: list[dict], prefetch_block: str = "") -> list[str]:
    """
    Return the category blocks relevant to this patient.

    A category matches if any of its keywords appears in the joined problem
    names/causes OR in the pre-fetched vital/lab trends (prefetch_block) — the same
    dual signal lab_alert_rules uses. The causal/secondary block is added whenever
    any problem has a `cause`, independent of keywords.
    """
    haystack = _problem_text(problems)
    if prefetch_block:
        haystack += " " + prefetch_block.lower()

    blocks: list[str] = []
    for _category, (keywords, text) in _CATEGORIES.items():
        if any(kw in haystack for kw in keywords):
            blocks.append(text)

    if has_secondary_problem(problems):
        blocks.append(_CAUSAL_BLOCK)

    return blocks


def matched_categories(problems: list[dict], prefetch_block: str = "") -> list[str]:
    """Names of matched categories — for logging/observability (mirrors lab_alert_rules logs)."""
    haystack = _problem_text(problems)
    if prefetch_block:
        haystack += " " + prefetch_block.lower()
    cats = [c for c, (kws, _t) in _CATEGORIES.items() if any(kw in haystack for kw in kws)]
    if has_secondary_problem(problems):
        cats.append("causal")
    return cats


def format_prompt_block(blocks: list[str]) -> str:
    """
    Join matched category blocks into a single injection section for the system prompt.
    Returns empty string if nothing matched (no injection).
    """
    if not blocks:
        return ""
    header = (
        "════════════════════════════════════════════════════════════════════════\n"
        "CATEGORY-SPECIFIC RULES  (apply to the problem types present in this patient)\n"
        "════════════════════════════════════════════════════════════════════════"
    )
    return header + "\n\n" + "\n\n".join(blocks)
