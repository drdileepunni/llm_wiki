"""
Insulin dosing advice for glucose-related CDS alert cards.

When a problem_tracker assessment is for an uncontrolled glucose problem, this
module:
  1. Gathers engine inputs from the EMR (glucose trend, active insulin orders,
     diet orders, vasopressors).
  2. Runs the deterministic InsulinRecommendationEngine to compute a dose.
  3. For SC recommendations: persists an order action set to GCS and attaches
     a placeable order (Regular Insulin, checkbox) to the alert card.
  4. For IV recommendations: attaches advisory-only text (no checkbox/order).

The result is stored on the assessment dict under "_insulin_order" so that
alert_cards._build_problem_body_sections can render it without any further
computation.

Enrichment is always wrapped in a try/except so a failure here never blocks
the alert from being sent.
"""
from __future__ import annotations

import logging
import os
import re
import uuid

logger = logging.getLogger(__name__)

_GLUCOSE_KEYWORDS = (
    "hyperglycemia", "hypoglycemia", "glucose", "blood sugar",
    "rbs", "cbg", "grbs", "diabetic", "blood glucose",
)

_INSULIN_NAME_PATTERNS = ("insulin",)

_VASOPRESSOR_NAMES = (
    "norepinephrine", "noradrenaline", "epinephrine", "adrenaline",
    "dopamine", "dobutamine", "vasopressin",
)

_NPO_PATTERNS = ("npo", "nil by mouth", "nil per os", "fasting", "nbm")


def is_glucose_problem(assessment: dict) -> bool:
    name = (assessment.get("problem_name") or "").lower()
    return any(kw in name for kw in _GLUCOSE_KEYWORDS)



def _read_meds(cpmrn: str, encounter: int) -> list[dict]:
    """Return active medication orders; empty list on any error."""
    try:
        from tools.radar_sync.order_actions import _read_active_medications
        return _read_active_medications(cpmrn, encounter) or []
    except Exception:
        logger.debug("insulin_advice: could not read active meds for %s", cpmrn)
        return []


def gather_inputs(cpmrn: str, encounter: int) -> tuple[dict, dict]:
    """
    Build the engine input dict and a sourced-info dict (for card display).
    Returns (engine_input, sourced) where sourced records each value + assumed flag.
    """
    engine_input: dict = {}
    sourced: dict = {}

    # ── Glucose trend ──────────────────────────────────────────────────────────
    grbs_values: list[float] = []
    try:
        from tools.radar_sync.status_classifier import _get_lab_trend as _glt
        trend_str = _glt(cpmrn, encounter, "glucose", n=5) or ""
        # Parse numeric values from trend string: "glucose = 280 mg/dL" etc.
        grbs_values = [float(m) for m in re.findall(r"=\s*([\d.]+)", trend_str)]
    except Exception:
        pass

    if not grbs_values:
        # Nothing to compute from — caller should bail.
        sourced["grbs"] = {"values": [], "assumed": False, "found": False}
        return {}, sourced

    engine_input["GRBS"] = grbs_values
    sourced["grbs"] = {
        "values": grbs_values,
        "found": True,
        "assumed": False,
        "label": f"Glucose {', '.join(str(int(v)) for v in grbs_values)} mg/dL (newest first)",
    }

    # ── Active medications ─────────────────────────────────────────────────────
    meds = _read_meds(cpmrn, encounter)

    # Prior insulin doses + route detection
    insulin_meds = [
        m for m in meds
        if any(p in (m.get("name") or "").lower() for p in _INSULIN_NAME_PATTERNS)
    ]
    prior_doses: list[float] = []
    route = "sc"
    route_found = False
    for m in insulin_meds[:4]:
        try:
            prior_doses.append(float(m.get("quantity", 0) or 0))
        except (TypeError, ValueError):
            prior_doses.append(0.0)
        r = (m.get("route") or "").lower()
        if "iv" in r or "infusion" in r or "drip" in r:
            route = "iv"
            route_found = True
        elif "sc" in r or "subcut" in r:
            if not route_found:
                route = "sc"
                route_found = True

    if prior_doses:
        engine_input["Insulin"] = prior_doses
        sourced["insulin"] = {
            "found": True, "assumed": False,
            "label": f"Prior doses: {prior_doses} IU (from active insulin orders)",
        }
    else:
        sourced["insulin"] = {
            "found": False, "assumed": True,
            "label": "Prior insulin doses: not found — engine starts at Level 2",
        }

    engine_input["route"] = route
    sourced["route"] = {
        "found": route_found, "assumed": not route_found,
        "value": route,
        "label": f"Route: {route.upper()}" + (" (assumed SC — no active insulin found)" if not route_found else ""),
    }

    # Diet order (NPO vs. others)
    diet_found = False
    diet = "others"
    diet_meds_and_orders = [m for m in meds]
    for m in diet_meds_and_orders:
        name = (m.get("name") or "").lower()
        instr = (m.get("instructions") or "").lower()
        if any(p in name or p in instr for p in _NPO_PATTERNS):
            diet = "NPO"
            diet_found = True
            break

    engine_input["diet_order"] = diet
    sourced["diet"] = {
        "found": diet_found, "assumed": not diet_found,
        "value": diet,
        "label": f"Diet: {diet}" + (" (assumed — no NPO order found)" if not diet_found else ""),
    }

    # Dual inotropes
    vasopress_active = [
        m for m in meds
        if any(p in (m.get("name") or "").lower() for p in _VASOPRESSOR_NAMES)
    ]
    dual_inotropes = len(vasopress_active) >= 2
    engine_input["Dual inotropes"] = dual_inotropes
    sourced["dual_inotropes"] = {
        "found": True, "assumed": False,
        "value": dual_inotropes,
        "label": (
            f"Dual inotropes: Yes ({len(vasopress_active)} vasopressors active)"
            if dual_inotropes
            else f"Dual inotropes: No ({len(vasopress_active)} vasopressor found)"
        ),
    }

    return engine_input, sourced


def compute(engine_input: dict) -> dict:
    from tools.radar_sync.insulin import InsulinRecommendationEngine
    eng = InsulinRecommendationEngine()
    return eng.recommend_insulin_dose(engine_input)


def _build_order_payload(reco: dict, current_grbs: float) -> dict:
    """Build a create_order-compatible payload for a SC correction dose."""
    dose = reco["Suggested_insulin_dose"]
    return {
        "name": "Regular Insulin",
        "quantity": dose,
        "unit": "IU",
        "route": "SC",
        "form": "Injection",
        "sos": "correction dose for hyperglycemia",
        "sosReason": f"Blood glucose {int(current_grbs)} mg/dL — CDS insulin protocol",
        "startNow": True,
        "instructions": (
            f"Correction dose: {dose} IU SC stat — per CDS insulin protocol "
            f"(Basal Bolus level {reco.get('level', '?')}). "
            f"Next glucose check in {reco.get('next_grbs_after', '?')} hours."
        ),
        "createdBy": "CDS insulin adviser",
    }


def _human_label(reco: dict, current_grbs: float) -> str:
    dose = reco["Suggested_insulin_dose"]
    unit = reco.get("unit", "IU")
    route_str = "SC" if reco["Suggested_route"] == "subcutaneous" else "IV"
    return (
        f"Regular Insulin {dose} {unit} {route_str} — correction for glucose "
        f"{int(current_grbs)} mg/dL (Level {reco.get('level', '?')})"
    )


def attach_insulin_order(cpmrn: str, encounter: int, assessment: dict) -> None:
    """
    Compute an insulin recommendation for a glucose-related alert and attach
    it to `assessment["_insulin_order"]`. No-ops (silently) if the problem is
    not glucose-related, if no glucose value is available, or on any error.

    Side-effect: if SC, persists an action set to GCS so the /order-action
    webhook can execute the order when the clinician ticks the checkbox.
    """
    try:
        if not is_glucose_problem(assessment):
            return

        engine_input, sourced = gather_inputs(cpmrn, encounter)
        if not engine_input:
            logger.info("insulin_advice: no glucose data for %s — skipping", cpmrn)
            return

        reco = compute(engine_input)
        if "error" in reco:
            logger.warning("insulin_advice: engine error for %s: %s", cpmrn, reco["error"])
            return

        current_grbs = (engine_input.get("GRBS") or [0])[0]
        label = _human_label(reco, current_grbs)
        is_sc = reco["Suggested_route"] == "subcutaneous"

        action_set_id = None
        if is_sc:
            order_payload = _build_order_payload(reco, current_grbs)
            action_set_id = uuid.uuid4().hex
            action_set = {
                "insulin:0": {
                    "kind": "new",
                    "order": order_payload,
                    "label": label,
                }
            }
            try:
                from tools.radar_sync.order_action_store import save_action_set
                save_action_set(action_set_id, cpmrn, encounter, action_set)
            except Exception:
                logger.exception(
                    "insulin_advice: failed to save action set for %s — order not placeable",
                    cpmrn,
                )
                action_set_id = None  # degrade to advisory

        assessment["_insulin_order"] = {
            "reco": reco,
            "sourced": sourced,
            "current_grbs": current_grbs,
            "label": label,
            "action_set_id": action_set_id,
            "advisory_only": not is_sc or action_set_id is None,
        }

        logger.info(
            "insulin_advice: attached %s recommendation for %s enc=%d — %s",
            "SC placeable" if not assessment["_insulin_order"]["advisory_only"] else "advisory",
            cpmrn, encounter, label,
        )

    except Exception:
        logger.exception(
            "insulin_advice: unexpected error for %s enc=%d — skipping enrichment",
            cpmrn, encounter,
        )
