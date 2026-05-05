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
    """Clear all active orders from the dummy patient."""
    db = get_db()
    db.patients.update_one(
        {"CPMRN": VIVA_DUMMY_CPMRN},
        {"$set": {"orders.active": _empty_active_orders()}},
    )


def reset_dummy_patient_chart() -> None:
    """
    Wipe all clinical chart data (vitals, IO, documents, orders, vent flags)
    from the dummy patient. Called at the start of every new viva session so the
    teacher's first scenario seeds a clean state.
    """
    db = get_db()
    db.patients.update_one(
        {"CPMRN": VIVA_DUMMY_CPMRN},
        {"$set": {
            "vitals": [],
            "io": [],
            "documents": [],
            "orders.active": _empty_active_orders(),
            "isNIV.value": False,
            "isHFNC.value": False,
            "isIntubated.value": False,
        }},
    )


# ── Viva student agent read functions ─────────────────────────────────────────

def get_io_summary(cpmrn: str, hours: int = 6) -> dict:
    """Return total urine output, intake, and fluid balance over the last N hours."""
    from datetime import timedelta
    p = _find_patient(cpmrn)
    if not p:
        return {"error": f"Patient {cpmrn} not found"}
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    entries = [e for e in p.get("io", []) if e.get("timestamp", "") >= cutoff]
    total_urine  = sum(e.get("urine_ml", 0)  for e in entries)
    total_intake = sum(e.get("intake_ml", 0) for e in entries)
    return {
        "period_hours":    hours,
        "entries_counted": len(entries),
        "total_urine_ml":  total_urine,
        "total_intake_ml": total_intake,
        "net_balance_ml":  total_intake - total_urine,
        "urine_rate_ml_per_hr": round(total_urine / hours, 1) if hours > 0 else 0,
    }


def get_vital_trend(cpmrn: str, parameter: str, hours: int = 4) -> list[dict]:
    """Return time-series values for one vital sign over the last N hours."""
    from datetime import timedelta
    _KEY_MAP = {
        "BP":          "daysBP",
        "HR":          "daysHR",
        "SpO2":        "daysSpO2",
        "RR":          "daysRR",
        "Temperature": "daysTemperature",
        "FiO2":        "daysFiO2",
        "MAP":         "daysMAP",
    }
    mongo_key = _KEY_MAP.get(parameter, f"days{parameter}")
    p = _find_patient(cpmrn)
    if not p:
        return []
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    entries = [
        {"timestamp": v.get("timestamp"), "value": v.get(mongo_key)}
        for v in p.get("vitals", [])
        if v.get("timestamp", "") >= cutoff and v.get(mongo_key) is not None
    ]
    return sorted(entries, key=lambda e: e.get("timestamp", ""))


def get_recent_notes_for_patient(cpmrn: str, category: str | None = None, limit: int = 3) -> list[dict]:
    """Return recent clinical event notes, optionally filtered by category."""
    p = _find_patient(cpmrn)
    if not p:
        return []
    docs = p.get("documents", [])
    if category:
        docs = [d for d in docs if d.get("category") == category]
    docs = sorted(docs, key=lambda d: d.get("reportedAt", ""), reverse=True)
    return [
        {
            "category":   d.get("category"),
            "name":       d.get("name"),
            "text":       d.get("text", ""),
            "reportedAt": d.get("reportedAt"),
        }
        for d in docs[:limit]
    ]


# ── Viva simulation write functions ───────────────────────────────────────────

def push_vitals(cpmrn: str, vitals: dict) -> None:
    """
    Push a new vitals snapshot onto the patient's vitals array.
    `vitals` keys use the friendly names from get_latest_vitals (BP, HR, SpO2, …).
    """
    _KEY_MAP = {
        "SpO2":         "daysSpO2",
        "HR":           "daysHR",
        "RR":           "daysRR",
        "BP":           "daysBP",
        "MAP":          "daysMAP",
        "Temperature":  "daysTemperature",
        "FiO2":         "daysFiO2",
        "OxygenFlow":   "daysOxygenFlow",
        "TherapyDevice":"daysTherapyDevice",
        "AVPU":         "daysAVPU",
        "CVP":          "daysCVP",
        "VentMode":     "daysVentMode",
        "VentPEEP":     "daysVentPEEP",
        "VentPIP":      "daysVentPip",
        "VentRRSet":    "daysVentRRset",
    }
    snapshot: dict = {"timestamp": datetime.utcnow().isoformat(), "isVerified": True, "dataBy": "viva-sim"}
    for friendly, mongo_key in _KEY_MAP.items():
        if friendly in vitals:
            snapshot[mongo_key] = vitals[friendly]
    get_db().patients.update_one(
        {"CPMRN": cpmrn},
        {"$push": {"vitals": snapshot}},
    )


def push_lab_result(cpmrn: str, name: str, attributes: dict) -> None:
    """
    Push a lab result document.
    `attributes` format: {"pH": {"name": "pH", "value": "7.34", "errorRange": {"min": 7.35, "max": 7.45}}, ...}
    """
    doc = {
        "category": "labs",
        "name": name,
        "reportedAt": datetime.utcnow().isoformat(),
        "attributes": attributes,
    }
    get_db().patients.update_one(
        {"CPMRN": cpmrn},
        {"$push": {"documents": doc}},
    )


def push_event_note(cpmrn: str, category: str, text: str) -> None:
    """Push a clinical event note (ECG findings, nursing note, etc.)."""
    doc = {
        "category": category,
        "name": "Event Note",
        "reportedAt": datetime.utcnow().isoformat(),
        "text": text,
        "addedBy": "viva-sim",
    }
    get_db().patients.update_one(
        {"CPMRN": cpmrn},
        {"$push": {"documents": doc}},
    )


def push_io_entry(cpmrn: str, urine_ml: float, intake_ml: float, period_mins: int = 60) -> None:
    """Push an intake/output entry."""
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "urine_ml": urine_ml,
        "intake_ml": intake_ml,
        "period_mins": period_mins,
        "addedBy": "viva-sim",
    }
    get_db().patients.update_one(
        {"CPMRN": cpmrn},
        {"$push": {"io": entry}},
    )


def complete_lab_orders(cpmrn: str, lab_names: list[str]) -> None:
    """
    Remove named labs from active orders (because results have been filed).
    Matches case-insensitively against the `investigation` field.
    """
    if not lab_names:
        return
    lower_names = {n.lower() for n in lab_names}
    p = get_db().patients.find_one({"CPMRN": cpmrn})
    if not p:
        return
    active = (p.get("orders") or {}).get("active") or {}
    remaining = [
        lab for lab in active.get("labs", [])
        if (lab.get("investigation") or "").lower() not in lower_names
    ]
    get_db().patients.update_one(
        {"CPMRN": cpmrn},
        {"$set": {"orders.active.labs": remaining}},
    )


def update_vent_flags(cpmrn: str, is_niv: bool | None, is_hfnc: bool | None, is_intubated: bool | None) -> None:
    """Update the respiratory support flags on the patient document."""
    updates: dict = {}
    if is_niv is not None:
        updates["isNIV.value"] = is_niv
    if is_hfnc is not None:
        updates["isHFNC.value"] = is_hfnc
    if is_intubated is not None:
        updates["isIntubated.value"] = is_intubated
    if updates:
        get_db().patients.update_one({"CPMRN": cpmrn}, {"$set": updates})
