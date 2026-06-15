"""
One-time script to seed monitoring protocols into the monitoring_protocols collection.

Run from repo root:
    python -m tools.radar_sync.seed_monitoring_protocols [--dry-run]

Adding a new dynamic monitoring protocol = adding a new document here and re-running.
No code changes required.

Protocol schema:
  protocol_id     — unique key (used for matching in patient_problems.context_gate)
  applies_when    — list of lowercase substrings matched against problem_name.lower()
  gate_question   — plain-language question the model is asked to answer
  scenarios       — list of permissive scenarios to rule in/out (each with band_description,
                    window description, and invalidate_if triggers)
  escalation_target_after_window — what to taper toward once the window closes
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

PROTOCOLS = [
    {
        "protocol_id": "permissive-hypertension",
        "applies_when": [
            "hypertension",
            "high bp",
            "hypertensive",
            "elevated blood pressure",
            "high blood pressure",
        ],
        "gate_question": (
            "Is this patient's elevated blood pressure currently INTENDED? "
            "Rule in or out each permissive scenario below. "
            "Each has its own permitted band, clinical window, and invalidation triggers."
        ),
        "scenarios": [
            {
                "name": "acute_ischaemic_stroke_no_tpa",
                "description": "Acute ischaemic stroke WITHOUT thrombolysis",
                "band_description": "SBP ≤ 220 mmHg AND DBP ≤ 120 mmHg",
                "window": "First 48 hours from stroke onset",
                "invalidate_if": [
                    "tPA / alteplase / thrombolysis given",
                    "more than 48 hours have passed since stroke onset",
                    "acute neurological deterioration",
                    "hypertensive emergency with end-organ damage",
                ],
            },
            {
                "name": "acute_ischaemic_stroke_post_tpa",
                "description": "Acute ischaemic stroke WITH thrombolysis — tighter target",
                "band_description": "SBP ≤ 180 mmHg AND DBP ≤ 105 mmHg (NOT the same as no-tPA window)",
                "window": "First 24 hours post-tPA",
                "invalidate_if": [
                    "more than 24 hours since tPA given",
                    "haemorrhagic transformation confirmed on imaging",
                ],
            },
            {
                "name": "spinal_cord_injury",
                "description": "Acute traumatic spinal cord injury — MAP augmentation",
                "band_description": "MAP 85–90 mmHg (maintain, do not drop below 85)",
                "window": "7 days from time of spinal cord injury",
                "invalidate_if": [
                    "more than 7 days from injury",
                    "haemodynamic instability requiring vasopressor reduction",
                ],
            },
            {
                "name": "sah_vasospasm",
                "description": "Post-SAH vasospasm with secured aneurysm — induced hypertension",
                "band_description": "Induced hypertension as directed by neurosurgery/neuro-ICU team",
                "window": "Days 4–14 post-SAH bleed (peak vasospasm window), aneurysm must be secured",
                "invalidate_if": [
                    "vasospasm resolved on TCD or CTA angiography",
                    "aneurysm not yet secured",
                    "outside day 4–14 window",
                    "haemorrhagic complication",
                ],
            },
            {
                "name": "raised_icp",
                "description": "Raised intracranial pressure — maintain cerebral perfusion pressure",
                "band_description": "MAP sufficient to maintain CPP ≥ 60–70 mmHg (CPP = MAP − ICP)",
                "window": "While ICP remains elevated (ICP > 20 mmHg)",
                "invalidate_if": [
                    "ICP normalised (sustained < 20 mmHg)",
                    "ICP monitoring removed",
                    "CPP target met at normal MAP",
                ],
            },
            {
                "name": "ich_acute",
                "description": "Intracerebral haemorrhage — first 24 hours",
                "band_description": "SBP ≤ 150–160 mmHg (do not aggressively lower below 140)",
                "window": "First 24 hours from ICH onset",
                "invalidate_if": [
                    "more than 24 hours from ICH onset",
                    "haematoma expansion confirmed",
                ],
            },
            {
                "name": "renovascular_chronic",
                "description": "Bilateral renal artery stenosis or chronic renovascular hypertension",
                "band_description": "Tolerate above-normal BP; target near patient's documented chronic baseline",
                "window": "Chronic — ongoing while diagnosis applies",
                "invalidate_if": [
                    "renovascular diagnosis ruled out",
                    "renal function acutely deteriorating despite permissive BP",
                ],
            },
        ],
        "escalation_target_after_window": (
            "Once the permissive window closes, taper toward SBP < 140 mmHg over 24–48 hours. "
            "Rate of reduction: no more than ~15% of presenting BP in the first 24 hours of treatment. "
            "Do NOT drop rapidly — autoregulation takes time to re-establish."
        ),
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed monitoring protocols into MongoDB")
    parser.add_argument("--dry-run", action="store_true", help="Print the docs without writing")
    args = parser.parse_args()

    if args.dry_run:
        print(json.dumps(PROTOCOLS, indent=2, ensure_ascii=False, default=str))
        return

    from backend.services.emr.db import get_db
    db = get_db()

    for protocol in PROTOCOLS:
        pid = protocol["protocol_id"]
        db["monitoring_protocols"].update_one(
            {"protocol_id": pid},
            {"$set": protocol},
            upsert=True,
        )
        print(f"[seed] Upserted protocol: {pid}")
        for s in protocol.get("scenarios", []):
            print(f"  • {s['name']}: {s['band_description']}")

    print(f"\n[seed] Done — {len(PROTOCOLS)} protocol(s) seeded into monitoring_protocols.")


if __name__ == "__main__":
    main()
