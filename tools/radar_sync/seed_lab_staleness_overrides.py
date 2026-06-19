"""
One-time script to seed the initial lab staleness overrides into GCS app_settings.

Run from repo root:
    source .venv/bin/activate
    GCS_BUCKET=patientview-cds-pipeline-ops python tools/radar_sync/seed_lab_staleness_overrides.py [--dry-run]

After seeding, edit overrides via the dashboard Config → Lab Staleness tab — no redeploy needed.
The doc lives at: gs://patientview-cds-pipeline-ops/app_settings/lab_staleness_overrides.json
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

# Keys are lowercase lab names that match what get_lab_trend / next_check.lab_name returns.
# Values are the maximum result age in hours before the pipeline treats the lab as too stale
# to base a new alert on.
INITIAL_OVERRIDES: dict[str, int] = {
    "lactate": 12,   # lactate changes rapidly; a >12h result doesn't reflect current perfusion
}

DOC = {
    "_id": "lab_staleness_overrides",
    "enabled": True,
    "overrides": INITIAL_OVERRIDES,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed lab staleness overrides into GCS app_settings")
    parser.add_argument("--dry-run", action="store_true", help="Print the doc without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(DOC, indent=2, ensure_ascii=False))
        return

    from backend.services.emr.db import get_db
    db = get_db()

    existing = db["app_settings"].find_one({"_id": "lab_staleness_overrides"})
    if existing:
        print("[seed] lab_staleness_overrides already exists in GCS.")
        print(f"       Current overrides: {existing.get('overrides', {})}")
        print("[seed] To overwrite, delete app_settings/lab_staleness_overrides.json from GCS first.")
        return

    db["app_settings"].insert_one(DOC)
    print("[seed] Wrote lab_staleness_overrides to app_settings/lab_staleness_overrides.json")
    for lab, hours in INITIAL_OVERRIDES.items():
        print(f"  • {lab}: max {hours}h")


if __name__ == "__main__":
    main()
