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

Protocol schema — a document may carry any combination of:
  protocol_id       — unique key
  applies_when      — list of lowercase substrings matched against problem_name.lower()
  applies_when_secondary — bool: if true, fires when any problem carries a `cause`

  [permissive gate — suppress alerts during expected clinical states]
  gate_question     — plain-language question for the model
  scenarios         — list of {name, description, band_description, window, invalidate_if}
  escalation_target_after_window — what to taper toward once the window closes

  [guidance — inject domain reasoning into the system prompt]
  guidance          — verbatim decision-tree / reasoning text (replaces clinical_rule_blocks.py)

  [audit — documentation audit triggered 12h after first detection]
  audit             — {required_documentation: [...], window_hours: int, record_when_none: str}
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
            "hypoxemia",
            "hypoxaemia",
            "desaturation",
            "low spo2",
            "spo2",
            "oxygen saturation",
            "o2 sat",
            "low oxygen",
            "tachypnea",
            "tachypnoea",
            "respiratory distress",
            "oxygen requirement",
            "respiratory failure",
            "refractory hypox",
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
    {
        "protocol_id": "haemoglobin-alert-criteria",
        "applies_when": [
            "anemia",
            "anaemia",
            "low hemoglobin",
            "low haemoglobin",
            "low hb",
            "hb drop",
            "hemoglobin",
            "haemoglobin",
            "bleeding",
            "blood loss",
        ],
        "gate_question": (
            "Should this patient's anaemia / low Hb trigger an alert? "
            "This protocol defines the ONLY two conditions under which a Hb alert is warranted. "
            "IMPORTANT: call get_lab_trend('Hb') first to get the full trend. "
            "A scenario is ACTIVE (suppress alert) if its band conditions are met and no invalidation trigger fires. "
            "A scenario is BREACHED (alert) if any invalidation trigger fires. "
            "If no scenario matches this patient's context, apply normal alert rules."
        ),
        "scenarios": [
            {
                "name": "stable_hb_general",
                "description": (
                    "General patient without active myocardial ischaemia — "
                    "Hb is acceptable as long as it has not fallen > 1.0 g/dL in the last 24 hours "
                    "and remains above the absolute floor of 7.0 g/dL"
                ),
                "band_description": (
                    "Current Hb ≥ 7.0 g/dL "
                    "AND drop from highest Hb value in the last 24 hours to current Hb is ≤ 1.0 g/dL. "
                    "To compute the 24h drop: from get_lab_trend('Hb'), identify the highest value "
                    "in the last 24 hours and subtract the current value. "
                    "If only one reading exists in 24 hours, compare to the most recent prior reading."
                ),
                "window": (
                    "Ongoing while no active myocardial ischaemia or ACS diagnosis is present"
                ),
                "invalidate_if": [
                    "current Hb < 7.0 g/dL (absolute floor — always alert)",
                    "Hb has dropped > 1.0 g/dL from the highest value in the last 24 hours",
                    "active myocardial ischaemia or ACS is documented (use the cardiac_ischaemia scenario instead)",
                ],
            },
            {
                "name": "stable_hb_cardiac_ischaemia",
                "description": (
                    "Patient with active myocardial ischaemia or ACS — "
                    "higher Hb floor applies because myocardium has reduced tolerance for anaemia. "
                    "Hb ≥ 8.0 g/dL is required AND no drop > 1.0 g/dL in 24 hours."
                ),
                "band_description": (
                    "Current Hb ≥ 8.0 g/dL "
                    "AND drop from highest Hb in the last 24 hours to current Hb is ≤ 1.0 g/dL. "
                    "Active myocardial ischaemia or ACS must be a current documented diagnosis."
                ),
                "window": (
                    "While active myocardial ischaemia, NSTEMI, STEMI, or ACS is a current active diagnosis"
                ),
                "invalidate_if": [
                    "current Hb < 8.0 g/dL (tighter floor for cardiac ischaemia — always alert)",
                    "Hb has dropped > 1.0 g/dL from the highest value in the last 24 hours",
                    "myocardial ischaemia / ACS diagnosis resolves or is no longer active",
                ],
            },
        ],
        "escalation_target_after_window": (
            "When a scenario is breached: set clinical_status='worsening' and alert immediately. "
            "For a rapid drop (> 1.0 g/dL in 24h): investigate active bleeding — review I/O, drain outputs, "
            "surgical site, and GI symptoms. "
            "For absolute floor breach (Hb < 7.0 general, < 8.0 cardiac): assess transfusion need. "
            "Target: Hb ≥ 8.0 g/dL for general patients post-transfusion; Hb ≥ 9.0 g/dL for active cardiac ischaemia."
        ),
    },
    {
        "protocol_id": "lactate-alert-criteria",
        "applies_when": [
            "lactate",
            "hyperlactatemia",
            "lactic acidosis",
            "hypoperfusion",
            "shock",
            "septic shock",
        ],
        "gate_question": (
            "Does this patient's lactate situation require an alert? "
            "HARD RULE — check get_lab_trend('Lactate') first. "
            "If the most recent lactate result is MORE THAN 12 HOURS OLD, STOP immediately: "
            "do NOT comment on, alert on, or reference lactate at all — treat as if no lactate data exists. "
            "Also call get_vital_trend('MAP') to assess for hypotensive events. "
            "Then evaluate the two scenarios below. "
            "A scenario is ACTIVE (suppress alert) if its band conditions are met. "
            "A scenario is BREACHED (alert) if its invalidation triggers are met."
        ),
        "scenarios": [
            {
                "name": "elevated_lactate_with_hypoperfusion",
                "description": (
                    "Elevated lactate requires haemodynamic corroboration before alerting — "
                    "except when critically high (> 4 mmol/L), which is shock-level regardless of MAP. "
                    "Lactate 2–4 mmol/L without MAP < 65 events is likely an isolated lab finding and should not alert."
                ),
                "band_description": (
                    "Suppress alert if ANY of the following: "
                    "(a) lactate ≤ 2.0 mmol/L; "
                    "(b) lactate 2.0–4.0 mmol/L AND no MAP < 65 event in the last 6 hours; "
                    "(c) lactate > 2.0 mmol/L AND a plan note addressing lactate or hypoperfusion "
                    "exists within 6 hours of the lactate result time."
                ),
                "window": "Result must be within 12 hours — older lactate results are ignored entirely",
                "invalidate_if": [
                    "lactate > 4.0 mmol/L AND no plan note within 6 hours of the result (alert regardless of MAP)",
                    "lactate 2.0–4.0 mmol/L AND at least one MAP < 65 event in the last 6 hours "
                    "AND no plan note within 6 hours of the lactate result",
                    "lactate result is more than 12 hours old (stop — do not alert, do not comment)",
                ],
            },
            {
                "name": "persistent_hypotension_without_lactate_workup",
                "description": (
                    "Persistent hypotension without a lactate result is a missed workup — "
                    "a single MAP dip is not sufficient (too transient); "
                    "requires at least 2 consecutive MAP < 65 readings to confirm persistence."
                ),
                "band_description": (
                    "Suppress alert if ANY of the following: "
                    "(a) fewer than 2 consecutive MAP < 65 readings in the last 6 hours; "
                    "(b) a lactate result exists within the last 6 hours (workup done)."
                ),
                "window": "Ongoing — evaluated on each run where MAP trend data is available",
                "invalidate_if": [
                    "2 or more consecutive MAP < 65 readings in the last 6 hours "
                    "AND no lactate result within the last 6 hours",
                ],
            },
        ],
        "escalation_target_after_window": (
            "Scenario 1 breach — alert with: "
            "'Lactate [X] mmol/L ([time IST]) — [with MAP < 65 events / critically elevated] — no management plan documented.' "
            "Investigate for occult hypoperfusion, sepsis, mesenteric ischaemia, or hepatic failure. "
            "Scenario 2 breach — alert with: "
            "'Persistent hypotension (≥ 2 consecutive MAP < 65 readings) — no lactate resulted in 6h. Lactate workup required.' "
            "12h staleness — if lactate > 12h old, suppress all lactate alerts and note: "
            "'Most recent lactate is > 12h old — cannot assess current perfusion status.'"
        ),
    },
    {
        "protocol_id": "acknowledged-myocardial-injury",
        "applies_when": [
            "troponin",
            "myocardial injury",
            "nstemi",
            "acs",
            "acute coronary",
            "elevated troponin",
            "troponin elevation",
            "myocardial infarction",
            "cardiac enzyme",
        ],
        "gate_question": (
            "Has the care team acknowledged this patient's troponin elevation or myocardial injury? "
            "Call get_patient_notes() and check for any note from a care provider that references "
            "troponin, myocardial injury, ACS, NSTEMI, or cardiac enzymes within 24 hours of the "
            "most recent troponin result. "
            "A note that acknowledges the finding — even without a formal management plan — is "
            "sufficient to suppress the alert. Evaluate the scenario below."
        ),
        "scenarios": [
            {
                "name": "care_team_acknowledgment_within_24h",
                "description": (
                    "Troponin elevation is known to and acknowledged by the care team. "
                    "A clinician note referencing the troponin result within 24 hours of the lab draw "
                    "confirms awareness — no specific management plan is required for suppression."
                ),
                "band_description": (
                    "Suppress alert if: a care provider note exists within 24 hours of the most recent "
                    "troponin result that references troponin, myocardial injury, ACS, NSTEMI, or cardiac "
                    "enzymes. Acknowledgment alone is sufficient — a formal plan is not required."
                ),
                "window": "24 hours from the most recent troponin result",
                "invalidate_if": [
                    "no care provider note referencing troponin or myocardial injury exists within "
                    "24 hours of the result",
                    "troponin is rising serially (delta-positive trend) AND the most recent note does not "
                    "acknowledge the rising trend specifically — a note written before the upward trend "
                    "was detected does not count as acknowledgment of the new trend",
                ],
            },
        ],
        "escalation_target_after_window": (
            "Alert with: 'Troponin elevation — no care team acknowledgment documented in the last 24 hours.' "
            "If troponin is rising, note the trend explicitly. "
            "Once the 24-hour window has passed without an acknowledging note, alert regardless of absolute value."
        ),
    },

    # ── Guidance-only protocols (absorbed from clinical_rule_blocks.py) ────────
    # These carry only `guidance` — no permissive gate, no audit.
    # They replace the five category blocks that were previously hardcoded in
    # clinical_rule_blocks.py and injected conditionally per patient.
    {
        "protocol_id": "guidance-neuro",
        "applies_when": [
            "gcs", "consciousness", "conscious", "encephalopath", "coma", "comatose",
            "seizure", "sensorium", "neuro", "altered mental", "obtunded", "drowsy",
        ],
        "guidance": (
            "GCS DELTA — for any problem related to GCS, consciousness, or neurological status: do NOT "
            "alert unless GCS has dropped ≥ 2 points within the last 6 hours. Call "
            "get_vital_trend('GCS', n=6) and compare the most recent reading against the reading from 6 "
            "hours ago. If the delta is < 2 (stable or improving), should_alert=False regardless of the "
            "absolute GCS value, of whether a plan note exists, and of the treatment-inadequate override. "
            "A chronically low GCS is NOT a reason to alert. Only a negative delta ≥ 2 within the 6-hour "
            "window justifies an alert."
        ),
    },
    {
        "protocol_id": "guidance-respiratory",
        "applies_when": [
            "hypox", "spo2", "sp02", "desaturat", "tachypn", "respiratory", "ards",
            "oxygen", "ventilat", "fio2", "hypercapn", "pneumon", "weaning",
        ],
        "guidance": (
            "- For respiratory problems: NEVER judge SpO2 in isolation. get_vital_trend('SpO2') "
            "returns the SF ratio (SpO2 / FiO2%) alongside each reading. Use the SF ratio trend, "
            "not raw SpO2, to assess oxygenation. If FiO2 was reduced and the SF ratio is stable or "
            "improved, the SpO2 drop is planned weaning — treat as stable or improving, NOT worsening.\n"
            "- FiO2 RULE — FiO2 is a clinician-controlled ventilator setting, not a patient parameter. Do "
            "NOT set clinical_status=\"worsening\"/\"critical\" and do NOT alert based on FiO2 changes "
            "alone. FiO2 increases are intentional clinical interventions — alerting on them is circular. "
            "To assess oxygenation use SpO2 or SF ratio (both via get_vital_trend('SpO2')). If SpO2 is "
            "maintained ≥92% despite high FiO2, oxygenation is being managed — do not alert."
        ),
    },
    {
        "protocol_id": "guidance-vital-hard-ceiling",
        "applies_when": [
            "hypertension", "high bp", "blood pressure", "hypertensive",
            "hypotension", "low bp", "shock", "map",
            "tachycard", "bradycard", "heart rate",
            "hypox", "spo2", "sp02", "desaturat",
        ],
        "guidance": (
            "HARD-CEILING OVERRIDE (BP / HR / SpO2) — a documented management plan does NOT justify "
            "staying silent when the CURRENT value is in an emergency range. For these problems, "
            "if the most recent reading breaches ANY of the hard ceilings below, the plan is by "
            "definition not controlling the problem: set being_addressed=FALSE and should_alert=TRUE, "
            "clinical_status=\"critical\". This overrides note-freshness (Step 3), the response buffer "
            "(Step 4), and the being_addressed suppression — a plan that leaves the patient in these "
            "ranges is inadequate, not reassuring.\n"
            "  Hard ceilings (any single breach on the most recent reading triggers the override):\n"
            "    • Systolic BP ≥ 180 mmHg  (hypertensive emergency)\n"
            "    • Systolic BP ≤ 90 mmHg  OR  MAP < 60 mmHg  (haemodynamic compromise)\n"
            "    • Heart rate ≥ 130 bpm  OR  ≤ 40 bpm\n"
            "    • SpO2 ≤ 88%  (severe hypoxia — the SF-ratio co-requirement does NOT apply at this depth)\n"
            "Only the permissive-context gate (Step 1, e.g. a matched permissive-hypertension scenario) "
            "and an emergency procedure already underway (Step 2, e.g. active CPR/intubation) still take "
            "precedence. If neither applies and a ceiling is breached, you MUST alert even if a plan is "
            "documented. Do NOT set being_addressed=True alongside should_alert=True — that contradiction "
            "is suppressed downstream; report being_addressed=False so the alert is delivered."
        ),
    },
    {
        "protocol_id": "guidance-renal",
        "applies_when": [
            "aki", "kidney", "oliguri", "anuri", "creatinine", "renal",
            "fluid overload", "fluid balance", "rrt", "dialysis",
        ],
        "guidance": (
            "- For AKI, oliguria, anuria, or fluid-balance problems: ALWAYS call get_io before concluding "
            "output is absent — the structured summary may not reflect the latest I/O data. get_io shows "
            "DAILY TOTALS first, then hourly detail. If the hourly window shows 0 ml but the daily total "
            "is non-zero, I/O is charted as a daily batch entry — do NOT interpret as anuria; use the "
            "daily total to assess fluid balance.\n"
            "- I/O charting in ICUs is frequently incomplete or entered retrospectively. Zero urine "
            "output in the chart — even across several consecutive hours — does NOT reliably indicate "
            "true anuria or oliguria. Always treat recorded 0 ml output as 'possible missed charting' "
            "unless ALL three of the following are true: (1) the daily total is also 0 ml, "
            "(2) clinical notes explicitly document anuria or oliguria, AND (3) creatinine is rising. "
            "Do NOT escalate AKI, alert for anuria, or conclude oliguria on "
            "the basis of 0 ml charting alone."
        ),
    },
    {
        "protocol_id": "guidance-symptom",
        "applies_when": [
            "pain", "nausea", "dizzin", "breathless", "fatigue",
            "chest tightness", "discomfort", "vomit",
        ],
        "guidance": (
            "SUBJECTIVE SYMPTOM RULE — do NOT set clinical_status=\"worsening\"/\"critical\" and do NOT "
            "alert for problems whose primary evidence is a patient-reported symptom (pain, nausea, "
            "dizziness, fatigue, reported breathlessness, reported chest tightness) without corroborating "
            "objective evidence. \"Patient reports severe pain\", \"patient complains of nausea\", or "
            "\"patient feels breathless\" alone is NOT sufficient to alert. Objective evidence means at "
            "least ONE of:\n"
            "  • A validated numeric score meeting a documented threshold (e.g. NRS/VAS pain score ≥ 7/10 "
            "explicitly recorded)\n"
            "  • A physiological correlate that itself breaches the VITAL SIGN ALERT FLOORS (new "
            "tachycardia, hypotension, hypoxia, etc.) AND is plausibly caused by the symptom\n"
            "  • An imaging or lab finding showing objective worsening of the underlying cause\n"
            "If none are present, classify the symptom-based problem as stable and should_alert=False. The "
            "adequacy of the current treatment plan is the clinician's call — do NOT alert purely because "
            "you judge the prescribed analgesic or antiemetic insufficient."
        ),
    },
    {
        "protocol_id": "guidance-causal-secondary",
        "applies_when": [],
        "applies_when_secondary": True,
        "guidance": (
            "CAUSAL / SECONDARY PROBLEMS\n"
            "When a problem is marked secondary (has a cause), apply this reasoning:\n"
            "- If the primary driver (the cause) is being_addressed=True and its clinical_status is NOT "
            "\"critical\" or \"worsening\", the secondary problem should NOT generate an independent alert "
            "solely because its own parameters remain abnormal. Rationale: secondary organ dysfunction "
            "(AKI, coagulopathy, thrombocytopaenia) lags behind the primary problem by 24–72h. Treating "
            "the cause IS the treatment.\n"
            "- Exception — DO alert for the secondary problem if ANY of the following are present "
            "regardless of the primary driver's status:\n"
            "    • A rapid step-change worsening (e.g. creatinine rises >50% from last snapshot)\n"
            "    • A value in a life-threatening range (K+ ≥ 6.0, pH < 7.20, bicarb < 12)\n"
            "    • A clinical sign requiring independent intervention (RRT indication, dialysis)\n"
            "- If the primary driver is NOT being_addressed, assess the secondary problem normally."
        ),
    },

    # ── Global objectivity rule — applies to every problem ───────────────────
    {
        "protocol_id": "guidance-objectivity",
        "applies_when": [],           # empty = matches every problem
        "guidance": (
            "OBJECTIVITY RULE — applies to every tracked problem, without exception.\n"
            "\n"
            "Only alert on clinical findings that meet ALL of the following:\n"
            "  1. MEASURABLE — the finding must be expressible as a number, a validated score, "
            "     a confirmed diagnosis, a documented procedure, or a concrete clinical observation "
            "(e.g. new peripheral oedema on exam, documented desaturation). "
            "Vague status labels such as 'Watcher', 'High Dependency Watch', 'Step-down', "
            "'condition fair', 'condition critical', 'deteriorating', 'improving', "
            "or any other administrative triage designation are NOT clinical findings.\n"
            "  2. THRESHOLD-CROSSING — the value or finding must breach a documented clinical "
            "     threshold (a vital sign alert floor, a lab reference range, a validated score "
            "     cut-off, or a clearly worsening trend with a quantified delta). A change from "
            "     one descriptive label to another (e.g. 'fair' → 'Watcher') is NOT a "
            "     threshold crossing.\n"
            "  3. INDEPENDENT — the finding must not be solely derived from an administrative "
            "     EMR field, a monitoring category, or a triage designation. 'Patient placed on "
            "Watcher status' documents a monitoring level — it is not a clinical problem "
            "requiring its own management plan.\n"
            "\n"
            "If the only evidence for a 'problem' is a broad status label or a note that the "
            "patient's overall condition has changed category, do NOT create it as a tracked "
            "problem and do NOT alert. Instead, look for the underlying objective reason "
            "(e.g. new tachycardia, rising creatinine) and assess that finding directly.\n"
            "\n"
            "Examples — DO NOT alert:\n"
            "  • 'Patient placed on Watcher status'\n"
            "  • 'Condition changed from stable to guarded'\n"
            "  • 'Patient deteriorating per nursing note'\n"
            "  • 'Overall clinical status worsening'\n"
            "Examples — DO alert (if thresholds met):\n"
            "  • HR increased from 88 to 118 bpm over 2 hours\n"
            "  • Creatinine rose from 1.2 to 2.1 mg/dL in 24h\n"
            "  • Pain NRS score 8/10 with objective tachycardia\n"
            "  • SpO₂ dropped from 97% to 88% on current FiO₂"
        ),
    },

    # ── Tachycardia — first unified consumer (guidance + audit) ──────────────
    {
        "protocol_id": "tachycardia",
        "applies_when": [
            "tachycard", "tachyarrhythmia", "arrhythmia", "atrial fibrillation",
            "svt", "palpitation", "rate control", "sinus tach",
        ],
        "guidance": (
            "TACHYCARDIA ASSESSMENT PROTOCOL (entry: HR > 100 bpm)\n"
            "\n"
            "STEP 1 — IS THIS UNSTABLE TACHYCARDIA? (alert immediately if yes)\n"
            "Unstable = ANY of the following:\n"
            "  • HR > 150 bpm (alone, regardless of coexisting factors)\n"
            "  • HR > 100 bpm AND any coexisting haemodynamic / respiratory derangement:\n"
            "      - MAP < 65 mmHg OR SBP < 90 mmHg (or a downward trend toward these)\n"
            "      - RR > 28 breaths/min OR rising respiratory effort\n"
            "      - SpO₂ < 92% (with SF ratio < 350)\n"
            "      - GCS drop ≥ 1 point in the last 6 hours\n"
            "If unstable: set clinical_status=critical, should_alert=True. Override note-freshness and "
            "response-buffer suppression rules. Message: 'HR [X] bpm — unstable tachycardia. "
            "Assess for peri-arrest. Consider ACLS.' Do not suppress for any existing plan note.\n"
            "\n"
            "STEP 2 — IS THIS ISOLATED STABLE TACHYCARDIA? (HR 100–150, no coexisting derangement)\n"
            "Check get_patient_notes() for a documented reversible cause within the last 24 hours:\n"
            "  Acceptable documented causes: pain, fever (Temp > 38.5°C with documented management plan), "
            "delirium / ICU psychosis (documented and being managed), sedation inadequacy "
            "(documented RASS target mismatch being addressed), hypovolaemia / fluid deficit "
            "(documented and being corrected).\n"
            "\n"
            "  IF a reversible cause IS documented AND a management plan is present:\n"
            "    set being_addressed=True, should_alert=False, next_check in 1 hour.\n"
            "    Note: 'HR [X] bpm — tachycardia attributed to [cause], management documented.'\n"
            "\n"
            "  IF no reversible cause is documented (or no plan for a documented cause):\n"
            "    set clinical_status=worsening, should_alert=True (care-gap alert).\n"
            "    Message: 'HR [X] bpm — no documented reversible cause. Workup recommended: "
            "12-lead ECG, cardiac enzymes, serum electrolytes and ABG. Document the aetiology.'\n"
            "    Additionally: call get_io() — if 24-hour urine output < 0.5 ml/kg/h AND no fluid "
            "resuscitation plan is documented, add: 'Low urine output noted. Consider fluid bolus "
            "10–30 mL/kg if haemodynamically appropriate and no contraindication.'\n"
            "\n"
            "STEP 3 — HR AT THRESHOLD (HR exactly 100–105 bpm)\n"
            "Treat as isolated stable tachycardia (Step 2). A single borderline reading with a "
            "plausible documented cause may be classified stable with 1–2h recheck."
        ),
        "audit": {
            "required_documentation": [
                "fever / infection aetiology documented",
                "pain assessment and management documented",
                "delirium / ICU psychosis documented",
                "sedation adequacy documented",
                "urine output and fluid status documented",
            ],
            "window_hours": 12,
            "record_when_none": "inadequate_documentation",
        },
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
