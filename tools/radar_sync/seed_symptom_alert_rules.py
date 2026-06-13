"""
One-time script to seed the initial symptom alert rules into GCS app_settings.

Run from repo root:
    python tools/radar_sync/seed_symptom_alert_rules.py [--dry-run]

After seeding, edit rules by uploading a new JSON to:
    app_settings/symptom_alert_rules.json
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
        "problem": "pain",
        "aliases": [
            "pain", "abdominal pain", "chest pain", "epigastric pain",
            "back pain", "headache", "flank pain", "pelvic pain",
        ],
        "min_score": 7,
        "score_name": "NRS/VAS pain score",
        "objective_criteria": [
            "new tachycardia (HR > 120 bpm) plausibly caused by pain severity",
            "new hypertension (SBP > 180 mmHg) plausibly caused by pain",
            "imaging or lab finding showing objective worsening of the underlying cause"
            " (e.g. new free fluid on USG, rising lipase, new peritoneal signs documented"
            " by the clinical team)",
        ],
        "notes": (
            "Subjective pain report alone is insufficient. Paracetamol or opioid already"
            " prescribed counts as a management plan — do not alert because you judge it"
            " inadequate."
        ),
    },
    {
        "problem": "nausea",
        "aliases": ["nausea", "vomiting", "nausea and vomiting", "n/v"],
        "min_score": None,
        "score_name": None,
        "objective_criteria": [
            "documented inability to tolerate oral medications affecting critical drug delivery",
            "new electrolyte disturbance attributable to vomiting (hypokalemia, metabolic alkalosis)",
            "signs of aspiration or airway compromise",
        ],
        "notes": (
            "Nausea/vomiting reported by patient alone is insufficient without objective"
            " consequence documented above."
        ),
    },
    {
        "problem": "dyspnea",
        "aliases": [
            "dyspnea", "dyspnoea", "breathlessness", "shortness of breath",
            "sob", "reported breathlessness",
        ],
        "min_score": None,
        "score_name": None,
        "objective_criteria": [
            "SpO2 < 92% (verified reading, not unverified monitor capture)",
            "RR > 28 breaths/min on verified vital",
            "new infiltrate or pleural effusion on imaging",
        ],
        "notes": (
            "Patient-reported breathlessness alone is insufficient — the vital sign floors"
            " (SpO2, RR) must be breached on a verified reading."
        ),
    },
]

DOC = {
    "_id": "symptom_alert_rules",
    "enabled": True,
    "rules": INITIAL_RULES,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed symptom alert rules into GCS app_settings")
    parser.add_argument("--dry-run", action="store_true", help="Print the doc without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(DOC, indent=2, ensure_ascii=False))
        return

    from backend.services.emr.db import get_db
    db = get_db()

    existing = db["app_settings"].find_one({"_id": "symptom_alert_rules"})
    if existing:
        print(f"[seed] symptom_alert_rules already exists — {len(existing.get('rules', []))} rules found.")
        print("[seed] To overwrite, delete app_settings/symptom_alert_rules.json from GCS first.")
        return

    db["app_settings"].insert_one(DOC)
    print(f"[seed] Wrote {len(INITIAL_RULES)} rules to app_settings/symptom_alert_rules.json")
    for r in INITIAL_RULES:
        print(f"  • {r['problem']}: {r['notes']}")


if __name__ == "__main__":
    main()
