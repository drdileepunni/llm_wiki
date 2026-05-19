"""Pull patient chart from Radar EMR and upsert to local MongoDB."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

from .radar_auth import get_id_token

logger = logging.getLogger(__name__)

RADAR_READ_URL = os.environ.get("RADAR_READ_URL", "")

# Fields requested from Radar for each patient
CHART_RETURN_FIELDS: dict[str, int] = {
    "CPMRN": 1, "name": 1, "lastName": 1, "encounters": 1, "visitId": 1,
    "age": 1, "sex": 1, "weightKg": 1, "heightCm": 1,
    "hospitalName": 1, "unitName": 1, "bedNo": 1, "ICUAdmitDate": 1,
    "allergies": 1, "chronic": 1, "home_medications": 1,
    "patientType": 1, "bloodGroup": 1,
    "isIntubated": 1, "isNIV": 1, "isHFNC": 1, "isTrach": 1,
    "severity": 1, "code": 1, "operativeStatus": 1,
    "pressors": 1, "coagulopathy": 1, "renalFailure": 1,
    "ards": 1, "cardiacFailure": 1,
    "vitals": 1,
    "documents": 1,
    "orders.active": 1, "orders.pending": 1, "orders.completed": 1,
    "notes.finalNotes": 1,
    "io": 1,
}


def _radar_post(payload: dict) -> Any:
    url = RADAR_READ_URL
    if not url:
        raise RuntimeError("RADAR_READ_URL not set")
    token = get_id_token(url)
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_admitted_patients(workspace: str = "1A") -> list[dict]:
    """Return lightweight list of currently admitted patients in workspace."""
    data = _radar_post({"function": "get_workspace_patients", "workspace_name": workspace})
    if isinstance(data, dict) and "patients" in data:
        data = data["patients"]
    if isinstance(data, dict):
        data = [data]
    patients = []
    for p in (data or []):
        cpmrn = p.get("display_id") or (p.get("id") or "").split("/")[0]
        enc_str = (p.get("id") or "").split("/")[-1] if "/" in (p.get("id") or "") else "1"
        try:
            encounter = int(enc_str)
        except ValueError:
            encounter = 1
        patients.append({
            "CPMRN": cpmrn,
            "encounter": encounter,
            "display_name": p.get("display_name", ""),
            "isIntubated": p.get("isIntubated", {}).get("value", False) if isinstance(p.get("isIntubated"), dict) else bool(p.get("isIntubated")),
        })
    return [p for p in patients if p["CPMRN"]]


def pull_chart(cpmrn: str, encounter: int = 1) -> dict:
    """Fetch full chart for a single patient from Radar."""
    result = _radar_post({
        "function": "get_patient_json",
        "filter_using": {"CPMRN": cpmrn, "encounters": encounter},
        "return_fields": CHART_RETURN_FIELDS,
    })
    if isinstance(result, list):
        if not result:
            raise ValueError(f"No patient found for CPMRN={cpmrn} encounter={encounter}")
        return result[0]
    return result


def upsert_to_local(chart: dict) -> None:
    """Upsert the chart into local MongoDB patients collection."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
    from backend.services.emr.db import get_db

    cpmrn = chart.get("CPMRN")
    if not cpmrn:
        raise ValueError("chart has no CPMRN field")
    chart["_synced_at"] = datetime.now(timezone.utc)
    db = get_db()
    db.patients.update_one({"CPMRN": cpmrn}, {"$set": chart}, upsert=True)
    logger.info("upsert_to_local: saved chart for CPMRN=%s", cpmrn)
