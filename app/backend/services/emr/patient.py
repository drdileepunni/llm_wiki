from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .db import get_db

VIVA_DUMMY_CPMRN = "VIVA_DUMMY_001"


def _find_patient(cpmrn: str) -> dict | None:
    db = get_db()
    return db.patients.find_one({"CPMRN": cpmrn})


def get_patient_demographics(cpmrn: str) -> dict[str, Any]:
    """
    Return core demographics needed for order calculations:
    name, age (years), gender, weight, height, BMI, IBW,
    patientType, allergies, bloodGroup, admitDate, hospital/unit.
    """
    p = _find_patient(cpmrn)
    if not p:
        return {"error": f"Patient with CPMRN '{cpmrn}' not found"}

    age_obj = p.get("age") or {}
    age_years = age_obj.get("year")

    return {
        "cpmrn": p.get("CPMRN"),
        "mrn": p.get("MRN"),
        "name": " ".join(filter(None, [p.get("name"), p.get("lastName")])),
        "age_years": age_years,
        "age_months": age_obj.get("month"),
        "dob": p.get("dob"),
        "gender": p.get("gender"),
        "patientType": p.get("patientType"),
        "weightKg": p.get("weightKg"),
        "heightCm": p.get("heightCm"),
        "BMI": p.get("BMI"),
        "IBW": p.get("IBW"),
        "bloodGroup": p.get("bloodGroup"),
        "allergies": p.get("allergies", []),
        "chronic": p.get("chronic", []),
        "hospitalName": p.get("hospitalName"),
        "unitName": p.get("unitName"),
        "bedNo": p.get("bedNo"),
        "ICUAdmitDate": p.get("ICUAdmitDate"),
        "isCurrentlyAdmitted": p.get("isCurrentlyAdmitted"),
        "code": p.get("code"),
    }


def get_latest_vitals(cpmrn: str) -> dict[str, Any]:
    """
    Return the most recent vitals entry: SpO2, HR, RR, BP, MAP,
    FiO2, OxygenFlow, TherapyDevice, Temperature, and ventilator
    settings if applicable.
    """
    p = _find_patient(cpmrn)
    if not p:
        return {"error": f"Patient with CPMRN '{cpmrn}' not found"}

    vitals_list: list[dict] = p.get("vitals", [])
    if not vitals_list:
        return {"cpmrn": cpmrn, "vitals": None, "message": "No vitals recorded"}

    # Sort by timestamp descending, take the most recent
    def _ts(v: dict) -> str:
        return v.get("timestamp") or ""

    latest = max(vitals_list, key=_ts)

    return {
        "cpmrn": cpmrn,
        "timestamp": latest.get("timestamp"),
        "SpO2": latest.get("daysSpO2"),
        "HR": latest.get("daysHR"),
        "RR": latest.get("daysRR"),
        "BP": latest.get("daysBP"),
        "MAP": latest.get("daysMAP"),
        "Temperature": latest.get("daysTemperature"),
        "TemperatureUnit": latest.get("daysTemperatureUnit"),
        "FiO2": latest.get("daysFiO2"),
        "OxygenFlow": latest.get("daysOxygenFlow"),
        "TherapyDevice": latest.get("daysTherapyDevice"),
        "AVPU": latest.get("daysAVPU"),
        "CVP": latest.get("daysCVP"),
        "PatientPosition": latest.get("daysPatPosition"),
        # Ventilator parameters
        "VentMode": latest.get("daysVentMode"),
        "VentPEEP": latest.get("daysVentPEEP"),
        "VentPIP": latest.get("daysVentPip"),
        "VentRRSet": latest.get("daysVentRRset"),
        "isIntubated": (p.get("isIntubated") or {}).get("value"),
        "isNIV": (p.get("isNIV") or {}).get("value"),
        "isHFNC": (p.get("isHFNC") or {}).get("value"),
        "isVerified": latest.get("isVerified"),
        "dataBy": latest.get("dataBy"),
    }


def get_latest_labs(cpmrn: str, tests: list[str] | None = None) -> dict[str, Any]:
    """
    Return the most recent value for each lab test from patient documents.

    Args:
        cpmrn: Patient CPMRN identifier
        tests: Optional list of test names to filter (e.g. ["Creatinine", "Hb"]).
               If omitted, returns the latest value for every lab found.

    Returns:
        Dict keyed by test name → {value, unit, reportedAt}
    """
    p = _find_patient(cpmrn)
    if not p:
        return {"error": f"Patient with CPMRN '{cpmrn}' not found"}

    documents: list[dict] = p.get("documents", [])
    lab_docs = [d for d in documents if d.get("category") == "labs"]

    if not lab_docs:
        return {"cpmrn": cpmrn, "labs": {}, "message": "No lab documents found"}

    # Normalise filter to lowercase set for case-insensitive matching
    filter_set = {t.lower() for t in tests} if tests else None

    # Collect latest result per test name
    latest: dict[str, dict] = {}
    for doc in lab_docs:
        reported_at = doc.get("reportedAt", "")
        attributes: dict = doc.get("attributes") or {}
        doc_name = doc.get("name", "")

        # Each attribute is a sub-test within a document (e.g. CBC has Hb, WBC…)
        for attr_key, attr_val in attributes.items():
            if not isinstance(attr_val, dict):
                continue

            test_name = attr_val.get("name") or attr_key or doc_name
            if filter_set and test_name.lower() not in filter_set:
                continue

            existing = latest.get(test_name)
            if existing is None or reported_at > existing["reportedAt"]:
                latest[test_name] = {
                    "value": attr_val.get("value"),
                    "unit": None,  # unit not stored per-attribute in this schema
                    "reportedAt": reported_at,
                    "documentName": doc_name,
                    "normalRange": {
                        "min": (attr_val.get("errorRange") or {}).get("min"),
                        "max": (attr_val.get("errorRange") or {}).get("max"),
                    },
                }

    # If tests were requested but some weren't found as attributes, try matching by document name
    if filter_set:
        for doc in lab_docs:
            doc_name = doc.get("name", "")
            if doc_name.lower() in filter_set and doc_name not in latest:
                latest[doc_name] = {
                    "value": doc.get("label"),
                    "unit": None,
                    "reportedAt": doc.get("reportedAt", ""),
                    "documentName": doc_name,
                    "normalRange": {"min": None, "max": None},
                }

    return {"cpmrn": cpmrn, "labs": latest}


def get_active_orders(cpmrn: str) -> dict[str, Any]:
    """
    Return all currently active orders (medications, labs, procedures,
    diets, vents, bloods) to help the agent avoid duplicates.
    """
    p = _find_patient(cpmrn)
    if not p:
        return {"error": f"Patient with CPMRN '{cpmrn}' not found"}

    active = (p.get("orders") or {}).get("active") or {}

    def _slim_med(m: dict) -> dict:
        return {
            "name": m.get("name"),
            "quantity": m.get("quantity"),
            "unit": m.get("unit"),
            "route": m.get("route"),
            "form": m.get("form"),
            "frequency": m.get("frequency"),
            "orderNo": m.get("orderNo"),
            "startTime": m.get("startTime"),
        }

    def _slim_lab(l: dict) -> dict:
        return {
            "investigation": l.get("investigation"),
            "discipline": l.get("discipline"),
            "frequency": l.get("frequency"),
            "orderNo": l.get("orderNo"),
        }

    def _slim_procedure(pr: dict) -> dict:
        return {
            "pType": pr.get("pType"),
            "name": pr.get("name"),
            "site": pr.get("site"),
            "orderNo": pr.get("orderNo"),
        }

    return {
        "cpmrn": cpmrn,
        "active_orders": {
            "medications": [_slim_med(m) for m in active.get("medications", [])],
            "labs": [_slim_lab(l) for l in active.get("labs", [])],
            "procedures": [_slim_procedure(pr) for pr in active.get("procedures", [])],
            "diets": active.get("diets", []),
            "vents": active.get("vents", []),
            "bloods": active.get("bloods", []),
        },
    }


# ── Viva dummy patient ─────────────────────────────────────────────────────────

def _empty_active_orders() -> dict:
    return {"medications": [], "labs": [], "procedures": [], "diets": [], "vents": [], "bloods": []}


def upsert_dummy_patient(details: dict) -> dict:
    """Create or replace the viva dummy patient in MongoDB."""
    db = get_db()
    docs = []
    if details.get("creatinine") or details.get("egfr"):
        attrs: dict = {}
        if details.get("creatinine"):
            attrs["Creatinine"] = {
                "name": "Creatinine",
                "value": details["creatinine"],
                "errorRange": {"min": 60, "max": 120},
            }
        if details.get("egfr"):
            attrs["eGFR"] = {
                "name": "eGFR",
                "value": details["egfr"],
                "errorRange": {"min": 60, "max": 120},
            }
        docs = [{"category": "labs", "name": "Renal Function", "reportedAt": datetime.utcnow().isoformat(), "attributes": attrs}]

    patient_doc = {
        "CPMRN": VIVA_DUMMY_CPMRN,
        "name": details.get("name", "Viva Patient"),
        "age": {"year": details.get("age_years", 50), "month": 0},
        "gender": details.get("gender", "male"),
        "patientType": "adult",
        "weightKg": details.get("weight_kg"),
        "heightCm": details.get("height_cm"),
        "allergies": details.get("allergies", []),
        "chronic": details.get("diagnoses", []),
        "hospitalName": "Viva Training",
        "unitName": "ICU",
        "isCurrentlyAdmitted": True,
        "orders": {"active": _empty_active_orders()},
        "documents": docs,
        "isIntubated": {"value": False},
        "isNIV": {"value": False},
        "isHFNC": {"value": False},
    }
    db.patients.replace_one({"CPMRN": VIVA_DUMMY_CPMRN}, patient_doc, upsert=True)
    return get_dummy_patient()


def get_dummy_patient() -> dict | None:
    """Return the dummy patient in a frontend-friendly shape, or None."""
    db = get_db()
    p = db.patients.find_one({"CPMRN": VIVA_DUMMY_CPMRN})
    if not p:
        return None
    age_obj = p.get("age") or {}
    labs: dict = {}
    for doc in p.get("documents", []):
        if doc.get("category") == "labs":
            for attr_key, attr_val in (doc.get("attributes") or {}).items():
                if isinstance(attr_val, dict):
                    labs[attr_val.get("name", attr_key)] = attr_val.get("value")
    return {
        "cpmrn": VIVA_DUMMY_CPMRN,
        "name": p.get("name"),
        "age_years": age_obj.get("year"),
        "gender": p.get("gender"),
        "weight_kg": p.get("weightKg"),
        "height_cm": p.get("heightCm"),
        "allergies": p.get("allergies", []),
        "diagnoses": p.get("chronic", []),
        "creatinine": labs.get("Creatinine"),
        "egfr": labs.get("eGFR"),
    }


def place_viva_order(order: dict) -> dict:
    """
    Place, edit, or stop a single order on the dummy patient.

    order fields:
      action          — "new" | "edit" | "stop"
      order_type      — "med" | "lab" | "procedure" | "comm" | "vents" | "diet" | "blood"
      orderable_name  — matched name from catalog
      order_details   — {quantity, unit, route, form, frequency, instructions}
      existing_order_no — required for action="edit"|"stop"
      recommendation  — original text (used as fallback name)
    """
    db = get_db()
    p = db.patients.find_one({"CPMRN": VIVA_DUMMY_CPMRN})
    if not p:
        return {"error": "Dummy patient not found — create it first"}

    active = dict((p.get("orders") or {}).get("active") or _empty_active_orders())
    for k in ("medications", "labs", "procedures", "diets", "vents", "bloods"):
        active.setdefault(k, [])

    action = order.get("action", "new")
    order_type = (order.get("order_type") or "med").lower()
    details = order.get("order_details") or {}
    existing_no = order.get("existing_order_no")
    order_no = f"ORD{uuid.uuid4().hex[:6].upper()}"

    if order_type in ("med", "medication"):
        med_doc = {
            "name": order.get("orderable_name", order.get("recommendation", "")),
            "quantity": details.get("quantity", ""),
            "unit": details.get("unit", ""),
            "route": details.get("route", ""),
            "form": details.get("form", ""),
            "frequency": details.get("frequency", ""),
            "instructions": details.get("instructions", ""),
            "orderNo": order_no,
            "startTime": datetime.utcnow().isoformat(),
        }
        if action == "new":
            active["medications"].append(med_doc)
        elif action == "edit" and existing_no:
            active["medications"] = [
                med_doc if m.get("orderNo") == existing_no else m
                for m in active["medications"]
            ]
            order_no = existing_no  # keep same number for edits
        elif action == "stop" and existing_no:
            active["medications"] = [m for m in active["medications"] if m.get("orderNo") != existing_no]

    elif order_type == "lab":
        lab_doc = {
            "investigation": order.get("orderable_name", ""),
            "discipline": details.get("discipline", ""),
            "frequency": details.get("frequency", "once"),
            "orderNo": order_no,
        }
        if action == "new":
            active["labs"].append(lab_doc)
        elif action == "stop" and existing_no:
            active["labs"] = [l for l in active["labs"] if l.get("orderNo") != existing_no]

    elif order_type in ("procedure", "comm", "monitoring"):
        proc_doc = {
            "pType": order_type,
            "name": order.get("orderable_name") or order.get("recommendation", ""),
            "site": details.get("site", ""),
            "instructions": details.get("instructions", ""),
            "orderNo": order_no,
        }
        if action == "new":
            active["procedures"].append(proc_doc)
        elif action == "stop" and existing_no:
            active["procedures"] = [pr for pr in active["procedures"] if pr.get("orderNo") != existing_no]

    elif order_type == "vents":
        vent_doc = {
            "name": order.get("orderable_name", "Ventilator"),
            "instructions": details.get("instructions", ""),
            "orderNo": order_no,
            "startTime": datetime.utcnow().isoformat(),
        }
        if action == "new":
            active["vents"].append(vent_doc)
        elif action == "stop" and existing_no:
            active["vents"] = [v for v in active["vents"] if v.get("orderNo") != existing_no]

    db.patients.update_one({"CPMRN": VIVA_DUMMY_CPMRN}, {"$set": {"orders.active": active}})
    return {"order_no": order_no, "action": action, "placed": True}


def reset_dummy_patient_orders() -> None:
    """Clear all active orders from the dummy patient (called at session start)."""
    db = get_db()
    db.patients.update_one(
        {"CPMRN": VIVA_DUMMY_CPMRN},
        {"$set": {"orders.active": _empty_active_orders()}},
    )
