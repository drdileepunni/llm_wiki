"""
One-time script to seed the initial lab alert rules into GCS app_settings.

Run from repo root:
    python tools/radar_sync/seed_lab_alert_rules.py [--dry-run]

After seeding, edit rules by uploading a new JSON to:
    app_settings/lab_alert_rules.json
in the GCS bucket — no redeploy needed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

INITIAL_RULES = [
    {
        "lab": "platelets",
        "aliases": ["platelets", "plt", "platelet count"],
        "absolute_floor": 20000,
        "absolute_ceiling": None,
        "delta_pct": 30,
        "delta_abs": None,
        "delta_direction": "drop",
        "logic": "floor_OR_delta",
        "unit": "cells/µL",
        "notes": "Alert if < 20,000 cells/µL OR drop ≥ 30% from prior value",
    },
    {
        "lab": "hemoglobin",
        "aliases": ["hemoglobin", "haemoglobin", "hb", "hgb"],
        "absolute_floor": 7.0,
        "absolute_ceiling": None,
        "delta_pct": None,
        "delta_abs": 1.0,
        "delta_direction": "drop",
        "logic": "floor_OR_delta",
        "unit": "g/dL",
        "notes": "Alert if < 7.0 g/dL OR drop > 1.0 g/dL from prior value",
    },
    {
        "lab": "TLC",
        "aliases": ["tlc", "wbc", "total count", "leukocyte", "white blood cell"],
        "absolute_floor": None,
        "absolute_ceiling": 12000,
        "delta_pct": 100,
        "delta_abs": None,
        "delta_direction": "rise",
        "logic": "ceiling_AND_delta",
        "unit": "cells/µL",
        "notes": "Alert if WBC > 12,000 cells/µL AND rise ≥ 100% from prior value",
    },
]

DOC = {
    "_id": "lab_alert_rules",
    "enabled": True,
    "rules": INITIAL_RULES,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed lab alert rules into GCS app_settings")
    parser.add_argument("--dry-run", action="store_true", help="Print the doc without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(DOC, indent=2, ensure_ascii=False))
        return

    from backend.services.emr.db import get_db
    db = get_db()

    existing = db["app_settings"].find_one({"_id": "lab_alert_rules"})
    if existing:
        print(f"[seed] lab_alert_rules already exists in GCS — found {len(existing.get('rules', []))} rules.")
        print("[seed] To overwrite, delete app_settings/lab_alert_rules.json from GCS first.")
        return

    db["app_settings"].insert_one(DOC)
    print(f"[seed] Wrote {len(INITIAL_RULES)} rules to app_settings/lab_alert_rules.json")
    for r in INITIAL_RULES:
        print(f"  • {r['lab']}: {r['notes']}")


if __name__ == "__main__":
    main()
