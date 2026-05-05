"""
Seed missing monitoring / procedure orderables into MongoDB.

Run from the repo root:
    python scripts/seed_monitoring_orderables.py

Uses upsert on (name, type, patientType) so it's safe to re-run.
"""

import os
import sys

# Allow imports from app/backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.backend.services.emr.db import get_db

ENTRIES = [
    # ── Capnography / EtCO2 ──────────────────────────────────────────────────
    {
        "name": "EtCO2 monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "continuous",
            "instructions": "Continuous waveform capnography. Target EtCO2 35–40 mmHg (normocapnia) unless otherwise specified.",
        },
    },
    # ── Temperature ──────────────────────────────────────────────────────────
    {
        "name": "Temperature monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "every 1 hours",
            "instructions": "Monitor core temperature. Target normothermia 36.0–37.5°C unless targeted temperature management is in place.",
        },
    },
    # ── Arterial line ────────────────────────────────────────────────────────
    {
        "name": "Arterial line",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "invasive",
            "frequency": "continuous",
            "instructions": "Continuous invasive arterial blood pressure monitoring. Preferred site: radial artery.",
        },
    },
    # ── Neurological monitoring ──────────────────────────────────────────────
    {
        "name": "Neurological monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "every 1 hours",
            "instructions": "Neurological observations: GCS, pupillary response, focal deficits. Escalate for any acute change.",
        },
    },
    # ── Blood glucose monitoring ─────────────────────────────────────────────
    {
        "name": "Blood glucose monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "every 1 hours",
            "instructions": "Point-of-care blood glucose. Target 6–10 mmol/L in ICU unless otherwise specified.",
        },
    },
    # ── SpO2 monitoring ──────────────────────────────────────────────────────
    {
        "name": "SpO2 monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "continuous",
            "instructions": "Continuous pulse oximetry.",
        },
    },
    # ── Urine output monitoring ──────────────────────────────────────────────
    {
        "name": "Urine output monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "monitoring",
            "frequency": "every 1 hours",
            "instructions": "Hourly urine output via urinary catheter. Target ≥ 0.5 mL/kg/hr.",
        },
    },
    # ── ICP monitoring ───────────────────────────────────────────────────────
    {
        "name": "ICP monitoring",
        "type": "procedure",
        "patientType": "adult",
        "presets": {
            "form": "invasive",
            "frequency": "continuous",
            "instructions": "Continuous intracranial pressure monitoring. Target ICP < 20 mmHg, CPP 60–70 mmHg.",
        },
    },
]


def main():
    db = get_db()
    col = db.orderables
    inserted = 0
    updated = 0

    for entry in ENTRIES:
        key = {"name": entry["name"], "type": entry["type"], "patientType": entry["patientType"]}
        result = col.update_one(key, {"$set": entry}, upsert=True)
        if result.upserted_id:
            print(f"  INSERTED  {entry['name']}")
            inserted += 1
        else:
            print(f"  UPDATED   {entry['name']}")
            updated += 1

    print(f"\nDone. {inserted} inserted, {updated} updated.")


if __name__ == "__main__":
    main()
