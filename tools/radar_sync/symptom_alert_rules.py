"""
Per-symptom alert rules — configurable objective criteria required before the problem
tracker may alert on a symptom-based problem.

Rules live at app_settings/symptom_alert_rules.json in GCS and can be edited without
a redeploy. Use seed_symptom_alert_rules.py to write the initial set.

Filtering matches against the patient's problem list (structured_summary["problems"]),
not the prefetch block, so only rules for problems this patient actually has are injected.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_rules(db: Any) -> list[dict]:
    """Return enabled symptom rules from app_settings, or [] if missing/disabled."""
    try:
        doc = db["app_settings"].find_one({"_id": "symptom_alert_rules"})
        if doc and doc.get("enabled", True) and doc.get("rules"):
            return list(doc["rules"])
    except Exception:
        logger.exception("symptom_alert_rules: failed to load rules from app_settings")
    return []


def filter_rules_for_patient(rules: list[dict], problems: list[dict]) -> list[dict]:
    """
    Return only rules whose symptom type matches at least one problem in this patient's
    problem list. Matching is alias-based, case-insensitive.

    problems: structured_summary["problems"] — each dict has at least a "name" key.
    """
    if not rules or not problems:
        return []

    problem_names_lower = {p.get("name", "").lower() for p in problems}
    matched = []
    for rule in rules:
        aliases = rule.get("aliases") or [rule.get("problem", "")]
        if any(alias.lower() in name for alias in aliases for name in problem_names_lower):
            matched.append(rule)
    return matched


def format_prompt_block(rules: list[dict]) -> str:
    """
    Format matched symptom rules into a SYMPTOM ALERT CRITERIA block for injection
    into the system prompt. Returns empty string if rules list is empty.
    """
    if not rules:
        return ""

    lines = [
        "- SYMPTOM ALERT CRITERIA — the following symptom-based problems are present for"
        " this patient. In addition to the general SUBJECTIVE SYMPTOM RULE, these specific"
        " objective thresholds apply before you may alert:",
    ]

    for rule in rules:
        problem   = rule.get("problem", "?").capitalize()
        criteria  = rule.get("objective_criteria") or []
        min_score = rule.get("min_score")
        score_name = rule.get("score_name", "pain score")

        lines.append(f"    • {problem}: alert ONLY if at least one of these is present:")
        if min_score is not None:
            lines.append(f"        – {score_name} ≥ {min_score}/10 explicitly documented")
        for c in criteria:
            lines.append(f"        – {c}")

    lines.append(
        "  If none of the above criteria are met for a symptom-based problem, set"
        " clinical_status=stable and should_alert=False regardless of how the patient"
        " describes their symptoms."
    )

    return "\n".join(lines)
