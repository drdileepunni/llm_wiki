"""
Trial run: agentic CDS Step 2 grounding for hyperkalemia severity classification.

Seeds a K+ result into the viva dummy patient, then calls run_chat in CDS mode
with a hyperkalemia brief. Verifies that Step 2 calls get_latest_lab, classifies
the tier, and grounds the correct doses.

Usage (from repo root):
    app/.venv/bin/python test_agentic_grounding.py [--k 6.2]

Default K+ = 6.2 (moderate). Try 5.7 (mild) or 6.8 (severe) to test all tiers.
"""

import sys
import os
import json
import argparse
import logging
from pathlib import Path

# ── Path + env setup ──────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / "app"))
os.environ.setdefault("WIKI_ROOT", str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "app" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logging.getLogger("wiki.chat_pipeline").setLevel(logging.DEBUG)
log = logging.getLogger("trial")

# ── Args ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--k", type=float, default=6.2, help="Serum K+ to seed (mmol/L)")
args = parser.parse_args()
potassium_value = args.k

if potassium_value < 6.0:
    expected_tier = "MILD"
elif potassium_value < 6.5:
    expected_tier = "MODERATE"
else:
    expected_tier = "SEVERE"

print(f"\n{'='*70}")
print(f"  Seeding K+ = {potassium_value} mmol/L  →  Expected tier: {expected_tier}")
print(f"{'='*70}\n")

# ── Imports ───────────────────────────────────────────────────────────────────
from backend.services.emr.patient import (
    push_lab_result,
    upsert_dummy_patient,
    VIVA_DUMMY_CPMRN,
)
from backend.services.chat_pipeline import run_chat
from backend.config import get_kb

# ── 1. Ensure dummy patient exists ────────────────────────────────────────────
log.info("Upserting dummy patient CPMRN=%s", VIVA_DUMMY_CPMRN)
upsert_dummy_patient({
    "name": "Trial Patient",
    "age_years": 65,
    "gender": "male",
    "weight_kg": 70.0,
    "creatinine": 2.4,
    "diagnoses": ["Type 2 diabetes mellitus", "CKD stage 3"],
    "home_meds": ["Metformin 500mg BD", "Lisinopril 10mg OD"],
    "allergies": [],
})

# ── 2. Seed the K+ result ─────────────────────────────────────────────────────
log.info("Pushing K+ = %.1f mmol/L to patient chart", potassium_value)
push_lab_result(
    VIVA_DUMMY_CPMRN,
    name="U&E",
    attributes={
        "potassium": {
            "name": "Potassium",
            "value": str(potassium_value),
            "errorRange": {"min": 3.5, "max": 5.0},
        },
        "sodium": {
            "name": "Sodium",
            "value": "138",
            "errorRange": {"min": 136, "max": 145},
        },
        "creatinine": {
            "name": "Creatinine",
            "value": "214",
            "errorRange": {"min": 60, "max": 110},
        },
        "urea": {
            "name": "Urea",
            "value": "14.2",
            "errorRange": {"min": 2.5, "max": 7.8},
        },
    },
)
print(f"✓ Seeded U&E with K+ = {potassium_value} mmol/L\n")

# ── 3. Run CDS pipeline ───────────────────────────────────────────────────────
kb = get_kb("agent_school")

brief = (
    f"65-year-old male with known CKD stage 3 and Type 2 DM on Metformin and Lisinopril. "
    f"Admitted with nausea and generalised weakness. "
    f"U&E shows potassium {potassium_value} mmol/L, creatinine 214 umol/L. "
    f"ECG shows peaked T-waves. "
    f"What is the appropriate immediate management?"
)

print(f"Brief: {brief}\n")
print("Running CDS pipeline (watch for 'CDS Step 2 agentic' log lines)...\n")

result = run_chat(
    question=brief,
    kb=kb,
    mode="cds",
    cpmrn=VIVA_DUMMY_CPMRN,
    include_patient_context=True,
)

# ── 4. Print results ──────────────────────────────────────────────────────────
# run_chat CDS mode returns fields directly (not nested under "snapshots")
snap = result

print(f"\n{'='*70}")
print("  CDS OUTPUT")
print(f"{'='*70}\n")

print("── Immediate Next Steps ──────────────────────────────────────────────")
for s in snap.get("immediate_next_steps", []):
    print(f"  • {s}")

print("\n── Specific Parameters (grounded doses) ──────────────────────────────")
for p in snap.get("specific_parameters", []):
    print(f"  • {p}")

print("\n── Monitoring / Follow-up ────────────────────────────────────────────")
for m in snap.get("monitoring_followup", []):
    print(f"  • {m}")

print("\n── Pages Consulted ───────────────────────────────────────────────────")
for pg in snap.get("pages_consulted", []):
    print(f"  • {pg}")

print("\n── Clinical Reasoning ────────────────────────────────────────────────")
for r in snap.get("clinical_reasoning", []):
    print(f"  • {r}")

cost = snap.get("cost_usd", 0)
print(f"\n── Cost: ${cost:.4f}  ──────────────────────────────────────────────\n")

# ── 5. Verdict ────────────────────────────────────────────────────────────────
def _to_str(x):
    return json.dumps(x) if isinstance(x, dict) else str(x)

all_text = " ".join(
    [_to_str(x) for x in snap.get("immediate_next_steps", [])] +
    [_to_str(x) for x in snap.get("specific_parameters", [])] +
    [_to_str(x) for x in snap.get("clinical_reasoning", [])]
).lower()

tier_detected = None
if "severe" in all_text or "30 ml" in all_text or "3 g calcium" in all_text:
    tier_detected = "SEVERE"
elif "moderate" in all_text or "salbutamol" in all_text or ("10 unit" in all_text and "insulin" in all_text):
    tier_detected = "MODERATE"
elif "mild" in all_text or "patiromer" in all_text or "dietary restriction" in all_text:
    tier_detected = "MILD"

print(f"{'='*70}")
print(f"  Expected tier : {expected_tier}")
print(f"  Detected tier : {tier_detected or 'UNKNOWN (check output above)'}")
matched = tier_detected == expected_tier
print(f"  Result        : {'PASS ✓' if matched else 'MISMATCH — review output'}")
print(f"{'='*70}\n")
