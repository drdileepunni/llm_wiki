"""
One-time script to seed monitoring protocols into the monitoring_protocols collection.

⚠ PRODUCTION BUCKET — always run with the production bucket env var:
    GCS_BUCKET=patientview-cds-pipeline-ops python -m tools.radar_sync.seed_monitoring_protocols

The local default (cds-pipeline-ops) is NOT the production bucket used by Cloud Run.
Running without GCS_BUCKET set will silently write to the wrong bucket and protocols
will have no effect on any live patients.

Dry-run (prints JSON without writing):
    python -m tools.radar_sync.seed_monitoring_protocols --dry-run

Adding a new dynamic monitoring protocol = adding a new document to PROTOCOLS below
and re-running with the production GCS_BUCKET. No code changes required.

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
    {
        "protocol_id": "established-low-gcs",
        "applies_when": [
            "gcs",
            "low gcs",
            "reduced gcs",
            "altered consciousness",
            "neurological deterioration",
            "coma",
            "encephalopathy",
        ],
        "gate_question": (
            "Is this patient's low GCS expected and established given their current clinical context? "
            "DIRECTION RULE — only a NEGATIVE delta (GCS dropping) is clinically concerning. "
            "A positive delta (GCS improving) is always a good sign and must NEVER trigger an alert. "
            "To assess the delta: call get_vital_trend('GCS', n=6) and compare the most recent "
            "reading against the reading from 6 hours ago. "
            "If GCS is stable or improving over the last 6 hours → permissive_active (suppress). "
            "If GCS has dropped ≥2 points within the last 6 hours → permissive_breached (alert). "
            "Do NOT compare current GCS to admission baseline or any value older than 6 hours — "
            "a drop from GCS 15 at admission two days ago is irrelevant if GCS has been stable since."
        ),
        "scenarios": [
            {
                "name": "post_craniotomy",
                "description": "Post-craniotomy / post-neurosurgical procedure — expected neurological depression",
                "band_description": (
                    "GCS stable or improving over the last 6 hours from the post-operative baseline. "
                    "Only alert if GCS drops ≥2 points within the 6-hour window."
                ),
                "window": "First 24–48 hours post-craniotomy or neurosurgical procedure",
                "invalidate_if": [
                    "GCS drops ≥2 points within the last 6 hours (negative delta only — improvement is not a trigger)",
                    "new pupillary asymmetry or non-reactivity documented in a fresh note",
                    "new focal neurological deficit appearing in current assessment",
                    "imaging shows new haematoma, herniation, or acute hydrocephalus",
                ],
            },
            {
                "name": "established_neurological_injury",
                "description": (
                    "Known neurological injury (TBI, ICH, stroke, HIE, SAH, post-arrest) "
                    "with a stable established low GCS"
                ),
                "band_description": (
                    "GCS is unchanged or improving over the last 6 hours. "
                    "A chronically low GCS from a known injury is the patient's current baseline — "
                    "it is NOT acute deterioration. "
                    "Alert only if GCS drops ≥2 points within the 6-hour window."
                ),
                "window": "While GCS is stable or improving within the 6-hour assessment window",
                "invalidate_if": [
                    "GCS drops ≥2 points within the last 6 hours (negative delta — improvement never triggers)",
                    "new clinical signs in a fresh note: pupillary asymmetry, Cushing's triad, abnormal posturing",
                    "new haemorrhage, oedema, or herniation on imaging",
                    "seizure activity not controlled by current regimen AND GCS worsening",
                ],
            },
            {
                "name": "intentional_sedation",
                "description": "Low GCS due to intentional pharmacological sedation (propofol, midazolam, etc.)",
                "band_description": (
                    "GCS is consistent with the documented sedation target "
                    "(deep sedation RASS −4/−5 ≈ GCS 3–6). "
                    "Alert suppressed while sedation is active and GCS matches target. "
                    "Improvement in GCS as sedation is lightened is expected and should never alert."
                ),
                "window": "While sedative infusion is running and targeting deep sedation",
                "invalidate_if": [
                    "sedation discontinued or hold placed AND GCS does not recover within 2 hours",
                    "GCS lower than expected for the current sedation dose (suggests another cause)",
                    "new haemodynamic compromise attributable to oversedation",
                ],
            },
            {
                "name": "post_ictal",
                "description": "Post-ictal GCS depression following a documented seizure",
                "band_description": (
                    "GCS expected to be depressed for up to 2 hours post-seizure. "
                    "Improvement in GCS during this window is expected — never alert on it. "
                    "Alert only if GCS is not recovering after 2 hours OR drops further."
                ),
                "window": "First 2 hours following a documented seizure",
                "invalidate_if": [
                    "more than 2 hours since last documented seizure AND GCS not recovering",
                    "GCS drops further from the post-ictal nadir (negative delta)",
                    "seizure recurrence or status epilepticus",
                ],
            },
        ],
        "escalation_target_after_window": (
            "When an invalidation trigger fires (GCS drops ≥2 points in 6 hours, new brainstem signs, "
            "or uncontrolled seizures), alert immediately. "
            "Neurology or neurosurgery review is warranted. "
            "Consider urgent CT head if not already done within the last 12 hours."
        ),
    },
    {
        "protocol_id": "permissive-respiratory",
        "applies_when": [
            "hypoxia",
            "desaturation",
            "low spo2",
            "low oxygen",
            "tachypnea",
            "tachypnoea",
            "respiratory distress",
            "oxygen requirement",
            "hypoxaemia",
        ],
        "gate_question": (
            "Is this patient's hypoxia or tachypnea likely to be expected and tolerable given their clinical context? "
            "IMPORTANT: Use the SF ratio (SpO2 / FiO2%), not raw SpO2, as the primary oxygenation measure — "
            "call get_vital_trend('SpO2') which returns SF ratio alongside each reading. "
            "Assess each scenario below — only one needs to match to apply permissive monitoring. "
            "If matched, do NOT alert; set next_check as specified and monitor. "
            "A scenario is invalidated if ANY of its invalidation triggers are present. "
            "SpO2 < 85% is NEVER permissive — alert immediately regardless of context."
        ),
        "scenarios": [
            {
                "name": "post_operative",
                "description": (
                    "Post-operative atelectasis, splinting, or pain-driven tachypnea in the first 48 hours — "
                    "both mild hypoxia and isolated tachypnea are expected and usually self-resolving"
                ),
                "band_description": (
                    "Mild hypoxia: SpO2 88–91% AND SF ratio ≥ 200 AND RR ≤ 30 — recheck in 1 hour. "
                    "Isolated tachypnea (no hypoxia): RR 25–30 AND SpO2 ≥ 92% AND SF ratio ≥ 315 — recheck in 2 hours."
                ),
                "window": "First 48 hours from a documented surgical procedure",
                "invalidate_if": [
                    "SpO2 < 88% on any reading",
                    "SF ratio < 200",
                    "RR > 30 on two consecutive readings",
                    "FiO2 requirement increasing across last 3 readings",
                    "new fever > 38.5°C alongside tachypnea (raises pneumonia or PE concern)",
                    "clinical notes document respiratory distress, accessory muscle use, or new wheeze",
                    "new diagnosis of pneumonia, pulmonary embolism, or ARDS",
                    "more than 48 hours since documented surgical procedure",
                ],
            },
            {
                "name": "known_baseline_hypoxia",
                "description": (
                    "Patient with documented chronically low resting SpO2 (COPD, interstitial lung disease, "
                    "obesity hypoventilation) — stable at their own established baseline"
                ),
                "band_description": (
                    "SpO2 at or above the patient's documented resting baseline (typically 88–93%) "
                    "AND SF ratio ≥ 200. "
                    "Do not alert if the patient is at their known baseline — this is not acute deterioration."
                ),
                "window": "Ongoing while no acute deterioration from documented baseline is present",
                "invalidate_if": [
                    "SpO2 drops more than 4% below the patient's documented resting baseline",
                    "SF ratio < 200",
                    "new respiratory complaint or clinical sign not previously documented",
                    "FiO2 requirement increasing across last 3 readings",
                ],
            },
        ],
        "escalation_target_after_window": (
            "If a permissive window expires without improvement, set clinical_status to 'worsening' and alert. "
            "For hypoxia: target SpO2 ≥ 94% (or ≥ 92% for known COPD/baseline hypoxia) on stable or decreasing FiO2. "
            "For tachypnea: target RR < 24 with a documented underlying cause. "
            "Post-operative patients beyond 48 hours should be treated as a primary respiratory problem — "
            "apply standard alert thresholds (SpO2 < 92%, RR > 28)."
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
