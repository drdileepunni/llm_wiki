"""
Reconciliation diff engine: map transcribed chart meds → active EMR orders and
classify each as edit / discontinue / new (or match_no_change / ambiguous).

A single Gemini structured-output call does the clinical normalization (brand↔generic,
abbreviation/frequency synonyms) that string matching can't. The active orders are
passed with their stable `orderNo` so the model references them by key.

`to_action_set` converts the result into the keyed action-set dict that
order_action_store.save_action_set / order_actions.apply_order_actions expect, applying
the **Moderate** discontinue posture chosen by the user:
  * edit / new      → kept when confidence ∈ {high, medium}
  * discontinue     → kept ONLY when confidence == high (covers both explicit chart
                      stops/holds and clear absence from an otherwise-complete chart)
  * match_no_change / ambiguous → audit-only, never an action
Discontinue checkboxes are additionally rendered un-ticked by the card builder.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ReconItem(BaseModel):
    classification: str            # edit | discontinue | new | match_no_change | ambiguous
    drug_name: str                 # normalized drug name (for labels)
    chart_med_verbatim: Optional[str] = None
    matched_order_no: Optional[str] = None
    matched_order_label: Optional[str] = None
    # proposed medication fields (used for edit/new); only the changed ones matter for edit
    quantity: Optional[float] = None
    unit: Optional[str] = None
    route: Optional[str] = None
    form: Optional[str] = None
    frequency_hours: Optional[int] = None    # dosing interval in hours (e.g. 12 for BD)
    changed_fields: list[str] = []           # for edit: which of quantity/unit/route/form/frequency change
    discontinue_reason: Optional[str] = None
    rationale: str
    confidence: str                          # high | medium | low


class ReconResult(BaseModel):
    items: list[ReconItem]
    overall_notes: str


_SYSTEM = (
    "You are a critical-care pharmacist performing medication reconciliation between a "
    "handwritten treatment chart and the active EMR medication orders. Be precise and "
    "conservative — a wrongful discontinue is dangerous."
)

_EDITABLE = {"quantity", "unit", "route", "form", "frequency"}


def _render_orders(active_orders: list[dict]) -> str:
    lines = []
    for o in active_orders:
        freq = (o.get("frequency") or {})
        hrs = freq.get("hours")
        lines.append(
            f"- orderNo={o.get('orderNo')} | {o.get('name')} "
            f"{o.get('quantity')}{o.get('unit') or ''} {o.get('route') or ''} "
            f"q{hrs}h" + (f" form={o.get('form')}" if o.get('form') else "")
        )
    return "\n".join(lines) if lines else "(no active medication orders)"


def _render_meds(extracted_meds: list[dict]) -> str:
    lines = []
    for m in extracted_meds:
        lines.append(
            f"- {m.get('name')} {m.get('dose') or ''} {m.get('route') or ''} "
            f"{m.get('frequency') or ''} [{m.get('status_on_chart')}] "
            f"(verbatim: {m.get('verbatim')!r}, conf={m.get('confidence')})"
        )
    return "\n".join(lines) if lines else "(no medications transcribed)"


def _build_prompt(extracted_meds, active_orders, summary_narrative, cpmrn, encounter) -> str:
    return (
        f"PATIENT CONTEXT:\n{(summary_narrative or '').strip() or '(none)'}\n\n"
        f"CHART MEDICATIONS (from the latest handwritten treatment chart / progress note):\n"
        f"{_render_meds(extracted_meds)}\n\n"
        f"ACTIVE EMR MEDICATION ORDERS:\n{_render_orders(active_orders)}\n\n"
        "Reconcile the two lists. Emit one item per clinically meaningful finding:\n"
        "- classification='edit': chart med matches an active order (same drug) but dose/route/"
        "frequency differ. Set matched_order_no, the corrected quantity/unit/route/form/"
        "frequency_hours, and changed_fields = the subset of "
        "[quantity,unit,route,form,frequency] that actually change.\n"
        "- classification='new': chart med has NO matching active order. Provide quantity, unit, "
        "route, form, frequency_hours.\n"
        "- classification='discontinue': an active EMR order should be stopped — either the chart "
        "explicitly marks that drug stopped/held, OR the drug is clearly absent from an otherwise-"
        "complete chart. Set matched_order_no and discontinue_reason. Use confidence='high' ONLY "
        "when you are sure (explicit stop, or the chart is clearly complete for that drug class); "
        "otherwise use 'medium'/'low' or classify as 'ambiguous'.\n"
        "- classification='match_no_change': chart and EMR agree. \n"
        "- classification='ambiguous': can't tell (illegible, partial chart, uncertain match).\n"
        "Always set drug_name, rationale, and confidence. Never discontinue from mere absence "
        "unless the chart is clearly complete for that class."
    )


def _gemini_client(model: str | None):
    root = Path(__file__).resolve().parents[3]
    for p in (str(root / "app"), str(root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from backend.config import GOOGLE_API_KEY, MODEL
    from backend.services.llm_client import GeminiLLMClient
    return GeminiLLMClient(api_key=GOOGLE_API_KEY, model=model or MODEL)


def reconcile(
    extracted_meds: list[dict],
    active_orders: list[dict],
    summary_narrative: str,
    cpmrn: str,
    encounter: int,
    model: str | None = None,
) -> dict:
    """Return {"items": [dict], "overall_notes": str, "usage": LLMUsage}."""
    from backend.services.llm_client import LLMUsage

    client = _gemini_client(model)
    prompt = _build_prompt(extracted_meds, active_orders, summary_narrative, cpmrn, encounter)
    result, usage = client.generate_json_with_usage(
        prompt=prompt, schema=ReconResult, system=_SYSTEM, max_tokens=4096
    )
    return {
        "items": result.get("items", []) or [],
        "overall_notes": result.get("overall_notes", ""),
        "usage": usage,
    }


def _frequency_obj(hours: int) -> dict:
    return {"fType": "every", "days": None, "hours": hours, "mins": None, "timeOfDay": None}


def _edit_changes(item: dict) -> dict:
    changes: dict = {}
    for f in (item.get("changed_fields") or []):
        if f == "quantity" and item.get("quantity") is not None:
            changes["quantity"] = item["quantity"]
        elif f == "unit" and item.get("unit"):
            changes["unit"] = item["unit"]
        elif f == "route" and item.get("route"):
            changes["route"] = item["route"]
        elif f == "form" and item.get("form"):
            changes["form"] = item["form"]
        elif f == "frequency" and item.get("frequency_hours"):
            changes["frequency"] = _frequency_obj(item["frequency_hours"])
    return changes


def _new_order(item: dict) -> dict:
    order = {"name": item.get("drug_name")}
    if item.get("quantity") is not None:
        order["quantity"] = item["quantity"]
    if item.get("unit"):
        order["unit"] = item["unit"]
    if item.get("route"):
        order["route"] = item["route"]
    if item.get("form"):
        order["form"] = item["form"]
    if item.get("frequency_hours"):
        order["frequency"] = _frequency_obj(item["frequency_hours"])
    return order


def _label_edit(item: dict, changes: dict) -> str:
    bits = []
    if "quantity" in changes:
        bits.append(f"dose→{changes['quantity']}{item.get('unit') or ''}")
    if "frequency" in changes:
        bits.append(f"freq→q{item['frequency_hours']}h")
    if "route" in changes:
        bits.append(f"route→{changes['route']}")
    detail = ", ".join(bits) or "update"
    return f"{item.get('drug_name')} — {detail} (per chart)"


def _label_new(item: dict) -> str:
    parts = [item.get("drug_name")]
    if item.get("quantity") is not None:
        parts.append(f"{item['quantity']}{item.get('unit') or ''}")
    if item.get("route"):
        parts.append(item["route"])
    if item.get("frequency_hours"):
        parts.append(f"q{item['frequency_hours']}h")
    return " ".join(str(p) for p in parts) + " — add (on chart, missing from EMR)"


def to_action_set(recon: dict) -> dict[str, dict]:
    """
    Convert reconcile() output into the keyed action-set dict, applying the Moderate
    discontinue posture (discontinue requires confidence=='high'; edit/new require
    confidence ∈ {high, medium}). Returns {} if nothing actionable.
    """
    actions: dict[str, dict] = {}
    new_idx = 0
    for item in recon.get("items", []):
        kind = (item.get("classification") or "").lower()
        conf = (item.get("confidence") or "").lower()

        if kind == "edit" and conf in ("high", "medium"):
            order_no = item.get("matched_order_no")
            changes = _edit_changes(item)
            if not order_no or not changes:
                continue
            actions[f"edit:{order_no}"] = {
                "kind": "edit", "order_no": order_no, "changes": changes,
                "label": _label_edit(item, changes),
            }
        elif kind == "discontinue" and conf == "high":
            order_no = item.get("matched_order_no")
            if not order_no:
                continue
            label = item.get("matched_order_label") or item.get("drug_name") or order_no
            actions[f"discontinue:{order_no}"] = {
                "kind": "discontinue", "order_no": order_no,
                "reasons": ["Other"],
                "statement": item.get("discontinue_reason") or "Not on current treatment chart",
                "label": f"{label} — discontinue (not on chart)",
            }
        elif kind == "new" and conf in ("high", "medium"):
            if not item.get("drug_name"):
                continue
            actions[f"new:{new_idx}"] = {
                "kind": "new", "order": _new_order(item), "label": _label_new(item),
            }
            new_idx += 1
        # match_no_change / ambiguous / low-confidence → audit-only
    return actions
