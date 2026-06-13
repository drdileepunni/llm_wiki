"""
Per-lab alert thresholds — loaded from GCS app_settings, injected into the problem
tracker's system prompt for the current patient only if the lab is present.

Rules live at app_settings/lab_alert_rules.json in GCS and can be edited without
a redeploy. Use seed_lab_alert_rules.py to write the initial set.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_rules(db: Any) -> list[dict]:
    """Return the enabled rules list from app_settings, or [] if missing/disabled."""
    try:
        doc = db["app_settings"].find_one({"_id": "lab_alert_rules"})
        if doc and doc.get("enabled", True) and doc.get("rules"):
            return list(doc["rules"])
    except Exception:
        logger.exception("lab_alert_rules: failed to load rules from app_settings")
    return []


def filter_rules_for_patient(rules: list[dict], prefetch_block: str) -> list[dict]:
    """
    Return only the rules whose lab is actually present in this patient's prefetch data.
    Matching is alias-based, case-insensitive, against the pre-built prefetch_block string
    (which contains all pre-fetched vital and lab trends for the patient).
    """
    if not rules or not prefetch_block:
        return []
    block_lower = prefetch_block.lower()
    matched = []
    for rule in rules:
        aliases = rule.get("aliases") or [rule.get("lab", "")]
        if any(alias.lower() in block_lower for alias in aliases):
            matched.append(rule)
    return matched


def format_prompt_block(rules: list[dict]) -> str:
    """
    Format matched rules into a LAB ALERT FLOORS block for injection into the system
    prompt. Returns empty string if rules list is empty (no injection needed).
    """
    if not rules:
        return ""

    lines = [
        "- LAB ALERT FLOORS — for lab-based problems, do NOT set clinical_status=\"worsening\""
        " or \"critical\" and do NOT alert unless the rule below is satisfied for the CURRENT"
        " (most recent) value:",
    ]

    for rule in rules:
        lab       = rule.get("lab", "?")
        unit      = rule.get("unit", "")
        logic     = rule.get("logic", "floor_OR_delta")
        floor     = rule.get("absolute_floor")
        ceiling   = rule.get("absolute_ceiling")
        delta_pct = rule.get("delta_pct")
        delta_abs = rule.get("delta_abs")
        direction = rule.get("delta_direction", "drop")
        notes     = rule.get("notes", "")

        parts: list[str] = []

        if floor is not None:
            parts.append(f"< {floor:,} {unit}".strip())
        if ceiling is not None:
            parts.append(f"> {ceiling:,} {unit}".strip())

        if delta_pct is not None:
            arrow = "drop" if direction == "drop" else ("rise" if direction == "rise" else "change")
            parts.append(f"{arrow} ≥ {delta_pct}% from prior value")
        elif delta_abs is not None:
            arrow = "drop" if direction == "drop" else ("rise" if direction == "rise" else "change")
            parts.append(f"{arrow} > {delta_abs} {unit}".strip())

        connector = " OR " if logic == "floor_OR_delta" else " AND "
        rule_str = connector.join(parts)
        label = lab.capitalize()
        lines.append(f"    • {label}:  alert if {rule_str}")

    lines += [
        "  A downward trend alone is NOT sufficient — the absolute threshold must also be"
        " breached (or both conditions met for AND rules). Apply the same TREND DIRECTION"
        " RULE as for vitals: if the value is recovering toward normal, do NOT alert.",
    ]

    return "\n".join(lines)
