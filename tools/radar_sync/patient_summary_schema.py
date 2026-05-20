"""
Structured patient summary schema for the CDS replay timeline.

This is the single source of truth per snapshot. The `narrative` field
is stored as the legacy `running_summary` string so the rest of the stack
doesn't break.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ActiveProblem(BaseModel):
    name: str
    status: Literal["critical", "worsening", "stable", "improving", "resolved"]
    presenting_features: str
    workup: str
    management: str
    current_state: str
    plan_changing_event: str | None = None


class PatientSummary(BaseModel):
    admission_narrative: str
    problems: list[ActiveProblem]
    resolved_problems: list[str]
    narrative: str
    suggested_actions: list[str] = []


def summary_to_text(s: dict) -> str:
    """Stitch a PatientSummary dict into a plain-text paragraph (legacy running_summary)."""
    lines = [s.get("admission_narrative", "")]
    lines.append("\nActive Problems:")
    for p in s.get("problems", []):
        lines.append(
            f"  {p['name']} [{p['status']}]: {p['presenting_features']} → "
            f"{p['workup']} → {p['management']} → {p['current_state']}"
        )
        if p.get("plan_changing_event"):
            lines.append(f"    Key event: {p['plan_changing_event']}")
    resolved = s.get("resolved_problems", [])
    if resolved:
        lines.append("\nResolved / Improving:")
        for r in resolved:
            lines.append(f"  - {r}")
    return "\n".join(lines)
