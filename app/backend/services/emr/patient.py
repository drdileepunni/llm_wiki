from __future__ import annotations

from typing import Any

from .db import get_db


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
