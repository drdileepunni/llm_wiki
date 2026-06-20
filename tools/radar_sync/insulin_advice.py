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

    # ── Active medications (fetch first — needed regardless of glucose) ───────
    meds = _read_meds(cpmrn, encounter)

    # ── Glucose trend ──────────────────────────────────────────────────────────
    grbs_values: list[float] = []
    grbs_readings: list[dict] = []  # [{"value": float, "ts_ist": str}, ...]
    try:
        from tools.radar_sync.status_classifier import _get_lab_trend as _glt
        trend_str = _glt(cpmrn, encounter, "glucose", n=5) or ""
        # Trend line format: "  [Jun 19, 3:30 PM IST] Glucose = 280 mg/dL"
        for line in trend_str.splitlines():
            ts_m = re.search(r"\[([^\]]+)\]", line)
            val_m = re.search(r"=\s*([\d.]+)", line)
            if val_m:
                val = float(val_m.group(1))
                grbs_values.append(val)
                grbs_readings.append({
                    "value": val,
                    "ts_ist": ts_m.group(1) if ts_m else "",
                })
    except Exception:
        pass

    if not grbs_values:
        # No glucose in snapshot — caller should not compute, but return full sourced
        # so the demo script can still display what was found in EMR.
        sourced["grbs"] = {"values": [], "readings": [], "assumed": False, "found": False,
                           "label": "Glucose: not found in snapshot"}
        # Still populate the rest so a test caller can override GRBS and use real EMR inputs.
    else:
        engine_input["GRBS"] = grbs_values
        sourced["grbs"] = {
            "values": grbs_values,
            "readings": grbs_readings,
            "found": True,
            "assumed": False,
            "label": f"Glucose {', '.join(str(int(v)) for v in grbs_values)} mg/dL (newest first)",
        }

    # Prior insulin doses + route detection
    insulin_meds = [
        m for m in meds
        if any(p in (m.get("name") or "").lower() for p in _INSULIN_NAME_PATTERNS)
    ]
    prior_doses: list[float] = []
    prior_orders: list[dict] = []  # [{"dose": float, "route": str, "ts_ist": str}, ...]
    route = "sc"
    route_found = False
    for m in insulin_meds[:4]:
        try:
            dose_val = float(m.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            dose_val = 0.0
        prior_doses.append(dose_val)
        r = (m.get("route") or "").lower()
        if "iv" in r or "infusion" in r or "drip" in r:
            route = "iv"
            route_found = True
        elif "sc" in r or "subcut" in r:
            if not route_found:
                route = "sc"
                route_found = True
        # Format createdAt as IST
        ts_ist = ""
        created = m.get("createdAt") or m.get("updatedAt") or ""
        if created:
            try:
                from datetime import datetime, timezone, timedelta
                _IST = timezone(timedelta(hours=5, minutes=30))
                dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts_ist = dt.astimezone(_IST).strftime("%-d %b, %-I:%M %p IST")
            except Exception:
                ts_ist = str(created)
        prior_orders.append({
            "dose": dose_val,
            "unit": m.get("unit", "IU"),
            "route": (m.get("route") or "").upper(),
            "ts_ist": ts_ist,
            "by": m.get("createdBy", ""),
        })

    if prior_doses:
        engine_input["Insulin"] = prior_doses
        sourced["insulin"] = {
            "found": True, "assumed": False,
            "orders": prior_orders,
            "label": f"Prior doses: {prior_doses} IU (from active insulin orders)",
        }
    else:
        sourced["insulin"] = {
            "found": False, "assumed": True,
            "orders": [],
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

    # If no glucose was found, caller must supply GRBS before calling compute().
    # Return empty engine_input as the signal, but always return full sourced.
    if not grbs_values:
        return {}, sourced

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
        "frequency": {"fType": "once", "days": None, "hours": None, "mins": None, "timeOfDay": None},
        "startNow": True,
        "skipSchedule": [],
        "combination": [],
        "sos": False,
        "sosReason": None,
        "instructions": (
            f"CDS insulin protocol (Basal Bolus level {reco.get('level', '?')}): "
            f"{dose} IU SC for glucose {int(current_grbs)} mg/dL. "
            f"Next glucose check in {reco.get('next_grbs_after', '?')} hours."
        ),
        "type": "medications",
        "category": "pending",
        "state": "red",
        "createdBy": "Aina bot",
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
