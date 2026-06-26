"""
One-time script to seed the default lab normal ranges into GCS app_settings.

Run from repo root:
    source .venv/bin/activate
    GCS_BUCKET=patientview-cds-pipeline-ops python -m tools.radar_sync.seed_lab_normal_ranges [--dry-run]

After seeding, edit ranges from the dashboard (Lab Alert Rules tab → Edit Ranges)
or by uploading a new JSON to app_settings/lab_normal_ranges.json in the GCS bucket.
No redeploy needed.
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

from tools.radar_sync.lab_normal_ranges import DEFAULT_CONFIG


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed lab normal ranges into GCS app_settings")
    parser.add_argument("--dry-run", action="store_true", help="Print the doc without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        return

    from backend.services.emr.db import get_db
    db = get_db()

    existing = db["app_settings"].find_one({"_id": "lab_normal_ranges"})
    if existing:
        n = sum(
            len(p.get("ranges", [])) for p in existing.get("panels", {}).values()
        )
        print(f"[seed] lab_normal_ranges already exists in GCS — found {n} range entries "
              f"across {len(existing.get('panels', {}))} panels.")
        print("[seed] To overwrite, delete app_settings/lab_normal_ranges.json from GCS first.")
        return

    db["app_settings"].insert_one(DEFAULT_CONFIG)
    total = sum(len(p.get("ranges", [])) for p in DEFAULT_CONFIG["panels"].values())
    print(f"[seed] Wrote {total} range entries across {len(DEFAULT_CONFIG['panels'])} panels "
          "to app_settings/lab_normal_ranges.json")
    for panel_key, panel in DEFAULT_CONFIG["panels"].items():
        always = " [always normal]" if panel.get("always_normal") else ""
        n = len(panel.get("ranges", []))
        print(f"  • {panel_key}: {n} range(s){always}")


if __name__ == "__main__":
    main()
