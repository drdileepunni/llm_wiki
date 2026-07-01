"""
Problem tracker — per-patient ReAct reasoning loop that maintains a persistent
problem list in MongoDB and fires targeted alerts.

Flow (runs once per patient per hourly scheduler cycle, replacing the CDS gate):
  1. Load current structured_summary.problems[] from summary_updater output
  2. Load stored patient_problems[] from MongoDB
  3. Run a single ReAct reasoning session (gemini-3.1-flash-lite with thinking)
     — model has tools to query vitals, labs, notes, and stored problem state
     — model calls set_all_assessments() once when done
  4. Upsert patient_problems[] in MongoDB
  5. Fire Google Chat alert for any problem with should_alert=True

Alert rules (applied by code, not model):
  - New problem, not being addressed → alert immediately
  - Existing problem: next_check overdue (due_after elapsed) AND not fulfilled → alert
  - Never re-alert the same problem within ALERT_COOLDOWN_HOURS if still being addressed

next_check timing (hardcoded):
  - type "vital" → due_after = now + 1h
  - type "lab"   → due_after = now + 6h
  - type "io"    → due_after = now + 1h  (urine output / fluid balance)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4 as _uuid4

logger = logging.getLogger(__name__)

_TRACKER_MODEL    = "gemini-2.5-flash"
_MAX_TOOL_ROUNDS  = 10
_THINKING_BUDGET  = 8000
_ALERT_COOLDOWN_H      = 8   # minimum hours between repeat alerts for same problem
_VITAL_STALENESS_HOURS      = 8   # vitals older than this → hard block on vital alerts
_VITAL_CARD_WARNING_HOURS   = 2   # vitals older than this → soft warning in prefetch + card marker
_LAB_STALENESS_HOURS        = 24  # labs older than this are considered stale for alert purposes
_CITED_NOTE_STALENESS_HOURS = 48  # if ALL cited notes are older than this, suppress — chronic audit issue not urgent alert
_LAB_UPLOAD_LAG_WARN_H     = 1   # labs uploaded >1h after reportedAt → show lag notice on card

# ── next_check interval config ────────────────────────────────────────────────
# Base intervals (hours) before backoff is applied.
# "lab_fast"    → labs that turn around quickly and need tight follow-up
# "lab_default" → all other labs
_BASE_INTERVAL_H: dict[str, int] = {"vital": 1, "io": 1, "lab_fast": 6, "lab_default": 24}

_FAST_LAB_KEYS: frozenset[str] = frozenset({
    "hb", "hemoglobin", "haemoglobin",          # kept at 6h (transfusion buffer parity)
    "sodium", "na",
    "potassium", "k",
    "lactate", "lactic",
    "abg", "vbg", "blood gas",                   # ABG/VBG family
    "ph", "pco2", "paco2",
    "hco3", "bicarb", "base excess", "be",
})

# Exponential backoff: interval doubles each unacknowledged re-alert,
# stopping at whichever limit is hit first.
_BACKOFF_MAX_ATTEMPTS  = 4   # growth stops after this many attempts
_BACKOFF_CAP_H         = 24  # hard ceiling for lab problems
_VITAL_BACKOFF_CAP_H   = 2   # vital/IO problems: never more than 2h between alerts


def _base_interval_h(nc_type: str, nc_key: str) -> int:
    if nc_type == "lab":
        k = (nc_key or "").lower()
        return _BASE_INTERVAL_H["lab_fast"] if any(f in k for f in _FAST_LAB_KEYS) else _BASE_INTERVAL_H["lab_default"]
    return _BASE_INTERVAL_H.get(nc_type, 1)


def _backoff_interval_h(nc_type: str, nc_key: str, attempts: int) -> int:
    """
    Compute the decayed next_check / re-alert interval.
    Doubles each attempt; stops growing once attempts >= _BACKOFF_MAX_ATTEMPTS
    or the type-specific cap is reached (whichever comes first).

    Vital/IO problems cap at 2h so a critically ill patient is never more than
    2h from the next forced check, regardless of how many unacknowledged alerts.

    Examples (vital, base=1h): 0→1h, 1→2h, 2→2h (cap), 3→2h (cap)
    Examples (fast-lab, base=6h): 0→6h, 1→12h, 2→24h (cap, stops)
    """
    base = _base_interval_h(nc_type, nc_key)
    clamped = min(attempts, _BACKOFF_MAX_ATTEMPTS)
    cap = _VITAL_BACKOFF_CAP_H if nc_type in ("vital", "io") else _BACKOFF_CAP_H
    return min(base * (2 ** clamped), cap)

def _compute_lab_upload_lag(delta: dict | None) -> dict | None:
    """Return {lag_hours, reported_ist, created_ist} for the lab with the largest upload lag, or None."""
    if not delta:
        return None
    labs = delta.get("new_labs") or []
    if not labs:
        return None
    from tools.radar_sync.delta_extractor import _parse_ts
    best = None
    for d in labs:
        reported = _parse_ts(d.get("reportedAt"))
        created  = _parse_ts(d.get("createdAt"))
        if reported and created and created > reported:
            lag_h = (created - reported).total_seconds() / 3600
            if lag_h > _LAB_UPLOAD_LAG_WARN_H and (best is None or lag_h > best[0]):
                best = (lag_h, reported, created)
    if best is None:
        return None
    _IST = timezone(timedelta(hours=5, minutes=30))
    lag_h, reported, created = best
    r_ist = reported.replace(tzinfo=timezone.utc).astimezone(_IST) if reported.tzinfo is None else reported.astimezone(_IST)
    c_ist = created.replace(tzinfo=timezone.utc).astimezone(_IST) if created.tzinfo is None else created.astimezone(_IST)
    return {
        "lag_hours": round(lag_h, 1),
        "reported_ist": r_ist.strftime("%-I:%M %p"),
        "created_ist":  c_ist.strftime("%-I:%M %p"),
    }


# ── Treatment response buffers ────────────────────────────────────────────────
# If a plan note is written within this window AFTER the triggering data,
# the intervention cannot yet have had time to take effect.
# being_addressed=True is enforced; treatment-inadequate override is blocked.

# Default buffer per next_check type (hours)
_RESPONSE_BUFFER_H: dict[str, int] = {"vital": 1, "lab": 4, "io": 1}

# Keyword → buffer override (hours), checked against problem name (lowercase).
# Listed longest-match-first so more specific keywords win.
_KEYWORD_BUFFER_H: list[tuple[str, int]] = [
    ("antimicrobial", 8),
    ("antibiotic",    8),
    ("transfus",      6),
    ("haemoglobin",   6),
    ("hemoglobin",    6),
    ("bicarbonate",   3),
    ("nahco3",        3),
    ("acidosis",      2),
    ("alkalosis",     2),
    ("respiratory",   2),
    ("ventilat",      2),
    ("vent",          2),
    ("abg",           2),
    ("vasopressor",   1),
    ("noradrenalin",  1),
    ("norepinephrin", 1),
    ("vasopressin",   1),
]

# ── Problem subsumption ontology ──────────────────────────────────────────────
# Parent → list of child problem name substrings it absorbs.
# All strings are lowercase for case-insensitive matching.
# When both parent and child would fire alerts for the same patient,
# the child is suppressed (post-hoc safety net — prompt rule is the primary path).
_PROBLEM_SUBSUMES: dict[str, list[str]] = {
    "septic shock":              ["refractory hypotension", "vasopressor dependency", "vasoplegia", "low map"],
    "refractory septic shock":   ["refractory hypotension", "vasopressor dependency", "vasoplegia", "low map"],
    "ards":                      ["hypoxemia", "refractory hypoxemia", "desaturation", "low spo2"],
    "acute respiratory failure": ["hypoxemia", "refractory hypoxemia", "desaturation"],
    "acute kidney injury":       ["oliguria", "anuria", "rising creatinine", "elevated creatinine"],
    "fluid overload":            ["pulmonary oedema", "pulmonary edema", "pleural effusion"],
    "diabetic ketoacidosis":     ["hyperglycemia", "metabolic acidosis"],
    "hepatic encephalopathy":    ["altered sensorium", "confusion"],
}

_SYSTEM = f"""You are a senior ICU clinician reviewing the current problem list for a patient.

You will receive:
1. The current structured_summary with problems and their clinical statuses
2. The stored problem state from the last assessment (if any)

For EACH problem you must decide three things: its clinical_status, whether it is
being_addressed, and whether to alert (should_alert). Work the ALERT DECISION LADDER
below from the top — the first rule that matches decides should_alert. The sections after
the ladder define the criteria each step relies on; the ladder defines the ORDER in which
they apply. When two rules seem to conflict, the higher ladder step wins.

════════════════════════════════════════════════════════════════════════
ALERT DECISION LADDER  —  walk top to bottom; first match wins
════════════════════════════════════════════════════════════════════════

STEP 0 — IS THIS PROBLEM ALERT-WORTHY AT ALL?  Branch by what the concern is DRIVEN BY,
because the bar is different for each. (Details under "STEP 0 — EVIDENCE-TYPE GATES".)
  • VITAL-driven  (hypotension, tachycardia, hypoxia, fever, bradycardia, etc.):
      alert-worthy ONLY if the CURRENT value crosses the vital floor AND the trend is flat
      or worsening. A drop from baseline that stays above the floor is NOT alert-worthy.
  • LAB-driven    (lactate, K+, creatinine, Hb, Na, pH, etc.):
      alert-worthy if a single clearly-abnormal value vs. the patient's baseline is present,
      the value is VERIFIED via get_lab_trend, and it is not stale (>24h). No vital floor is
      required — labs have their own criteria.
  • SYMPTOM-driven (pain, nausea, breathlessness, dizziness, etc.):
      alert-worthy ONLY with objective corroboration — a numeric score meeting a threshold,
      a vital correlate that itself crosses a floor, or a lab/imaging finding.
  • CARE-GAP / plan-discordance (conflicting orders, or notes giving incompatible plans —
      e.g. "soft diet" vs "NPO" after a tube removal):
      alert-worthy ONLY if you can cite ≥2 specific notes that contradict one another AND both
      orders are still active (neither superseded by a later order nor re-affirmed by a later
      note — see SUPERSESSION under the CARE-GAP / PLAN-DISCORDANCE RULE). No numeric threshold,
      and this applies only to plan/process problems — not numeric ones.
  If the problem fails the gate for its type → set clinical_status to stable and
  should_alert=False. Do not walk further down the ladder for it.

STEP 1 — PERMISSIVE CONTEXT GATE active for this problem?  (see "CONTEXT GATE")
  → should_alert=False. HIGHEST precedence — overrides every step below, including critical.

STEP 2 — TEAM ACTIVELY RESPONDING RIGHT NOW?  (ACLS/CPR in progress, emergency intubation
  underway, vasopressors being titrated this moment) → being_addressed=True, should_alert=False.
  You do NOT alert a clinician about something they are physically doing. The pipeline adds
  value by catching GAPS in care — not by narrating events already in progress.

STEP 3 — MOST RECENT PLAN NOTE < 24h OLD?  (see "NOTE FRESHNESS") → being_addressed=True,
  should_alert=False; do NOT apply the treatment-inadequate override (Step 5).
  EXCEPTION — VITALS: this step does NOT apply to vital-based problems (hypoxia, hypotension,
  tachycardia, tachypnoea, bradycardia, fever, etc.). If a vital is currently below its alert
  floor, a plan note does NOT suppress the alert — evaluate on the response buffer (Step 4).
  EXCEPTION — CARE-GAP: this step does NOT apply to CARE-GAP / plan-discordance problems. A
  fresh note that CONTRADICTS another fresh note is exactly what to alert on — fresh ≠ coherent.

STEP 4 — PLAN NOTE WITHIN THE RESPONSE-BUFFER WINDOW for its intervention type?  (see
  "TIMING RULE") → being_addressed=True, should_alert=False; set a next_check to monitor the
  expected response instead of alerting.

STEP 5 — MOST RECENT PLAN NOTE > 24h OLD **and** the problem still worsening despite adequate
  time to respond? → being_addressed=False, should_alert=True,
  alert_reason="Treatment inadequate — [details]".

STEP 6 — NO DOCUMENTED PLAN exists for this problem? → should_alert=True.

Across every step: NEVER alert when clinical_status is stable, improving, or resolved — not
even if a next_check is overdue. An overdue check on a non-worsening problem just resets the
window and continues monitoring silently. Never alert on a next_check that is not yet overdue.
And do NOT alert merely because a problem is worsening/critical and has a plan — trust the plan
unless Step 5 fires.

HYPERGLYCEMIA EXCEPTION — glucose-driven problems (Hyperglycemia, Uncontrolled glucose, Diabetes
with poor control, etc.) always require an insulin dose decision at the time the glucose result
lands. For these problems: skip Steps 3 and 4 entirely (note freshness and response buffer do
NOT apply). Always set should_alert=True whenever the glucose is above the hyperglycemia
threshold, regardless of whether a plan note exists or how recently it was written. The insulin
recommendation tool will populate the dose suggestion — your job is to ensure the alert fires
so the clinician receives the recommendation card.

════════════════════════════════════════════════════════════════════════
STEP 0 — EVIDENCE-TYPE GATES  (criteria for "alert-worthy?")
════════════════════════════════════════════════════════════════════════

VITAL SIGN ALERT FLOORS — for hemodynamic and respiratory problems, do NOT set
clinical_status="worsening"/"critical" and do NOT alert unless the CURRENT value (most recent
reading) crosses the relevant floor:
  • Hypotension / low MAP:  MAP < 65 mmHg  OR  systolic BP < 90 mmHg
  • Tachycardia:            HR > 100 bpm
  • Bradycardia:            HR < 40 bpm
  • Hypertension:           systolic BP > 180 mmHg
  • Hypoxia / low SpO2:     SpO2 < 92% AND SF ratio < 350  (BOTH required — SpO2 alone is not
                            enough; a drop from 99% to 96% is never sufficient regardless of SF)
  • Tachypnoea:             RR > 28 breaths/min AND SF ratio < 350  (BOTH required)
  • Fever:                  Temp > 38.5 °C
  NOTE — SF ratio < 350 is a hard co-requirement for ALL respiratory alerts (hypoxia and
  tachypnoea). get_vital_trend('SpO2') returns the SF ratio alongside each SpO2 reading — use
  the most recent SF value. If SF ratio is ≥ 350, do NOT alert even if SpO2 or RR cross their
  individual floors.
  PERSISTENCE OVERRIDE — the SF ratio gate is bypassed if SpO2 < 92% in 2 or more consecutive
  readings in get_vital_trend('SpO2'). Persistent desaturation across multiple readings cannot
  be explained by a transient FiO2 charting artefact; alert regardless of the SF ratio value.
  Check the last 3–4 readings: if ≥ 2 back-to-back readings all have SpO2 < 92%, treat the SF
  gate as satisfied and apply the normal alert/trend logic from that point.

MULTI-VITAL CRISIS OVERRIDE — if TWO or more distinct vital parameters are SIMULTANEOUSLY
below their alert floors at the current snapshot (e.g. SpO2 < 92% AND MAP < 65 mmHg, or
HR > 100 AND RR > 28), all suppression rules are bypassed: the SF ratio gate, note freshness
(Step 3), response buffer (Step 4), and improving-trend suppression do NOT apply. Multiple
simultaneous vital abnormalities signal acute physiological deterioration — alert regardless of
documented plans or recent notes. Set should_alert=True.
The permissive context gate (Step 1) and team-actively-responding (Step 2) still apply — a
permissive context that covers ALL the crossed floors still suppresses the alert, and an
emergency procedure already underway (CPR, active intubation) still takes precedence.

A drop from the patient's baseline is NOT sufficient on its own — the absolute value must cross
the floor. If the current value is above the floor (e.g. MAP 67 after a transient dip to 64),
classify as stable or improving — do NOT alert.

TREND DIRECTION — even if the current value is still below the floor, do NOT alert if the vital
is clearly recovering (most recent reading better than the prior and trending toward normal);
classify as improving and set a next_check to confirm recovery. Only alert if the vital is below
the floor AND the trend is flat or worsening. Examples:
  • SpO2 86% → 91%: trending up, do NOT alert — set next_check SpO2
  • SpO2 86% → 88% → 87%: flat/worsening below floor — alert
  • MAP 58 → 63 → 66: recovering through floor — do NOT alert
  • MAP 58 → 60 → 59: flat below floor — alert

VITAL TREND WINDOW — when assessing whether a vital is worsening or improving, only compare
readings within the last 6–8 hours. A change observed over days does NOT constitute acute
worsening. If the vital has been flat/stable within the last 6–8 hours, classify as stable
regardless of how different it looks vs. a value from 2–3 days ago. Only compare across longer
windows for an explicit chronic trend — and even then do not alert on it.

LAB criteria — a SINGLE clearly-abnormal lab result vs. the patient's baseline IS sufficient to
call worsening (labs are drawn infrequently, so you rarely have two panels to compare). But:
  • LAB VALUE ACCURACY — do NOT state a specific numeric lab value in alert_title or alert_reason
    unless you have called get_lab_trend and that exact value appears in the result. If you have
    not yet called get_lab_trend for this lab, call it before setting should_alert=True. If
    get_lab_trend returns no result within the past 24 hours, do NOT assert a current value —
    state the last known value and its date instead:
      CORRECT: "last known Glucose 166 mg/dL (16 Jun 11:40) — no recent result available"
      WRONG:   "Glucose 20 mg/dL — no insulin plan"   ← fabricated value; will be suppressed
  • LAB STALENESS — do NOT set should_alert=True for any lab-based problem if the most recent lab
    result driving the alert is more than 24 hours before the snapshot time. A stale lab cannot
    reflect the patient's current state. If the most recent result is >24h old, should_alert=False
    and state in addressed_evidence: "Most recent [lab] result is from [date] — >24h old; not
    alerting on stale data."

CARE-GAP / PLAN-DISCORDANCE RULE — for problems whose evidence is a documented CONTRADICTION in
the care plan rather than a number (conflicting orders, or two notes giving incompatible plans —
e.g. "soft diet" vs "NPO" after a tube/line/device action): alert ONLY if you can cite ≥2 specific
notes that contradict one another. State the cited notes explicitly in alert_reason — the clinician
must see the conflict itself, not just your conclusion.
GUARDRAILS:
  • This gate applies ONLY to problems that are fundamentally a plan/process concern. Do NOT scan
    numeric vital- or lab-driven problems for contradictions.
  • SUPERSESSION — for single-valued states (diet status, ventilation mode, vasopressor on/off,
    code status), a later order or note REPLACES an earlier one; it does not conflict with it.
    Before flagging a conflict, confirm BOTH orders are still active — i.e. neither has been
    superseded by a later order for the same state NOR re-affirmed by a later note. NPO → soft
    diet → continued NPO is a resolved sequence (current state = NPO), NOT a live conflict. A
    stale order that later events overtook is at most a housekeeping cleanup — never a clinical
    safety alert. Order the candidate events by timestamp and read the LATEST one as the current
    state before concluding anything.
  • Use the prefetched notes already in context ([0]-[N]). Do NOT issue additional
    query_patient_notes calls to hunt for contradictions unless a specific discordance is already
    visible in the prefetch.
Because the trigger is precisely that fresh notes disagree, the NOTE FRESHNESS suppression
(Step 3) does NOT apply to this problem type.

════════════════════════════════════════════════════════════════════════
TOOL USAGE  (how to read the data behind the gates)
════════════════════════════════════════════════════════════════════════
- For each overdue next_check: use get_vital_trend or get_lab_trend to check whether the expected
  result has arrived.
- For vital-sign-dependent problems (Fever, Tachycardia, Hypertension, Hypotension, etc.): if
  get_vital_trend returns "Unknown vital" or "No … readings found", do NOT conclude worsening
  based on notes alone — mark as stable with addressed_evidence="vital data unavailable in
  snapshots — cannot confirm worsening" and should_alert=False.
- If your tool data (get_io, get_vital_trend, get_lab_trend) directly contradicts the structured
  summary's clinical_status or current_state description, TRUST YOUR TOOL DATA and override the
  summary. Examples:
  • Summary says "anuric" but get_io shows average UO > 50 ml/hr → do NOT treat as anuria;
    reassess clinical_status as stable or improving based on the actual I/O numbers.
  • Summary says "worsening" but vitals/labs are trending toward normal → mark as stable/improving.
  • Summary says "critical" but MAP is stable and vasopressors are off → downgrade.
  In these cases set should_alert=False and explain the discrepancy in addressed_evidence. Do NOT
  fire an alert based on a summary label that your own tool evidence directly disproves.
- Whenever you call query_patient_notes, results are prefixed with [0], [1], [2]... These indices
  are GLOBAL — they accumulate across all query_patient_notes calls in this session. Reuse them
  when populating cited_note_indices (see OUTPUT CONTRACT).

════════════════════════════════════════════════════════════════════════
DETERMINING being_addressed AND CLINICAL STATUS
════════════════════════════════════════════════════════════════════════
- "Being addressed" = a documented plan exists, even if the result is not yet visible (e.g.
  transfusion ongoing). It means a plan exists — not necessarily that it is working yet. Query
  notes to find documentation of a plan or treatment.
- A documented INFORMED REFUSAL is a management plan: if notes document that an intervention was
  considered but declined by the patient or family ("dialysis declined by attendant", "family
  refusing surgery", "patient not consenting to intubation"), set being_addressed=True and
  should_alert=False. The clinical team has evaluated the situation and the decision has been
  made. Do not repeatedly alert for a problem where the only unaddressed element is an
  intervention the patient/family has explicitly declined.
- NOTE FRESHNESS — applies to LAB-BASED problems only. A plan note written within the last
  24 hours is a current, active plan for lab-driven problems. If the most recent plan note for
  a lab problem is < 24h old, set being_addressed=True and should_alert=False — regardless of
  whether the response buffer has expired, whether the problem is still worsening, and regardless
  of the treatment-inadequate override. Clinicians write notes at most once per day; a note from
  earlier the same day is still the active plan for a lab-level concern. Only apply the
  treatment-inadequate override when the most recent plan note is > 24h old AND the problem is
  worsening.
  NOTE FRESHNESS does NOT apply to vital-based problems (hypoxia, hypotension, tachycardia,
  tachypnoea, bradycardia, fever, etc.). Vitals are real-time; a plan note written hours ago does
  not mean the current vital abnormality is being actively managed at this moment. For vital
  problems, use the response buffer (Step 4) only — not note freshness.
  (Does NOT apply to CARE-GAP problems — see Step 3 of the ladder.)
- RESOLVED handling:
  • If get_problem_state shows a prior status of "resolved" but the current summary marks it as
    worsening/critical, treat the stored addressed_evidence as STALE — the old plan was for a
    prior episode. Query notes fresh to check whether a new plan exists for the current episode.
  • If the structured_summary marks a problem "resolved": you may keep it resolved, or downgrade
    to stable if you see lingering concerns. You may NOT upgrade to worsening/critical unless you
    have OBJECTIVE data (vital trend or lab value) showing clear deterioration — notes mentioning
    past treatment or monitoring are NOT sufficient to override "resolved". If the vital trend
    shows normal values (HR normal for tachycardia, BP normal for hypertension, normal labs), keep
    the problem resolved or stable and do NOT alert.

════════════════════════════════════════════════════════════════════════
SETTING next_check  (worsening / critical problems only)
════════════════════════════════════════════════════════════════════════
- ONLY set next_check for "worsening" or "critical" problems. Do NOT set it for stable, improving,
  or resolved problems — omit the field entirely.
- When setting next_check, specify:
  - "what": the specific thing to look for (e.g. "Hb post-transfusion", "HR on Cardizem", "urine output")
  - "type": "vital", "lab", or "io"
      • "vital" — heart rate, blood pressure, SpO2, MAP, RR, Temp (checked every 1h).
          Do NOT set vital_key="FiO2" — FiO2 is a ventilator setting, not a patient parameter.
      • "lab"   — any blood test result (checked every 6h)
      • "io"    — urine output or fluid balance from the I/O chart (checked every 1h)
  - "fulfilled": true if the expected result from the PREVIOUS next_check is already present in the
    chart; false if it has not arrived yet. The system uses "fulfilled" to manage the check window
    — do not skip it. (due_after resets automatically when fulfilled=true: vital=+1h, lab=+6h, io=+1h)
- For SUBJECTIVE SYMPTOM problems: the next_check MUST be the objective vital that corroborates the
  alert, not an unrelated parameter. If HR is the objective correlate, set vital_key="HR". If there
  is no objective vital correlate (e.g. the problem is corroborated only by a lab or imaging
  finding), omit next_check entirely — do NOT default to GCS or any other unrelated vital.

════════════════════════════════════════════════════════════════════════
CONTEXT GATE — PERMISSIVE WINDOW MONITORING  (Step 1 detail)
════════════════════════════════════════════════════════════════════════
Some problems have a monitoring protocol that defines a *permissive window* — a bounded period
where an abnormal value is clinically INTENDED. When the == CONTEXT GATE == block is present in
the user message, you MUST populate the context_gate field for each listed problem.

Gate verdict rules:
- permissive_active:    value is inside the band AND no invalidate_if trigger fired AND valid_until not passed
                        → set should_alert=False for this problem (suppress alert silently)
- permissive_breached:  value exceeds the permissive band BUT eligibility is still valid
                        → set should_alert=True (alert fires) — eligibility is NOT ended; it auto-resumes
- permissive_ended:     an invalidate_if trigger fired OR valid_until passed
                        → set should_alert=True; after this point the gate is closed permanently
- no_permissive_context: no scenario applies to this patient → normal alert rules apply

Carry-forward: if the stored gate says eligibility=active and nothing has changed, confirm in one
sentence and carry it forward. Do NOT re-derive unnecessarily. Only call tools if a trigger is
ambiguous or you need to verify the current value against the band.

If a gate verdict is permissive_active, override should_alert=False regardless of what the
problem's clinical_status or other rules suggest — the permissive window takes precedence. If the
gate verdict is permissive_breached or permissive_ended, apply normal alert rules (the gate does
not suppress in those cases).

════════════════════════════════════════════════════════════════════════
TIMING RULE — Treatment response buffer  (Step 4 detail)
════════════════════════════════════════════════════════════════════════
When a worsening or critical problem has a plan note documented within the buffer window of the
triggering data, the intervention has not had adequate time to show effect. In this case you MUST:
  • Set being_addressed = True
  • Do NOT apply the treatment-inadequate override (Step 5)
  • Do NOT alert — set a next_check to monitor the expected response instead

Response buffer by intervention / problem type:
  • Ventilator adjustment / ABG / pH / acidosis / alkalosis / respiratory: 2 hours
  • Bicarbonate infusion: 3 hours
  • General lab (default): 4 hours
  • Blood transfusion / haemoglobin: 6 hours
  • Antibiotics / antimicrobials: 8 hours
  • Vasopressor titration / hemodynamic vitals: 1 hour
  • Urine output / fluid balance (IO): 1 hour

If the pre-fetched TIMING CONTEXT section flags a problem with a DIRECTIVE, honour it
unconditionally — it has already done the timestamp arithmetic for you.

════════════════════════════════════════════════════════════════════════
CAUSAL / SECONDARY PROBLEMS
════════════════════════════════════════════════════════════════════════
The user message may include:
  1. A CO-EXISTING PROBLEM STATUSES block listing all tracked problems with their current
     clinical_status and being_addressed flags.
  2. A "cause" annotation on a problem — e.g. "AKI (secondary to: Septic Shock)".
When a problem carries a "cause", the CATEGORY-SPECIFIC RULES section below contains the
secondary-problem reasoning to apply.

════════════════════════════════════════════════════════════════════════
PROBLEM CONSOLIDATION  (before finalising the assessment list)
════════════════════════════════════════════════════════════════════════
Check each pair of alerting problems for clinical redundancy:
  • If two problems are synonymous (different names for the same condition), keep the more
    specific / more severe one and suppress the other (should_alert=False).
  • If one problem is a direct physiological criterion or consequence of another, do NOT alert them
    separately. Merge the subsidiary finding's key data into the primary problem's alert_reason and
    set should_alert=False for the subsidiary.
  Common examples (not exhaustive):
    • Refractory Hypotension + Refractory Septic Shock → alert only Septic Shock; include
      MAP/vasopressor data in the Septic Shock alert_reason
    • Oliguria + AKI → alert only AKI; include urine output numbers in AKI alert_reason
    • Hypoxemia + ARDS → alert only ARDS
    • Pulmonary Oedema + Fluid Overload → alert only Fluid Overload
    • Vasopressor Dependency + Septic Shock → alert only Septic Shock
  Rule: if problem B would not exist as an independent clinical concern without problem A, do not
  fire two separate alerts.

════════════════════════════════════════════════════════════════════════
OUTPUT CONTRACT  (how to write each assessment)
════════════════════════════════════════════════════════════════════════
- alert_title — name the SPECIFIC concern in ≤10 words, not the problem category. It is the first
  thing a clinician reads on the alert card; make it immediately actionable. When the alert is
  driven by a specific numeric vital or lab value, include the IST timestamp of that reading in
  parentheses so the clinician knows exactly when it was measured.
    Bad:  "Post-operative monitoring", "Hyponatremia", "Shock"
    Good: "Note contradicts stable haemodynamics", "Na=110 — no correction plan",
          "HR 160 (04:12 IST) uncontrolled — no rate plan", "Lactate 20 — pH discordance",
          "SpO2 90% (05:22 IST) — no oxygen plan", "Cr rising — no nephrology plan"
- alert_reason — always state: (1) the patient's baseline value, (2) the current value, (3) the
  direction of the recent trend. e.g. "BP rose from baseline 128/80 to a peak of 171/90; currently
  157/90 — trending down from peak but still well above baseline. No updated plan." Never phrase
  the reason so it only compares peak vs current, as that sounds like improvement.
- When alerting on a SUBJECTIVE SYMPTOM you MUST explicitly state the objective corroborating
  finding in alert_reason — do not leave it implied. The clinician reading the card has no context;
  they need to see both the symptom AND the objective finding that supports it. Example: "Patient
  reports refractory abdominal pain (NRS 8/10); HR 160 bpm provides objective corroboration. No
  revised analgesic plan documented."
- note_vs_objective — when should_alert=True and your evidence includes a vital or lab value cited
  from a clinical note, cross-reference it against the objective trend already provided in the
  prefetch (Section 2). Do NOT make additional tool calls for this — the prefetch data is already
  in context. If the note-cited value differs significantly from the objective trend, populate
  note_vs_objective with a single sentence comparing both (e.g. "Note [4] documents HR 16 bpm; last
  verified vital trend shows HR 72-78 bpm — likely a documentation error"). If there is no
  significant discordance, leave it empty. Check only the single vital/lab most relevant to the
  alert — not every value mentioned in the note. GCS SPECIFICS — GCS is recorded both in clinical
  notes (free text) and in the verified vitals flowsheet. If your alert evidence includes a GCS
  value from a note, always check the prefetch vital trend for GCS and compare; set
  next_check.type="vital" with vital_key="GCS" (not type="io") so the follow-up fetches the
  flowsheet reading. If the note GCS differs from the flowsheet GCS, populate note_vs_objective.
- cited_note_indices — populate with every [N] index you relied on when writing addressed_evidence
  or alert_reason. This creates an auditable citation trail so clinicians can see exactly which note
  the reasoning came from. Example: if note [2] said "RRT initiated" and you used it as evidence,
  set cited_note_indices: [2]. If a note contradicted the summary, still cite it.
- reasoning_fingerprint — 3–5 sentences in first person covering what data you checked, what you
  found, your alert decision (alerted / suppressed — reason / no alert — stable), and what specific
  data would change this assessment next run. Do NOT nest it — it is a plain string.
- TEMPORAL AWARENESS — evaluate notes relative to the snapshot time (shown at the top of the user
  message). A note written on Day X that says "patient experienced episodes today" refers to events
  on Day X, not the current assessment date.
    • Always check the timestamp of each cited note against the snapshot time. If the most recent
      note about a problem is > 8 hours before the snapshot time, state: "No fresh documentation for
      this assessment window."
    • Do NOT treat prior-day "today" language as evidence of current activity. A Jun 12 note saying
      "two episodes today" means two episodes on Jun 12 — not Jun 13.
    • If all available notes about a problem are from a prior calendar day AND objective vitals/labs
      are stable or improving, classify as stable — do not alert.
    • Copy-pasted summaries (same text under multiple authors or timestamps) count as ONE piece of
      evidence, not independent corroboration. Do not amplify confidence because the same event is
      described in several notes.
- CLINICAL TIMELINE — when a == CLINICAL TIMELINE == block appears in the user message, it
  represents the ordered history of this patient as previously recorded. Use it to establish
  temporal context before assessing any problem.
    • The most recent event in the timeline is the current clinical reality for that problem.
      A newer event that describes a different state than an older event (diet advanced from NPO to
      oral, ventilator weaned, vasopressors stopped) is CLINICAL PROGRESSION — not a contradiction.
      Do NOT flag progression as a care-gap.
    • SINGLE-VALUED STATES — some clinical states hold exactly ONE value at a time: diet status
      (NPO / soft / oral / …), ventilation mode, vasopressor on/off, code status. For these, the
      LATEST event of that kind IS the active state and every earlier event is SUPERSEDED, not
      coexisting. NPO → soft diet → NPO is a sequence of state transitions, not three conflicting
      orders. Only flag a contradiction when two events for the SAME single-valued state are the
      latest of their kind AND no later event re-affirms one of them. If any later note or order
      re-affirms one value, THAT value is current and there is NO conflict.
    • Only flag a contradiction when two events from the same care period describe incompatible
      active orders that are BOTH still current (neither superseded by a later event), OR when the
      most recent note itself explicitly describes an unresolved conflict.
    • Timeline events are NEUTRAL FACTS — record what was ordered or observed, never your
      interpretation of it. Write "Soft diet ordered", NEVER "Soft diet ordered (conflicting with
      NPO)". Baking a conflict/progression verdict into an event poisons every future run, which
      re-reads your conclusion as recorded truth and re-fires the same alert. Judge conflict vs.
      progression at READ time from the neutral events — do not persist the judgment.
    • In set_all_assessments, always emit timeline_updates with new events from this run. Each
      event MUST include a `source` citing the note/lab/vital it came from. If the timeline is
      empty or absent, seed it by recording the key events implied by the current summary.
    • Emit `prior_course` ONLY when the user message contains a "TIMELINE FOLD REQUIRED" block.
- Call set_all_assessments ONCE after reviewing all problems."""


# ── Tool definitions ──────────────────────────────────────────────────────────

_VITAL_TREND_TOOL = {
    "name": "get_vital_trend",
    "description": "Get the last N vital-sign readings from stored snapshots. Returns newest-first.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vital_name": {"type": "string", "description": "HR, BP, MAP, SpO2, RR, Temp, GCS (not FiO2 — use SpO2 for oxygenation)"},
            "n": {"type": "integer", "description": "Number of readings (default 6, max 20)"},
        },
        "required": ["vital_name"],
    },
}

_LAB_TREND_TOOL = {
    "name": "get_lab_trend",
    "description": "Get recent values for a lab parameter across stored snapshots. Partial name match OK.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "e.g. Hb, Creatinine, Potassium, Glucose, Lactate"},
            "n": {"type": "integer", "description": "Number of values (default 6, max 12)"},
        },
        "required": ["lab_name"],
    },
}

_NOTES_TOOL = {
    "name": "query_patient_notes",
    "description": (
        "Semantic search over all clinician notes. Use to find documented plans and treatments. "
        "Results are returned as numbered excerpts [0], [1], [2]... "
        "Indices are global across all query_patient_notes calls in this session — "
        "use them in cited_note_indices when calling set_all_assessments."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "e.g. 'anemia transfusion plan', 'Afib rate control'"},
        },
        "required": ["question"],
    },
}

_GET_PROBLEM_TOOL = {
    "name": "get_problem_state",
    "description": (
        "Fetch the stored state for a specific problem from the last assessment. "
        "Returns: being_addressed, addressed_evidence, next_check (what/type/due_after), last_alerted_at."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "problem_name": {"type": "string"},
        },
        "required": ["problem_name"],
    },
}

_SET_ALL_TOOL = {
    "name": "set_all_assessments",
    "description": "Call ONCE after reviewing all problems. Provide the full assessment for every problem.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "problem_name":       {"type": "string"},
                        "clinical_status":    {
                            "type": "string",
                            "enum": ["critical", "worsening", "stable", "improving", "resolved"],
                        },
                        "being_addressed":    {"type": "boolean"},
                        "addressed_evidence": {"type": "string", "description": "Quote from notes or 'No plan documented'"},
                        "should_alert":       {"type": "boolean"},
                        "alert_title":        {
                            "type": "string",
                            "description": (
                                "Required if should_alert=True. A concise ≤8-word phrase naming "
                                "the SPECIFIC concern — not the problem category. Shown as the "
                                "alert card header. Examples: 'Note contradicts stable haemodynamics', "
                                "'K=2.6 — no correction plan', 'Overdue ABG on CPAP', "
                                "'Lactate 20 — pH discordance', 'HR 160 uncontrolled — no rate plan'."
                            ),
                        },
                        "alert_reason":       {"type": "string", "description": "Required if should_alert=True"},
                        "note_vs_objective":  {
                            "type": "string",
                            "description": (
                                "Only populate when should_alert=True and your alert evidence includes "
                                "a vital or lab value cited from a clinical note. Compare that note-cited "
                                "value against the objective trend in the prefetch (no extra tool calls). "
                                "Write one sentence: e.g. 'Note [4] documents HR 16 bpm; verified vital "
                                "trend shows HR 72-78 bpm — likely a documentation error.' "
                                "Leave empty if there is no significant discordance."
                            ),
                        },
                        "suggestions":        {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific actionable suggestions if alerting",
                        },
                        "reasoning_fingerprint": {
                            "type": "string",
                            "description": (
                                "3-5 sentence first-person reasoning chain for the NEXT hourly run to "
                                "read instead of re-deriving from scratch. Cover: (1) what data you "
                                "checked (vitals/labs/notes), (2) what you found, (3) your alert "
                                "decision and why (alerted / suppressed — reason / no alert — stable), "
                                "(4) what specific data would change this assessment next run. "
                                "Example: 'I checked Cr trend (1.2→1.5→1.8 over 12h) and searched "
                                "notes — no nephrology plan found. Alert suppressed by 8h cooldown "
                                "expiring at 22:03. Would alert if Cr rises further or cooldown expires "
                                "with no new plan.'"
                            ),
                        },
                        "next_check": {
                            "type": "object",
                            "description": (
                                "Only set for worsening or critical problems. "
                                "Omit entirely for stable, improving, or resolved. "
                                "Set vital_key when type='vital', lab_name when type='lab', "
                                "neither when type='io'. Optionally set label for a human-readable "
                                "description shown in the alert (e.g. 'BP post-antihypertensive dose')."
                            ),
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["vital", "lab", "io"],
                                },
                                "vital_key": {
                                    "type": "string",
                                    "enum": ["HR", "BP", "MAP", "SpO2", "RR", "Temp", "GCS"],
                                    "description": (
                                        "Required when type='vital'. Pick exactly ONE vital — "
                                        "the most clinically relevant one for this problem."
                                    ),
                                },
                                "lab_name": {
                                    "type": "string",
                                    "description": (
                                        "Required when type='lab'. Single parameter name only "
                                        "(e.g. 'Hb', 'Creatinine', 'Sodium', 'Potassium'). "
                                        "Do not combine names or add context suffixes here — "
                                        "use label for that."
                                    ),
                                },
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "Optional human-readable description for the alert card "
                                        "(e.g. 'Hb after transfusion', 'BP post-antihypertensive'). "
                                        "If omitted, vital_key or lab_name is used."
                                    ),
                                },
                                "fulfilled": {
                                    "type": "boolean",
                                    "description": (
                                        "True if you found the expected result from the PREVIOUS next_check "
                                        "in the chart and are now setting a new follow-up target. "
                                        "False if the result has not yet arrived and you are repeating the same pending check."
                                    ),
                                },
                            },
                            "required": ["type", "fulfilled"],
                        },
                        "cited_note_indices": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Indices from query_patient_notes results (the [N] numbers) "
                                "that directly support your addressed_evidence or alert_reason. "
                                "Always populate this when you called query_patient_notes — "
                                "include every [N] you relied on as evidence."
                            ),
                        },
                        "context_gate": {
                            "type": "object",
                            "description": (
                                "ONLY populate for problems that have an active context-gate protocol "
                                "(listed in the == CONTEXT GATE == block). "
                                "Omit entirely for problems with no matching protocol, or where the "
                                "stored gate has eligibility='ended'."
                            ),
                            "properties": {
                                "verdict": {
                                    "type": "string",
                                    "enum": ["permissive_active", "permissive_breached", "permissive_ended", "no_permissive_context"],
                                    "description": (
                                        "permissive_active — in-band, eligibility intact, suppress alert. "
                                        "permissive_breached — out-of-band but eligibility preserved; alert fires, auto-resumes when corrected. "
                                        "permissive_ended — trigger fired or time expired; eligibility permanently ended. "
                                        "no_permissive_context — no permissive scenario applies; normal alert rules."
                                    ),
                                },
                                "scenario": {
                                    "type": "string",
                                    "description": "Name of the matched scenario (e.g. 'acute_ischaemic_stroke_no_tpa') or 'none'.",
                                },
                                "band_description": {
                                    "type": "string",
                                    "description": "Clinical English description of the permissive band (e.g. 'SBP ≤ 220 mmHg, DBP ≤ 120 mmHg').",
                                },
                                "valid_until_iso": {
                                    "type": "string",
                                    "description": (
                                        "ISO 8601 UTC datetime when the permissive window expires "
                                        "(e.g. '2026-06-17T14:00:00Z'). "
                                        "Compute from documented onset time + window duration. "
                                        "Omit or set null if there is no time cap (e.g. chronic renovascular, raised ICP)."
                                    ),
                                },
                                "rationale": {
                                    "type": "string",
                                    "description": (
                                        "First-person 2-4 sentence reasoning: which scenarios were ruled in/out and why, "
                                        "what evidence was found (note indices), and what would change this verdict."
                                    ),
                                },
                                "plan_if_permissive": {
                                    "type": "string",
                                    "description": "What should be done while the permissive window is active (e.g. 'Do not drop BP — maintain penumbral perfusion. Avoid antihypertensives unless SBP > 220.').",
                                },
                                "plan_when_ended": {
                                    "type": "string",
                                    "description": "What to do once the window closes or a trigger fires (e.g. 'Taper to SBP < 140 over 24–48h at ~15%/day. Restart home antihypertensives when swallowing safely.').",
                                },
                                "invalidate_if": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Carry forward the invalidate_if list from the matched scenario.",
                                },
                                "gate_cited_note_indices": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "Note indices used to derive this gate verdict.",
                                },
                            },
                            "required": ["verdict", "scenario", "rationale"],
                        },
                    },
                    "required": [
                        "problem_name", "clinical_status", "being_addressed",
                        "addressed_evidence", "should_alert", "reasoning_fingerprint",
                    ],
                },
            },
            "timeline_updates": {
                "type": "array",
                "description": (
                    "New clinical events from this run to append to the patient timeline. "
                    "Include any clinically meaningful changes: new orders, findings, "
                    "interventions, status changes. Each event MUST have a `source` "
                    "citing the note/lab/vital. Event text must be a NEUTRAL FACT free of "
                    "interpretation (see the `event` field). Omit events already in the "
                    "== CLINICAL TIMELINE == block."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "t": {
                            "type": "string",
                            "description": "ISO 8601 UTC timestamp (e.g. '2026-06-25T09:20:00Z').",
                        },
                        "event": {
                            "type": "string",
                            "description": (
                                "One concise sentence stating the NEUTRAL FACT of what was "
                                "ordered, observed, or done — never your interpretation of it. "
                                "Write 'Soft diet ordered', NEVER 'Soft diet ordered "
                                "(conflicting with NPO)'. The timeline is a factual ledger; "
                                "conflict/progression judgments are made at read time, not "
                                "baked into the event text."
                            ),
                        },
                        "problem": {
                            "type": "string",
                            "description": "Problem name or 'general' for cross-cutting events.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["order", "finding", "intervention", "status_change"],
                        },
                        "source": {
                            "type": "string",
                            "description": (
                                "Format: 'note@<ISO-timestamp>', 'lab@<name>:<value>', "
                                "or 'vital@<name>:<value>'. Required."
                            ),
                        },
                    },
                    "required": ["t", "event", "problem", "kind", "source"],
                },
            },
            "prior_course": {
                "type": "string",
                "description": (
                    "ONLY emit when the user message contains a 'TIMELINE FOLD REQUIRED' block. "
                    "Preserve existing prior_course text verbatim, then append the aged-out "
                    f"events as brief clauses. Keep under 1500 chars."
                ),
            },
        },
        "required": ["assessments"],
    },
}

_IO_TOOL = {
    "name": "get_io",
    "description": (
        "Get the per-hour fluid balance table from the I/O chart (chart.io). "
        "Returns intake (IV meds, feeds, other) and output (urine, drains, dialysis, other) "
        "per hour, net balance, and average urine output. "
        "Use for AKI, oliguria, anuria, fluid overload, positive fluid balance, drain output."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "n_hours": {"type": "integer", "description": "Number of recent hours to return (default 12, max 48)"},
        },
        "required": [],
    },
}

_TOOLS = [_VITAL_TREND_TOOL, _LAB_TREND_TOOL, _IO_TOOL, _NOTES_TOOL, _GET_PROBLEM_TOOL, _SET_ALL_TOOL]


# ── Tool implementations ───────────────────────────────────────────────────────

def _get_problem_state(cpmrn: str, encounter: int, problem_name: str, db: Any) -> str:
    doc = db["patient_problems"].find_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        {"being_addressed": 1, "addressed_evidence": 1, "next_check": 1, "last_alerted_at": 1},
    )
    if not doc:
        return f"No prior state for '{problem_name}' — this is a new problem."

    nc = doc.get("next_check") or {}
    due = nc.get("due_after")
    # Normalise naive datetimes from MongoDB to UTC-aware before comparing
    if isinstance(due, datetime) and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    overdue = isinstance(due, datetime) and datetime.now(timezone.utc) > due

    last_alert = doc.get("last_alerted_at")
    last_alert_str = last_alert.strftime("%Y-%m-%d %H:%M UTC") if isinstance(last_alert, datetime) else "never"

    # nc_key: machine fetch argument; nc_label: human display text
    # Support both new schema (key/label) and old schema (what) for backward compat
    nc_key   = nc.get("key") or nc.get("what", "")
    nc_label = nc.get("label") or nc_key
    nc_type  = nc.get("type", "")

    if nc_key or nc_type == "io":
        due_str = due.strftime("%Y-%m-%d %H:%M UTC") if isinstance(due, datetime) else str(due or "not set")
        display = nc_label if nc_label else nc_type
        nc_line = (
            f"  Next check: {display} ({nc_type}) — due {due_str}"
            f"{' [OVERDUE]' if overdue else ' [pending]'}\n"
            f"  Reminder: set next_check.fulfilled=true only if you find the above result "
            f"in the chart; false if it has not arrived yet."
        )
    else:
        nc_line = "  Next check: none (problem was stable/resolved at last assessment)"
        overdue = False  # no window → nothing is overdue

    result = (
        f"Problem: {problem_name}\n"
        f"  Being addressed: {doc.get('being_addressed', False)}\n"
        f"  Evidence: {doc.get('addressed_evidence', 'none')}\n"
        f"{nc_line}\n"
        f"  Last alerted: {last_alert_str}"
    )

    # Always auto-fetch the last 3 readings for the next_check item so the
    # model sees current data inline and cannot rely solely on stale notes.
    # When overdue, fetch 6 readings for fuller context.
    if (nc_key or nc_type == "io") and nc_type:
        try:
            from tools.radar_sync.status_classifier import _get_vital_trend, _get_lab_trend, _get_io
            n = 6 if overdue else 3
            if nc_type == "vital":
                trend = _get_vital_trend(cpmrn, encounter, nc_key, n)
            elif nc_type == "io":
                trend = _get_io(cpmrn, encounter, n_hours=12 if overdue else 6)
            else:
                trend = _get_lab_trend(cpmrn, encounter, nc_key, n)
            prefix = "⚠ OVERDUE" if overdue else "Current"
            result += f"\n  {prefix} — {nc_label} ({nc_type}) (auto-fetched):\n"
            for line in trend.splitlines():
                result += f"    {line}\n"
        except Exception:
            logger.exception("_get_problem_state: auto-trend fetch failed for %s / %s", problem_name, nc_key)

    # Auto-embed urine output for AKI / fluid / oliguria / anuria problems
    # so the model cannot rely solely on stale structured_summary text.
    _IO_KEYWORDS = ("aki", "acute kidney", "oliguria", "anuria", "renal failure",
                    "fluid", "urine output", "fluid overload", "fluid balance",
                    "drain", "dialysis")
    if any(kw in problem_name.lower() for kw in _IO_KEYWORDS):
        try:
            from tools.radar_sync.status_classifier import _get_io
            io_text = _get_io(cpmrn, encounter, 12)
            result += f"\n  Auto-fetched I/O balance (last 12 hr):\n"
            for line in io_text.splitlines():
                result += f"    {line}\n"
        except Exception:
            logger.exception("_get_problem_state: auto I/O fetch failed for %s", problem_name)

    return result


def _run_tool(
    name: str,
    args: dict,
    cpmrn: str,
    encounter: int,
    db: Any,
    session_chunks: list,
) -> str:
    try:
        if name == "get_vital_trend":
            from tools.radar_sync.status_classifier import _get_vital_trend
            return _get_vital_trend(cpmrn, encounter, args["vital_name"], int(args.get("n", 6)))
        if name == "get_lab_trend":
            from tools.radar_sync.status_classifier import _get_lab_trend
            return _get_lab_trend(cpmrn, encounter, args["lab_name"], int(args.get("n", 6)))
        if name == "query_patient_notes":
            from tools.radar_sync.query_notes import query_patient_notes_with_chunks
            start_idx = len(session_chunks)
            text, new_chunks = query_patient_notes_with_chunks(
                cpmrn, encounter, args["question"], start_index=start_idx
            )
            session_chunks.extend(new_chunks)
            return text
        if name == "get_io":
            from tools.radar_sync.status_classifier import _get_io
            return _get_io(cpmrn, encounter, int(args.get("n_hours", 12)))
        if name == "get_problem_state":
            return _get_problem_state(cpmrn, encounter, args["problem_name"], db)
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception("problem_tracker: tool %s failed", name)
        return f"Tool error: {e}"


# ── Alert eligibility check ────────────────────────────────────────────────────

def _all_cited_notes_stale(assessment: dict, snapshot_at: datetime) -> bool:
    """
    Return True if the assessment cited notes AND every cited note is older than
    _CITED_NOTE_STALENESS_HOURS before snapshot_at.
    Returns False when no notes were cited (alert based on objective vitals/labs — don't suppress).
    """
    cited = assessment.get("cited_notes") or []
    if not cited:
        return False

    cutoff = snapshot_at - timedelta(hours=_CITED_NOTE_STALENESS_HOURS)
    for note in cited:
        ts = note.get("timestamp")
        if ts is None:
            continue
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts > cutoff:
            return False  # at least one fresh note — allow alert
    return True


def _extract_alert_lab_value(alert_title: str) -> tuple[str, float] | None:
    """
    Extract (lab_name, value) from an alert title if a specific numeric lab value is present.
    Handles:  "Glucose 20 mg/dL — ...",  "Na=110 — ...",  "K 6.8 — ..."
    Returns None if no parseable lab+number pattern is found.
    """
    import re as _re
    title = alert_title.strip()
    # "Na=110", "Glucose=20.5"
    m = _re.match(r'^([A-Za-z][A-Za-z0-9]{0,14})\s*=\s*([\d.]+)', title)
    if m:
        return m.group(1), float(m.group(2))
    # "Glucose 20 mg/dL — ...", "Lactate 20.5 —"
    m = _re.match(
        r'^([A-Za-z][A-Za-z0-9\s]{0,14}?)\s+([\d.]+)\s*'
        r'(?:mg/dL|mmol/L|mEq/L|g/dL|%|IU/L|U/L|µmol/L|umol/L)?\s*[—\-–]',
        title,
    )
    if m:
        return m.group(1).strip(), float(m.group(2))
    return None


def _vital_improved_since_snapshot(
    cpmrn: str,
    encounter: int,
    vital_key: str,
    problem_name: str,
    latest_vital_ts: datetime,
) -> bool:
    """
    Fetch live vitals from Radar and return True if a newer reading arrived
    AFTER the most recent vital in the snapshot AND it shows improvement.

    Uses latest_vital_ts (the timestamp of the most recent vital reading that
    was in the snapshot) as the cutoff — NOT snapshot_at (the GCS blob write
    time), because the chart puller's API call happens 1-2 minutes before the
    blob is written, so snapshot_at is too late a cutoff and misses readings
    that were verified in that gap.

    Logic:
      - Get live vitals (newest-first, verified only).
      - Split into: post-snapshot readings (ts > latest_vital_ts) and the
        alerting reading (most recent ts <= latest_vital_ts, i.e. what the
        model saw when it decided to alert).
      - If there is at least one post-snapshot reading AND its value is better
        than the alerting reading, return True → suppress the alert.

    Direction inference:
      - RR, Temp → "high" (lower is better)
      - SpO2, GCS → "low" (higher is better)
      - HR, BP, MAP → inferred from problem_name (tachy/brady, hyper/hypo); fails
        open (returns False) if direction cannot be determined.

    Always returns False on any error — fails open (alert is allowed through).
    """
    _FIXED_DIRECTION: dict[str, str] = {
        "RR":          "high",
        "TEMP":        "high",
        "TEMPERATURE": "high",
        "SPO2":        "low",
        "GCS":         "low",
    }
    vk = vital_key.strip().upper()
    direction = _FIXED_DIRECTION.get(vk)

    if direction is None:
        pn = problem_name.lower()
        if vk == "HR":
            if any(w in pn for w in ("tachycardia", "tachyarrhythmia", "fast heart", "tachypnea")):
                direction = "high"
            elif any(w in pn for w in ("bradycardia", "slow heart", "heart block")):
                direction = "low"
        elif vk in ("BP", "MAP"):
            if any(w in pn for w in ("hypertens", "elevated bp", "high bp", "high blood pressure")):
                direction = "high"
            elif any(w in pn for w in ("hypotens", "low bp", "low map", "shock", "low blood pressure")):
                direction = "low"

    if direction is None:
        return False  # ambiguous vital + problem name — fail open

    try:
        from tools.radar_sync.chart_puller import fetch_fresh_vitals
        from tools.radar_sync.status_classifier import _VITAL_FIELD
        import pandas as pd

        field = _VITAL_FIELD.get(vk)
        if not field:
            return False

        vitals = fetch_fresh_vitals(cpmrn, encounter)
        if not vitals:
            return False

        cutoff = latest_vital_ts if latest_vital_ts.tzinfo else latest_vital_ts.replace(tzinfo=timezone.utc)

        def _parse_val(v: dict) -> float | None:
            raw = v.get(field)
            if raw is None:
                return None
            try:
                # BP is stored as "SBP/DBP" — use SBP for direction comparison
                return float(str(raw).split("/")[0].strip())
            except (ValueError, TypeError):
                return None

        def _parse_ts(v: dict):
            try:
                return pd.to_datetime(v.get("timestamp"), utc=True).to_pydatetime()
            except Exception:
                return None

        # Split into post-snapshot and snapshot-era readings
        post_snap_vals: list[float] = []
        snap_val: float | None = None
        for v in vitals:  # newest-first
            ts = _parse_ts(v)
            val = _parse_val(v)
            if val is None or ts is None:
                continue
            if ts > cutoff:
                post_snap_vals.append(val)
            elif snap_val is None:
                snap_val = val  # most recent reading the pipeline actually saw

        if not post_snap_vals or snap_val is None:
            return False  # no newer data, or no snapshot baseline to compare against

        best_new = min(post_snap_vals) if direction == "high" else max(post_snap_vals)
        improved = best_new < snap_val if direction == "high" else best_new > snap_val

        if improved:
            logger.info(
                "problem_tracker: pre-send vital check for '%s' %s enc=%d — "
                "%s improved since snapshot: alerting_val=%s new_val=%s (direction=%s)",
                problem_name, cpmrn, encounter, vk, snap_val, best_new, direction,
            )
        return improved

    except Exception:
        logger.exception(
            "problem_tracker: pre-send vital check failed for '%s' %s enc=%d — continuing",
            problem_name, cpmrn, encounter,
        )
        return False


def _should_suppress_alert(cpmrn: str, encounter: int, problem_name: str, db: Any) -> bool:
    """
    Return True if we alerted recently and should hold off.

    Uses `current_interval_h` from the stored problem doc as the cooldown window
    so the backoff-decayed interval governs both forced-recheck cadence and
    re-alert suppression. Falls back to _ALERT_COOLDOWN_H for legacy docs.
    """
    doc = db["patient_problems"].find_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        {"last_alerted_at": 1, "current_interval_h": 1},
    )
    if not doc:
        return False
    last = doc.get("last_alerted_at")
    if last is None:
        return False
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(last, datetime):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    cooldown_h = doc.get("current_interval_h") or _ALERT_COOLDOWN_H
    return (datetime.now(timezone.utc) - last) < timedelta(hours=cooldown_h)


# ── Upsert problem state ───────────────────────────────────────────────────────

def _upsert_problem(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    now: datetime,
    alerted: bool,
    db: Any,
) -> None:
    problem_name    = assessment["problem_name"]
    clinical_status = assessment.get("clinical_status", "stable")

    # Hard rule: next_check is ONLY valid for worsening/critical problems.
    # If the model set one for stable/improving/resolved, discard it unconditionally.
    # This prevents perpetually-overdue lab checks on resolved problems.
    nc_raw = assessment.get("next_check")
    if nc_raw and clinical_status not in ("worsening", "critical"):
        logger.info(
            "_upsert_problem: discarding next_check for '%s' %s enc=%d "
            "(status=%s — next_check only allowed for worsening/critical)",
            problem_name, cpmrn, encounter, clinical_status,
        )
        nc_raw = None

    if nc_raw:
        nc_type = nc_raw.get("type", "vital")

        # Extract the machine-readable fetch key and optional human-readable label.
        # New schema: vital_key (enum) / lab_name / label
        # Old schema fallback: what (kept for backward compat with existing DB docs)
        if nc_type == "vital":
            nc_key   = nc_raw.get("vital_key") or nc_raw.get("what", "")
            nc_label = nc_raw.get("label") or nc_key
        elif nc_type == "lab":
            nc_key   = nc_raw.get("lab_name") or nc_raw.get("what", "")
            nc_label = nc_raw.get("label") or nc_key
        else:  # io
            nc_key   = ""
            nc_label = nc_raw.get("label") or "fluid balance"

        if nc_type == "vital" and not nc_key:
            logger.warning(
                "_upsert_problem: missing vital_key for '%s' %s enc=%d — discarding next_check",
                problem_name, cpmrn, encounter,
            )
            nc_raw = None
        elif nc_type == "lab" and not nc_key:
            logger.warning(
                "_upsert_problem: missing lab_name for '%s' %s enc=%d — discarding next_check",
                problem_name, cpmrn, encounter,
            )
            nc_raw = None

        # fulfilled=True  → model found the expected result; start a fresh window
        # fulfilled=False → result still pending; preserve the original due_after
        #                   so the window doesn't roll forward every hour
        fulfilled = nc_raw.get("fulfilled", True)

        # Load prior state once for both due_after and backoff computation.
        prev_doc = db["patient_problems"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
            {"next_check": 1, "alert_attempts": 1, "first_alerted_at": 1, "being_addressed": 1, "last_alerted_at": 1},
        ) or {}

        # ── Backoff computation ───────────────────────────────────────────────
        prev_attempts     = int(prev_doc.get("alert_attempts") or 0)
        normalized        = clinical_status in ("stable", "improving", "resolved")
        new_plan          = bool(assessment.get("being_addressed")) and not bool(prev_doc.get("being_addressed"))
        still_bad         = clinical_status in ("worsening", "critical") and not assessment.get("being_addressed")

        if normalized or new_plan:
            alert_attempts    = 0
            first_alerted_at  = None
        elif alerted and still_bad:
            alert_attempts    = prev_attempts + 1
            prev_first        = prev_doc.get("first_alerted_at")
            first_alerted_at  = prev_first if prev_first else now
        else:
            alert_attempts    = prev_attempts
            first_alerted_at  = prev_doc.get("first_alerted_at")

        interval_h = _backoff_interval_h(nc_type, nc_key, alert_attempts)

        if fulfilled:
            if not alerted:
                # Alert was suppressed (cooldown). Align due_after with the cooldown expiry
                # so the forced recheck fires exactly when the alert becomes sendable again —
                # not at now+interval_h which would waste an expensive run mid-cooldown.
                _last = prev_doc.get("last_alerted_at")
                if isinstance(_last, str):
                    try:
                        _last = datetime.fromisoformat(_last.replace("Z", "+00:00"))
                    except ValueError:
                        _last = None
                if isinstance(_last, datetime):
                    if _last.tzinfo is None:
                        _last = _last.replace(tzinfo=timezone.utc)
                    cooldown_expiry = _last + timedelta(hours=interval_h)
                    due_after = cooldown_expiry if cooldown_expiry > now else now + timedelta(hours=interval_h)
                else:
                    due_after = now + timedelta(hours=interval_h)
            else:
                due_after = now + timedelta(hours=interval_h)
        else:
            stored_due = (prev_doc.get("next_check") or {}).get("due_after")
            if isinstance(stored_due, datetime):
                if stored_due.tzinfo is None:
                    stored_due = stored_due.replace(tzinfo=timezone.utc)
                due_after = stored_due
            else:
                due_after = now + timedelta(hours=interval_h)

        next_check: dict | None = {
            "key":       nc_key,    # machine-readable fetch argument
            "label":     nc_label,  # human-readable display text
            "type":      nc_type,
            "due_after": due_after,
        }
    else:
        # No next_check — stable/improving/resolved; clear any stored window.
        # Still compute backoff variables so set_fields below can reference them.
        next_check = None
        prev_doc = db["patient_problems"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
            {"alert_attempts": 1, "first_alerted_at": 1, "being_addressed": 1},
        ) or {}
        prev_attempts    = int(prev_doc.get("alert_attempts") or 0)
        normalized       = clinical_status in ("stable", "improving", "resolved")
        new_plan         = bool(assessment.get("being_addressed")) and not bool(prev_doc.get("being_addressed"))
        still_bad        = clinical_status in ("worsening", "critical") and not assessment.get("being_addressed")
        if normalized or new_plan:
            alert_attempts   = 0
            first_alerted_at = None
        elif alerted and still_bad:
            alert_attempts   = prev_attempts + 1
            prev_first       = prev_doc.get("first_alerted_at")
            first_alerted_at = prev_first if prev_first else now
        else:
            alert_attempts   = prev_attempts
            first_alerted_at = prev_doc.get("first_alerted_at")
        # Use a neutral interval for problems without a next_check (alert cooldown only)
        nc_type_fb, nc_key_fb = "vital", ""
        interval_h = _backoff_interval_h(nc_type_fb, nc_key_fb, alert_attempts)

    audit_entry = {
        "assessed_at":        now,
        "clinical_status":    assessment.get("clinical_status"),
        "being_addressed":    assessment.get("being_addressed"),
        "addressed_evidence": assessment.get("addressed_evidence", ""),
        "next_check":         next_check,
        "alerted":            alerted,
        "should_alert":       assessment.get("should_alert"),
        "alert_reason":        assessment.get("alert_reason", ""),
        "alert_title":         assessment.get("alert_title", ""),
        "note_vs_objective":   assessment.get("note_vs_objective", ""),
        "cited_notes":         assessment.get("cited_notes", []),
    }

    # Write reasoning fingerprint if provided by model.
    # New format: plain string. Old format (dict) kept for backward compat with stored docs.
    raw_fp = assessment.get("reasoning_fingerprint")
    stored_fingerprint: dict | None = None
    if isinstance(raw_fp, str) and raw_fp.strip():
        stored_fingerprint = {"anchored_at": now, "reasoning_chain": raw_fp.strip()}
    elif isinstance(raw_fp, dict) and raw_fp.get("reasoning_chain"):
        stored_fingerprint = {
            "anchored_at":      now,
            "reasoning_chain":  raw_fp.get("reasoning_chain", ""),
            "key_evidence":     raw_fp.get("key_evidence") or [],
            "alert_status":     raw_fp.get("alert_status", ""),
            "watch_conditions": raw_fp.get("watch_conditions") or [],
        }

    set_fields: dict = {
        "CPMRN":              cpmrn,
        "encounter":          encounter,
        "problem_name":       problem_name,
        "clinical_status":    assessment.get("clinical_status"),
        "being_addressed":    assessment.get("being_addressed"),
        "addressed_evidence": assessment.get("addressed_evidence", ""),
        "next_check":         next_check,  # None clears the field
        "last_assessed_at":   now,
        # Backoff state — drives both next_check cadence and re-alert cooldown
        "alert_attempts":     alert_attempts,
        "current_interval_h": interval_h,
    }
    if first_alerted_at is not None:
        set_fields["first_alerted_at"] = first_alerted_at
    elif normalized or new_plan:
        # Explicitly clear the streak timestamp on normalization/new-plan
        set_fields["first_alerted_at"] = None
    if stored_fingerprint:
        set_fields["reasoning_fingerprint"] = stored_fingerprint

    # Persist context_gate if model provided one.
    raw_gate = assessment.get("context_gate")
    if isinstance(raw_gate, dict) and raw_gate.get("verdict"):
        verdict = raw_gate.get("verdict", "")
        # Parse valid_until_iso → datetime
        valid_until: datetime | None = None
        viso = raw_gate.get("valid_until_iso")
        if viso:
            try:
                valid_until = datetime.fromisoformat(str(viso).replace("Z", "+00:00"))
            except Exception:
                logger.warning("_upsert_problem: could not parse valid_until_iso=%r for '%s'", viso, problem_name)

        # Eligibility: permissive_ended → sticky ended; breached keeps active
        if verdict in ("permissive_ended", "no_permissive_context"):
            eligibility = "ended" if verdict == "permissive_ended" else "none"
        else:
            eligibility = "active"

        stored_gate: dict = {
            "anchored_at":      now,
            "verdict":          verdict,
            "eligibility":      eligibility,
            "scenario":         raw_gate.get("scenario", "none"),
            "band_description": raw_gate.get("band_description", ""),
            "valid_until":      valid_until,
            "rationale":        raw_gate.get("rationale", ""),
            "plan_if_permissive": raw_gate.get("plan_if_permissive", ""),
            "plan_when_ended":  raw_gate.get("plan_when_ended", ""),
            "invalidate_if":    raw_gate.get("invalidate_if", []),
        }
        set_fields["context_gate"] = stored_gate
        logger.info(
            "_upsert_problem: gate verdict=%s eligibility=%s for '%s' %s enc=%d",
            verdict, eligibility, problem_name, cpmrn, encounter,
        )

    update: dict = {
        "$set": set_fields,
        "$setOnInsert": {"first_detected_at": now},
        "$push": {"assessments": {"$each": [audit_entry], "$slice": -50}},  # keep last 50
    }

    if alerted:
        update["$set"]["last_alerted_at"] = now

    db["patient_problems"].update_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        update,
        upsert=True,
    )


# ── Co-existing problem statuses (Option B) ───────────────────────────────────

def _build_coexisting_block(cpmrn: str, encounter: int, db: Any) -> str:
    """
    Pull the current assessment state for ALL tracked problems for this patient
    and format as a context block. Injected into the user_msg so the LLM can
    reason about causal relationships — e.g. AKI secondary to Septic Shock that
    is already being_addressed should NOT re-alert for AKI.
    """
    docs = list(db["patient_problems"].find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"problem_name": 1, "clinical_status": 1, "being_addressed": 1,
         "addressed_evidence": 1, "last_alerted_at": 1},
    ))
    if not docs:
        return ""

    lines: list[str] = [
        "== CO-EXISTING PROBLEM STATUSES ==",
        "Current assessment of all tracked problems for this patient.",
        "Use this to reason about causal relationships — e.g. if AKI is secondary to",
        "Septic Shock and Septic Shock is being_addressed, do NOT alert for AKI solely",
        "because creatinine is elevated (renal recovery lags 24-72h behind haemodynamic recovery).",
        "",
    ]
    for doc in docs:
        last_alert = doc.get("last_alerted_at")
        last_alert_str = last_alert.strftime("%Y-%m-%d %H:%M UTC") if isinstance(last_alert, datetime) else "never"
        being_addressed = doc.get("being_addressed", False)
        lines.append(
            f"  {doc['problem_name']}: {doc.get('clinical_status', '?').upper()}"
            f" | being_addressed={being_addressed}"
            f" | last_alerted={last_alert_str}"
        )
        evidence = doc.get("addressed_evidence", "")
        if evidence and being_addressed:
            lines.append(f"    Plan evidence: {evidence[:150]}")

    return "\n".join(lines)


# ── Clinical context rules (MongoDB-backed) ───────────────────────────────────

def _get_clinical_context_overrides(problems: list[dict], db: Any) -> str:
    """
    Fetch enabled clinical context rules from the `clinical_context_rules` collection
    and return any that match the patient's active problem list as a single injected
    string for the LLM prompt.

    Each rule document:
      {
        "_id":               "ischemic_stroke_permissive_bp",
        "condition_pattern": "ischemic stroke",   # case-insensitive substring match
        "rule_text":         "⚕ Permissive hypertension applies...",
        "enabled":           true,
        "reference":         "AHA/ASA 2019 Stroke Guidelines"
      }
    """
    try:
        rules = list(db["clinical_context_rules"].find({"enabled": True}))
    except Exception:
        logger.exception("problem_tracker: failed to fetch clinical_context_rules")
        return ""

    if not rules:
        return ""

    active_names = " ".join(p.get("name", "").lower() for p in problems)
    matched: list[str] = []
    for rule in rules:
        pattern = rule.get("condition_pattern", "").lower().strip()
        if pattern and pattern in active_names:
            matched.append(rule["rule_text"])

    return "\n".join(matched)


# ── Pre-fetch helpers (Fix 4: collapse 3-5 rounds → 1-2) ─────────────────────

def _buffer_hours_for_problem(problem_name: str, nc_type: str) -> int:
    """Return the appropriate treatment response buffer (hours) for a problem."""
    name_lower = problem_name.lower()
    for keyword, hours in _KEYWORD_BUFFER_H:
        if keyword in name_lower:
            return hours
    return _RESPONSE_BUFFER_H.get(nc_type, 4)


def _compute_timing_context(
    problems: list[dict],
    session_chunks: list,
    snapshot_at: datetime,
    cpmrn: str,
    encounter: int,
    db: Any,
) -> str:
    """
    For each worsening/critical problem, compute the gap between the triggering
    snapshot and the most recent plan note. If within the response buffer, emit
    a DIRECTIVE telling the model not to apply the treatment-inadequate override.
    """
    if isinstance(snapshot_at, str):
        snapshot_at = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)

    def _utc(t: datetime) -> datetime:
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t

    sections: list[str] = []

    for prob in problems:
        if prob.get("status", "stable") not in ("worsening", "critical"):
            continue

        name = prob.get("name", "")

        stored  = db["patient_problems"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": name},
            {"next_check": 1},
        )
        nc_type = ((stored or {}).get("next_check") or {}).get("type", "lab")
        buf_h   = _buffer_hours_for_problem(name, nc_type)

        # Query notes for this specific problem to get the most recent plan note
        try:
            from tools.radar_sync.query_notes import query_patient_notes_with_chunks
            start_idx = len(session_chunks)
            _, new_chunks = query_patient_notes_with_chunks(
                cpmrn, encounter,
                f"{name} management plan treatment response",
                start_index=start_idx,
            )
            session_chunks.extend(new_chunks)
        except Exception:
            logger.exception("timing_context: note query failed for '%s' %s", name, cpmrn)
            new_chunks = []

        valid = [c for c in new_chunks if isinstance(getattr(c, "note_time", None), datetime)]
        if not valid:
            continue

        most_recent = max(valid, key=lambda c: _utc(c.note_time))
        note_time   = _utc(most_recent.note_time)
        gap_min     = (note_time - snapshot_at).total_seconds() / 60

        if note_time >= snapshot_at and gap_min <= buf_h * 60:
            remaining_min = int(buf_h * 60 - gap_min)
            sections.append(
                f"⚠ {name}\n"
                f"  Triggering data (snapshot): {snapshot_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"  Plan note written:          {note_time.strftime('%Y-%m-%d %H:%M UTC')}"
                f" (+{int(gap_min)} min after data)\n"
                f"  Response buffer:            {buf_h}h — {remaining_min} min remaining\n"
                f"  DIRECTIVE: Plan is a direct response to this deterioration.\n"
                f"  → being_addressed = True\n"
                f"  → Do NOT apply treatment-inadequate override\n"
                f"  → Do NOT alert — set next_check to monitor expected response"
            )
        elif note_time < snapshot_at:
            stale_min = int((snapshot_at - note_time).total_seconds() / 60)
            sections.append(
                f"ℹ {name}\n"
                f"  Plan note written:          {note_time.strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"  Triggering data (snapshot): {snapshot_at.strftime('%Y-%m-%d %H:%M UTC')}"
                f" (+{stale_min} min after note)\n"
                f"  → Plan predates this deterioration by {stale_min} min."
                f" Assess whether it accounts for the current data."
            )

    if not sections:
        return ""

    header = [
        "== TIMING CONTEXT ==",
        "Pre-computed gap between triggering data and most recent plan note per problem.",
        "DIRECTIVE lines must be honoured unconditionally.",
        "",
    ]
    return "\n".join(header + sections)


def _build_prefetch_block(
    cpmrn: str,
    encounter: int,
    problems: list[dict],
    db: Any,
    session_chunks: list,
    snapshot_at: datetime | None = None,
    staleness_overrides: dict[str, int] | None = None,
) -> str:
    """
    Pre-fetch all data the model is likely to need and return it as a formatted
    block injected into the first user message. Tool calls become fallbacks for
    edge cases not covered here.

    Fetches:
      - Stored problem state (+ auto-fetched trends) for every active problem
      - Top-5 recent clinical notes (pre-queried, indices start from 0)
      - Fluid balance / IO (last 12h)
    """
    lines: list[str] = [
        "== PRE-FETCHED CONTEXT ==",
        "Use the data below as your primary source.",
        "Only call tools if you need something not listed here (e.g. a specific older lab).",
        "",
    ]

    # 1. Stored problem state for every active problem
    stored_names = [
        d["problem_name"]
        for d in db["patient_problems"].find(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"problem_name": 1},
        )
    ]
    if stored_names:
        lines.append("--- Stored problem states (includes auto-fetched trend data) ---")
        for name in stored_names:
            try:
                state_text = _get_problem_state(cpmrn, encounter, name, db)
                lines.append(state_text)
                lines.append("")
            except Exception:
                logger.exception("prefetch: _get_problem_state failed for '%s' %s", name, cpmrn)

    # 1b. Auto-fetch labs for NEW problems (no stored state → no next_check to fetch from).
    #     Without this, the model only has screener-flag data for brand-new problems
    #     and may miss a corrected result (e.g. K=2.78 from ABG screener flag, but
    #     latest serum K=4 never fetched).
    _NEW_PROBLEM_LAB = {
        "hypokalemia":       "potassium",
        "hyperkalemia":      "potassium",
        "hyponatremia":      "sodium",
        "hypernatremia":     "sodium",
        "hypocalcemia":      "calcium",
        "hypercalcemia":     "calcium",
        "hypomagnesemia":    "magnesium",
        "hypophosphatemia":  "phosphate",
        "hypoglycemia":      "glucose",
        "hyperglycemia":     "glucose",
        "anemia":            "hb",
        "thrombocytopenia":  "platelets",
        "leukocytosis":      "wbc",
        "hypoalbuminemia":   "albumin",
        "aki":               "cr",
        "acute kidney":      "cr",
        "renal failure":     "cr",
        "lactic acidosis":   "lactate",
        "hyperlactatemia":   "lactate",
    }
    new_problem_names = [p["name"] for p in problems if p["name"] not in stored_names]
    if new_problem_names:
        try:
            from tools.radar_sync.status_classifier import _get_lab_trend
            fetched_labs: set[str] = set()
            autofetch_lines: list[str] = []
            for pname in new_problem_names:
                pname_lc = pname.lower()
                lab_key = next(
                    (lab for kw, lab in _NEW_PROBLEM_LAB.items() if kw in pname_lc),
                    None,
                )
                if lab_key and lab_key not in fetched_labs:
                    fetched_labs.add(lab_key)
                    trend = _get_lab_trend(cpmrn, encounter, lab_key, n=4)
                    if trend and not trend.startswith("No ") and not trend.startswith("Unknown"):
                        autofetch_lines.append(f"  {pname} → {lab_key} trend: {trend}")
            if autofetch_lines:
                lines.append("--- Auto-fetched labs for new problems (use to verify screener flags) ---")
                lines.extend(autofetch_lines)
                lines.append("")
        except Exception:
            logger.exception("prefetch: new-problem auto-fetch failed for %s enc=%d", cpmrn, encounter)

    # 2. Recent clinical notes (pre-queried; indices available for citation)
    try:
        from tools.radar_sync.query_notes import query_patient_notes_with_chunks
        start_idx = len(session_chunks)
        note_text, new_chunks = query_patient_notes_with_chunks(
            cpmrn, encounter,
            "recent management plan treatment assessment clinical notes",
            start_index=start_idx,
        )
        session_chunks.extend(new_chunks)
        lines.append("--- Recent clinical notes (pre-fetched; use indices for citations) ---")
        lines.append(note_text)
        lines.append("")
    except Exception:
        logger.exception("prefetch: note query failed for %s enc=%d", cpmrn, encounter)

    # 3. IO balance
    try:
        from tools.radar_sync.status_classifier import _get_io
        io_text = _get_io(cpmrn, encounter, n_hours=12)
        lines.append("--- Fluid balance / IO (last 12h) ---")
        lines.append(io_text)
        lines.append("")
    except Exception:
        logger.exception("prefetch: IO fetch failed for %s enc=%d", cpmrn, encounter)

    # 4. Timing context — must appear last so note indices from (2) are already registered
    if snapshot_at is not None:
        try:
            timing_block = _compute_timing_context(
                problems, session_chunks, snapshot_at, cpmrn, encounter, db,
            )
            if timing_block:
                lines.append(timing_block)
                lines.append("")
        except Exception:
            logger.exception("prefetch: timing context failed for %s enc=%d", cpmrn, encounter)

    # 4b. Vital staleness warning — graded by age:
    #   >2h  → soft advisory: be cautious, more recent unverified readings may exist
    #   >8h  → hard warning: do NOT alert on vital-sign-based problems
    if snapshot_at is not None:
        try:
            from tools.radar_sync.status_classifier import _get_latest_vital_ts
            latest_vt = _get_latest_vital_ts(cpmrn, encounter)
            if latest_vt is not None:
                snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
                vital_age_h = (snap - latest_vt).total_seconds() / 3600
                if vital_age_h > _VITAL_STALENESS_HOURS:
                    age_str = f"{vital_age_h:.1f}h"
                    lines.append(
                        f"⚠ VITAL STALENESS WARNING: Most recent verified vital is "
                        f"{latest_vt.strftime('%Y-%m-%d %H:%M UTC')} — {age_str} before this "
                        f"snapshot. Vitals are STALE. Do NOT alert on vital-sign-based problems "
                        f"(tachycardia, bradycardia, hypotension, hypertension, hypoxemia, "
                        f"tachypnea, shock, desaturation). A stale reading cannot indicate "
                        f"current haemodynamic instability."
                    )
                    lines.append("")
                    logger.info(
                        "prefetch: vital staleness warning injected for %s enc=%d (%.1fh old)",
                        cpmrn, encounter, vital_age_h,
                    )
                elif vital_age_h > _VITAL_CARD_WARNING_HOURS:
                    age_str = f"{vital_age_h:.1f}h"
                    lines.append(
                        f"⚠ VITAL DATA ADVISORY: Most recent verified vital is "
                        f"{latest_vt.strftime('%Y-%m-%d %H:%M UTC')} — {age_str} before this "
                        f"snapshot. The bedside monitor may have more recent readings that have "
                        f"not yet been verified in the system. Be cautious about alerting on "
                        f"vital-sign trends: if the only concerning reading is old (>{_VITAL_CARD_WARNING_HOURS}h) "
                        f"and more recent readings are missing, the situation may have already resolved."
                    )
                    lines.append("")
                    logger.info(
                        "prefetch: vital advisory injected for %s enc=%d (%.1fh old)",
                        cpmrn, encounter, vital_age_h,
                    )
        except Exception:
            logger.exception("prefetch: vital staleness check failed for %s enc=%d", cpmrn, encounter)

    # 4c. Lab staleness warning — for each auto-fetched lab in section 1b that appears
    #     stale (>24h), inject an explicit per-lab warning so the model doesn't alert on it.
    if snapshot_at is not None:
        try:
            from tools.radar_sync.status_classifier import _get_latest_lab_ts
            snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
            stale_lab_warnings: list[str] = []
            _stale_ovr = staleness_overrides or {}
            for pname in ([p.get("name", "") for p in problems]):
                pname_lc = pname.lower()
                lab_key = next((lab for kw, lab in _NEW_PROBLEM_LAB.items() if kw in pname_lc), None)
                if not lab_key:
                    continue
                latest_lt = _get_latest_lab_ts(cpmrn, encounter, lab_key)
                if latest_lt is not None:
                    lab_age_h = (snap - latest_lt).total_seconds() / 3600
                    staleness_limit = _stale_ovr.get(lab_key.lower(), _LAB_STALENESS_HOURS)
                    if lab_age_h > staleness_limit:
                        stale_lab_warnings.append(
                            f"  {pname} → most recent {lab_key} is "
                            f"{latest_lt.strftime('%Y-%m-%d %H:%M UTC')} "
                            f"({lab_age_h:.0f}h ago, limit {staleness_limit}h) — DO NOT ALERT"
                        )
            if stale_lab_warnings:
                lines.append(
                    "⚠ LAB STALENESS WARNING: The following labs are too old to alert on "
                    "(per-lab staleness limits apply):"
                )
                lines.extend(stale_lab_warnings)
                lines.append("")
        except Exception:
            logger.exception("prefetch: lab staleness check failed for %s enc=%d", cpmrn, encounter)

    # 5. Linked-lab co-prefetch — fetch physiologically related labs when a problem
    #    (e.g. lactic acidosis) requires cross-lab plausibility checking.
    try:
        from tools.radar_sync.linked_lab_prefetch import (
            get_triggered_context_labs,
            build_linked_lab_block,
        )
        context_labs = get_triggered_context_labs(problems)
        if context_labs:
            existing = "\n".join(lines)
            linked_block = build_linked_lab_block(cpmrn, encounter, context_labs, existing)
            if linked_block:
                lines.append(linked_block)
                lines.append("")
    except Exception:
        logger.exception("prefetch: linked-lab fetch failed for %s enc=%d", cpmrn, encounter)

    return "\n".join(lines)


def _build_fingerprint_block(cpmrn: str, encounter: int, db: Any) -> str:
    """
    Load prior reasoning fingerprints from patient_problems and format them
    for injection into the prompt. Returns empty string if no fingerprints exist.
    """
    docs = list(db["patient_problems"].find(
        {"CPMRN": cpmrn, "encounter": encounter,
         "reasoning_fingerprint": {"$exists": True}},
        {"problem_name": 1, "reasoning_fingerprint": 1, "last_assessed_at": 1},
    ))
    if not docs:
        return ""

    lines: list[str] = [
        "== PRIOR REASONING (from last run) ==",
        "For each problem below, read the prior reasoning chain and determine whether",
        "it still holds given the new data. If new data touches a watch condition,",
        "re-examine that problem fully. If no watch condition is touched and the data",
        "is unchanged, confirm in one sentence and carry the fingerprint forward.",
        "Rewrite reasoning_chain to reflect any updates.",
        "",
    ]

    for doc in docs:
        fp = doc.get("reasoning_fingerprint") or {}
        if not fp:
            continue
        last_at = doc.get("last_assessed_at")
        last_at_str = last_at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(last_at, datetime) else "unknown"
        lines.append(f"Problem: {doc['problem_name']}  (assessed: {last_at_str})")
        # reasoning_fingerprint is now a plain string; old docs may still have dict format
        reasoning = fp.get("reasoning_chain", "(none)")
        lines.append(f"  Reasoning: {reasoning}")
        # Legacy dict fields — only present in old-format stored fingerprints
        ev = fp.get("key_evidence") or []
        if ev:
            lines.append(f"  Key evidence: {ev}")
        alert_status = fp.get("alert_status", "")
        if alert_status:
            lines.append(f"  Alert status: {alert_status}")
        wc = fp.get("watch_conditions") or []
        if wc:
            lines.append(f"  Watch conditions: {wc}")
        lines.append("")

    return "\n".join(lines)


# ── Context-gate helpers ──────────────────────────────────────────────────────

def _load_monitoring_protocols(db: Any) -> list[dict]:
    """Fetch all monitoring protocols from MongoDB. Returns [] on failure (safe default)."""
    try:
        return list(db["monitoring_protocols"].find({}))
    except Exception:
        logger.exception("problem_tracker: failed to load monitoring_protocols")
        return []


def _match_protocols(problems: list[dict], protocols: list[dict]) -> dict[str, dict]:
    """
    For each problem, find the first matching protocol (applies_when substring match).
    Returns {problem_name: protocol_doc}.
    """
    matched: dict[str, dict] = {}
    for prob in problems:
        name_lc = prob.get("name", "").lower()
        for proto in protocols:
            if any(kw in name_lc for kw in proto.get("applies_when", [])):
                matched[prob["name"]] = proto
                break
    return matched


def _build_gate_block(
    matched_protocols: dict[str, dict],
    stored_gates: dict[str, dict],
    snapshot_at: datetime,
) -> str:
    """
    Build the CONTEXT GATE prompt block for matched problems.
    Injects:
      - For problems with an active gate: carry-forward instructions + stored state
      - For problems without a gate: full scenario checklist for fresh derivation
    """
    if not matched_protocols:
        return ""

    lines: list[str] = [
        "== CONTEXT GATE ==",
        "The following problems have monitoring protocols. For each:",
        "  1. Check if any invalidate_if trigger appears in new notes/data → verdict=permissive_ended",
        "  2. Check if the current value is inside the band_description → if not, verdict=permissive_breached",
        "  3. If all clear → verdict=permissive_active; carry forward in one sentence",
        "  Call tools only if uncertain about a trigger or band status.",
        "  In set_all_assessments, populate context_gate for EACH of these problems.",
        "",
    ]

    for problem_name, proto in matched_protocols.items():
        stored = stored_gates.get(problem_name, {})
        eligibility = stored.get("eligibility", "")
        valid_until = stored.get("valid_until")
        # Normalise valid_until to UTC-aware
        if isinstance(valid_until, datetime) and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)

        lines.append(f"--- {problem_name} | Protocol: {proto['protocol_id']} ---")

        if eligibility == "ended":
            # Gate already sticky-ended — skip gate logic, fall through to normal alert
            lines.append("  Gate: ENDED (eligibility permanently closed — normal alert rules apply)")
            lines.append("  Do NOT populate context_gate for this problem.")
            lines.append("")
            continue

        if stored and eligibility == "active":
            # Carry-forward path
            anchored_at = stored.get("anchored_at")
            anchored_str = anchored_at.strftime("%Y-%m-%d %H:%M UTC") if isinstance(anchored_at, datetime) else "unknown"
            valid_str = valid_until.strftime("%Y-%m-%d %H:%M UTC") if isinstance(valid_until, datetime) else "none"
            hours_remaining = (
                f"{(valid_until - snapshot_at).total_seconds() / 3600:.1f}h remaining"
                if isinstance(valid_until, datetime) else "no time cap"
            )

            lines.append(f"  Stored gate (anchored {anchored_str}):")
            lines.append(f"    Eligibility: {eligibility}")
            lines.append(f"    Scenario:    {stored.get('scenario', 'none')}")
            lines.append(f"    Band:        {stored.get('band_description', '?')}")
            lines.append(f"    Valid until: {valid_str} ({hours_remaining})")
            lines.append(f"    Invalidate if: {stored.get('invalidate_if', [])}")
            lines.append(f"    Prior rationale: {stored.get('rationale', '(none)')}")
            lines.append("")
            lines.append("  CARRY-FORWARD INSTRUCTION:")
            lines.append("    If no invalidate_if trigger appears in new data AND value is in-band →")
            lines.append("    set verdict=permissive_active, confirm in one sentence (no tool calls needed).")
            lines.append("    If a trigger appears OR value is out-of-band → re-derive fully using tools.")

        else:
            # Fresh derivation path — no stored gate
            lines.append(f"  Gate question: {proto.get('gate_question', '')}")
            lines.append("")
            lines.append("  Scenarios to rule in/out:")
            for s in proto.get("scenarios", []):
                lines.append(f"    [{s['name']}]")
                lines.append(f"      Band:         {s['band_description']}")
                lines.append(f"      Window:       {s['window']}")
                lines.append(f"      Invalidate if: {s['invalidate_if']}")
            lines.append("")
            lines.append(f"  If no scenario applies → verdict=no_permissive_context (normal alert rules apply).")
            lines.append(f"  If window closes/trigger fires → verdict=permissive_ended.")
            lines.append(f"  After window: {proto.get('escalation_target_after_window', '')}")

        lines.append("")

    return "\n".join(lines)


def _load_stored_gates(cpmrn: str, encounter: int, problem_names: list[str], db: Any) -> dict[str, dict]:
    """Load stored context_gate sub-documents for matched problems."""
    gates: dict[str, dict] = {}
    for name in problem_names:
        doc = db["patient_problems"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": name},
            {"context_gate": 1},
        )
        if doc and doc.get("context_gate"):
            gates[name] = doc["context_gate"]
    return gates


# ── IO charting-miss caveat ───────────────────────────────────────────────────

_IO_ALERT_KEYWORDS = (
    "oliguria", "anuria", "aki", "acute kidney", "renal failure",
    "urine output", "fluid balance",
)


def _maybe_append_charting_caveat(cpmrn: str, encounter: int, assessment: dict) -> None:
    """
    For IO-related alerts where intake is recorded but urine output is 0,
    append a sentence to alert_reason asking the clinician to verify it's not a charting miss.
    """
    problem_name = assessment.get("problem_name", "").lower()
    if not any(kw in problem_name for kw in _IO_ALERT_KEYWORDS):
        return
    try:
        from tools.radar_sync.status_classifier import _get_io_day_summary
        summary = _get_io_day_summary(cpmrn, encounter)
        if summary and summary["urine_ml"] == 0 and summary["intake_ml"] > 0:
            caveat = (
                f"\n\n⚠ Charting note: Day {summary['day_num']} intake is recorded "
                f"({summary['intake_ml']:.0f} ml) but urine output is 0 ml. "
                f"This pattern may reflect missed charting rather than true anuria — "
                f"please verify urine output at bedside before acting."
            )
            assessment["alert_reason"] = (assessment.get("alert_reason") or "") + caveat
    except Exception:
        logger.exception("problem_tracker: charting caveat injection failed for '%s' %s enc=%d",
                         assessment.get("problem_name", ""), cpmrn, encounter)


# ── Post-hoc alert deduplication ─────────────────────────────────────────────

def _suppress_redundant_alerts(to_alert: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """
    Safety-net deduplication after the model returns assessments.
    If a child problem (per _PROBLEM_SUBSUMES) appears in to_alert alongside
    its parent, the child is removed. The prompt-level PROBLEM CONSOLIDATION
    rule handles the common case; this catches misses.
    """
    if len(to_alert) < 2:
        return to_alert

    alerting_names_lc = [a.get("problem_name", "").lower() for a, _ in to_alert]
    to_remove: set[str] = set()

    for parent_pattern, children in _PROBLEM_SUBSUMES.items():
        # Check if any alerting problem matches this parent (substring either way)
        matched_parent_lc = next(
            (n for n in alerting_names_lc if parent_pattern in n or n in parent_pattern),
            None,
        )
        if matched_parent_lc is None:
            continue
        for child_pattern in children:
            for n in alerting_names_lc:
                if n == matched_parent_lc:
                    continue
                if child_pattern in n or n in child_pattern:
                    to_remove.add(n)
                    logger.info(
                        "_suppress_redundant_alerts: suppressing '%s' (absorbed by '%s')",
                        n, matched_parent_lc,
                    )

    if not to_remove:
        return to_alert

    return [(a, aid) for a, aid in to_alert if a.get("problem_name", "").lower() not in to_remove]


# ── Screener-flag new-finding evaluation ────────────────────────────────────────

_SCREENER_EVAL_SYSTEM = """You are triaging ONE Pass-1 screener flag for an ICU patient. The \
screener detected something potentially worth tracking. Your single job is to decide whether that \
finding is ALREADY represented in the current problem list, is GENUINELY NEW, or is not a real new \
problem at all.

This is a focused triage — not a full assessment. Do exactly one thing: call report_finding.

Decision rules:
- maps_to_existing — the flagged finding is the same clinical concern as a problem already in the \
list (semantic match, NOT string match). Examples: "elevated blood pressure" → "Hypertension"; \
"worsening hypoxemia" → an existing "Acute Respiratory Failure"; "Cr 3.41 rising" → an existing \
"Acute Kidney Injury". When it maps, return the existing problem's exact name in mapped_problem_name.
- new_problem — the flag names a clinical concern with NO semantic overlap with any existing \
problem. Give it a precise clinical name (e.g. "Myocardial Injury", "Hyperkalemia"), an \
initial_status, and a one-line current_state that includes the triggering value and its timestamp \
if present in the flag or prefetch data.
- ignore — the flag is not a trackable clinical problem (e.g. a generic note about monitoring, a \
value that is actually normal, or pure narrative with no abnormal finding).

Bias: when a finding could plausibly map to an existing problem, prefer maps_to_existing over \
new_problem — creating a duplicate is worse than folding it into an existing problem. Only choose \
new_problem when you are confident no existing problem covers it."""

_REPORT_FINDING_TOOL = {
    "name": "report_finding",
    "description": "Report the triage decision for the screener flag.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["maps_to_existing", "new_problem", "ignore"],
            },
            "mapped_problem_name": {
                "type": "string",
                "description": "If maps_to_existing: the EXACT name of the existing problem it maps to.",
            },
            "new_problem_name": {
                "type": "string",
                "description": "If new_problem: a precise clinical name (e.g. 'Myocardial Injury').",
            },
            "initial_status": {
                "type": "string",
                "enum": ["critical", "worsening", "stable"],
                "description": "If new_problem: the initial clinical status.",
            },
            "current_state": {
                "type": "string",
                "description": "If new_problem: one line incl. the triggering value + timestamp if known.",
            },
            "rationale": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
        },
        "required": ["decision", "rationale"],
    },
}


def evaluate_screener_flag(
    screener_flag: str,
    problems: list[dict],
    prefetch_block: str,
    client: Any,
    db: Any,
    cpmrn: str = "",
    encounter: int = 0,
) -> dict | None:
    """
    Decide whether a Pass-1 screener flag represents a genuinely NEW problem that is not
    yet in the tracked list. Runs as its own focused, single-shot LLM call — separate from
    the main tracker loop — so the new-finding decision is not diluted by the full
    problem-assessment workload (the failure mode behind missed findings like a flagged
    troponin elevation the main loop raced past).

    Returns a problem dict to append to the tracker's problem list when the flag is a
    confirmed-new problem; returns None when it maps to an existing problem or is ignorable.
    Because the semantic dedup happens here in isolation, appending the returned problem
    cannot create a duplicate of an existing one.
    """
    if not screener_flag:
        return None

    from tools.radar_sync.react_tracer import ReActTracer

    existing_names = [p.get("name", "") for p in problems]
    user_msg = (
        f"SCREENER FLAG:\n\"{screener_flag}\"\n\n"
        f"CURRENT TRACKED PROBLEMS:\n"
        + ("\n".join(f"  • {n}" for n in existing_names) if existing_names else "  (none)")
        + (f"\n\nPRE-FETCHED VITAL / LAB DATA:\n{prefetch_block}\n" if prefetch_block else "\n")
        + "\nCall report_finding with your decision."
    )

    tracer = ReActTracer(cpmrn, encounter, step="screener_flag_eval", db=db)
    tracer.start_round(0)
    try:
        resp = client.create_message(
            messages=[{"role": "user", "content": user_msg}],
            tools=[_REPORT_FINDING_TOOL],
            system=_SCREENER_EVAL_SYSTEM,
            max_tokens=2000,
            force_tool=True,
        )
    except Exception:
        logger.exception("evaluate_screener_flag: LLM call failed for %s enc=%d", cpmrn, encounter)
        tracer.end_round()
        tracer.save(final_output={"error": "llm_failed"})
        return None

    tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.thinking_tokens)

    decision_args: dict = {}
    for block in resp.content:
        if block.type == "tool_use" and block.name == "report_finding":
            decision_args = block.input or {}
            tracer.log_tool_call(block.name, block.input)
        elif block.type == "thinking":
            tracer.log_thinking(block.text)
    tracer.end_round()

    decision = decision_args.get("decision")
    logger.info(
        "evaluate_screener_flag: %s enc=%d → decision=%s (%s)",
        cpmrn, encounter, decision, decision_args.get("rationale", ""),
    )
    tracer.save(final_output=decision_args)

    if decision == "new_problem":
        name = (decision_args.get("new_problem_name") or "").strip()
        if not name:
            return None
        # Guard against a duplicate slipping through despite the semantic-match instruction.
        if any(name.lower() == n.lower() for n in existing_names):
            logger.info(
                "evaluate_screener_flag: %s enc=%d — '%s' already in problem list, not appending",
                cpmrn, encounter, name,
            )
            return None
        return {
            "name": name,
            "status": decision_args.get("initial_status", "worsening"),
            "current_state": decision_args.get("current_state", "Flagged by Pass-1 screener"),
            "management": "not specified",
            "_from_screener_flag": True,
        }

    return None


def overdue_next_checks(
    cpmrn: str,
    encounter: int,
    db: Any,
    now: datetime | None = None,
) -> list[dict]:
    """
    Return all patient_problems for this patient that have an overdue next_check.

    A next_check is overdue when now > due_after (the stored window has elapsed).
    Used by the scheduler to decide whether to force an expensive run even when
    pass-1 would otherwise skip it.

    Returns a list of dicts: [{problem_name, type, key, label, due_after, doc}]
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    docs = list(db["patient_problems"].find(
        {"CPMRN": cpmrn, "encounter": encounter, "next_check": {"$ne": None}},
        {"problem_name": 1, "next_check": 1, "clinical_status": 1},
    ))

    overdue = []
    for doc in docs:
        nc = doc.get("next_check") or {}
        due = nc.get("due_after")
        if due is None:
            continue
        if isinstance(due, str):
            try:
                due = datetime.fromisoformat(due.replace("Z", "+00:00"))
            except ValueError:
                continue
        if not isinstance(due, datetime):
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if now > due:
            overdue.append({
                "problem_name": doc.get("problem_name", ""),
                "type":         nc.get("type", ""),
                "key":          nc.get("key", ""),
                "label":        nc.get("label", ""),
                "due_after":    due,
                "doc":          doc,
            })

    return overdue


# ── Main entry point ───────────────────────────────────────────────────────────

def track_problems(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    snapshot_at: datetime,
    screener_flag: str = "",
    focus: list[dict] | None = None,
    delta: dict | None = None,
    clinical_timeline: dict | None = None,
    problems_filter: list[str] | None = None,
) -> dict:
    """
    Run the problem tracker ReAct loop for one patient.
    Returns a status dict for the scheduler result entry.

    delta: the extract_delta() output for this cycle. When provided:
      - Phase A: a "DELTA THIS RUN" header is prepended to the user message to steer
        LLM attention toward what actually changed.
      - Phase B: only category rule blocks relevant to the delta are injected.
      - Phase C: (shadow mode or active) problem list is restricted to blast radius.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from backend.services.emr.db import get_db
    from tools.radar_sync.react_tracer import ReActTracer
    from tools.radar_sync.clinical_timeline import (
        empty_timeline, render_for_tracker, apply_updates,
    )

    # Coerce snapshot_at to datetime (GCS returns ISO strings from JSON)
    if isinstance(snapshot_at, str):
        snapshot_at = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)

    db     = get_db()
    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_TRACKER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="problem_tracker", db=db)

    problems: list[dict] = structured_summary.get("problems", [])
    if not problems:
        if focus:
            # Forced follow-up: synthesize stub problems from stored docs so the
            # recheck runs even when the rolling summary has no active problems.
            problems = []
            for f in focus:
                stored_doc = f.get("doc") or {}
                problems.append({
                    "name":             f["problem_name"],
                    "status":           stored_doc.get("clinical_status", "worsening"),
                    "current_state":    f"Forced follow-up: {f['type']} — {f['label'] or f['key']}",
                    "management":       stored_doc.get("addressed_evidence", "not specified"),
                    "_from_forced_recheck": True,
                })
            logger.info(
                "problem_tracker: no problems in summary for %s enc=%d — using %d forced-recheck stub(s)",
                cpmrn, encounter, len(problems),
            )
        else:
            logger.info("problem_tracker: no problems in summary for %s enc=%d", cpmrn, encounter)
            return {"problem_tracker": "no_problems"}

    # ── Phase C — blast-radius scoping ────────────────────────────────────────────
    # When problems_filter is passed by the scheduler (pre-computed from trigger evidence),
    # use it directly and activate pruning. Otherwise fall back to the old shadow-mode
    # behaviour (DELTA_SCOPE_PRUNE=1 env var required for active pruning).
    _prune_active = problems_filter is not None or os.environ.get("DELTA_SCOPE_PRUNE", "").strip() == "1"
    if problems_filter is not None and problems:
        _focus_names = {f["problem_name"] for f in (focus or [])}
        _filter_set  = set(problems_filter) | _focus_names
        _assess = [p for p in problems if p["name"] in _filter_set]
        _cold   = [p for p in problems if p["name"] not in _filter_set]
        if _cold:
            logger.info(
                "problem_tracker: problems_filter active — assess=%s prune=%s for %s enc=%d",
                [p["name"] for p in _assess], [p["name"] for p in _cold], cpmrn, encounter,
            )
            if not _assess:
                logger.info(
                    "problem_tracker: problems_filter — no matching problems for %s enc=%d, skipping",
                    cpmrn, encounter,
                )
                return {"problem_tracker": "no_hot_problems"}
            problems = _assess
    elif delta:
        try:
            from tools.radar_sync.delta_scope import blast_radius_problems, classify_delta
            _scope = classify_delta(delta)
            if not _scope.is_wildcard and problems:
                _focus_names = {f["problem_name"] for f in (focus or [])}
                _hot = blast_radius_problems(delta, problems)
                _hot_names = {p["name"] for p in _hot}
                _cold = [p for p in problems if p["name"] not in _hot_names and p["name"] not in _focus_names]
                _assess = _hot + [p for p in problems if p["name"] in _focus_names and p["name"] not in _hot_names]
                if _cold:
                    logger.info(
                        "problem_tracker: delta-scope %s — would_assess=%s would_prune=%s for %s enc=%d",
                        "ACTIVE" if _prune_active else "shadow",
                        [p["name"] for p in _assess],
                        [p["name"] for p in _cold],
                        cpmrn, encounter,
                    )
                    if _prune_active:
                        if not _assess:
                            logger.info(
                                "problem_tracker: delta-scope active — no hot problems for %s enc=%d, skipping",
                                cpmrn, encounter,
                            )
                            return {"problem_tracker": "no_hot_problems"}
                        problems = _assess
        except Exception:
            logger.exception("problem_tracker: delta-scope failed for %s enc=%d — assessing all", cpmrn, encounter)

    # Gate: skip entirely if no clinical data has been recorded yet.
    # Newly admitted patients may have no vitals/labs; alerting on data absence is not actionable.
    try:
        from tools.radar_sync.status_classifier import _get_latest_vital_ts
        if _get_latest_vital_ts(cpmrn, encounter) is None:
            logger.info(
                "problem_tracker: no clinical data for %s enc=%d — skipping run",
                cpmrn, encounter,
            )
            return {"problem_tracker": "no_data_skipped"}
    except Exception:
        logger.exception("problem_tracker: data sufficiency check failed for %s enc=%d — continuing", cpmrn, encounter)

    # Accumulates Chunk objects from every query_patient_notes call this session.
    # Indexed by the [N] numbers the model sees, so cited_note_indices can be resolved.
    # Populated by prefetch FIRST so pre-fetched note indices are available for citation.
    session_chunks: list = []

    # ── Lab staleness overrides — loaded once, used in prefetch + alert gate ──
    _staleness_overrides: dict[str, int] = {}
    try:
        from tools.radar_sync.lab_staleness_overrides import load_overrides as _load_staleness_overrides
        _staleness_overrides = _load_staleness_overrides(db)
    except Exception:
        logger.exception("problem_tracker: lab_staleness_overrides load failed for %s enc=%d — using defaults", cpmrn, encounter)

    # ── Fix 4: Pre-fetch all data and inject into first message ──────────────
    try:
        prefetch_block = _build_prefetch_block(
            cpmrn, encounter, problems, db, session_chunks, snapshot_at=snapshot_at,
            staleness_overrides=_staleness_overrides,
        )
    except Exception:
        logger.exception("problem_tracker: prefetch failed for %s enc=%d — continuing without", cpmrn, encounter)
        prefetch_block = ""

    # ── Screener-flag new-finding evaluation ──────────────────────────────────
    # Run as its own focused call BEFORE the main loop. If it confirms a genuinely
    # new problem, append it to the list so the main tracker assesses it on the
    # ladder like any other problem (no separate alert path). The semantic dedup
    # happens inside evaluate_screener_flag, so this cannot create a duplicate.
    if screener_flag:
        try:
            _new_problem = evaluate_screener_flag(
                screener_flag, problems, prefetch_block, client, db, cpmrn, encounter,
            )
            if _new_problem:
                problems.append(_new_problem)
                logger.info(
                    "problem_tracker: screener flag added new problem '%s' for %s enc=%d",
                    _new_problem["name"], cpmrn, encounter,
                )
                # Re-build prefetch so the new problem's vitals/labs are in context.
                try:
                    prefetch_block = _build_prefetch_block(
                        cpmrn, encounter, problems, db, session_chunks, snapshot_at=snapshot_at,
                        staleness_overrides=_staleness_overrides,
                    )
                except Exception:
                    logger.exception("problem_tracker: prefetch rebuild failed for %s enc=%d", cpmrn, encounter)
        except Exception:
            logger.exception("problem_tracker: screener flag eval failed for %s enc=%d", cpmrn, encounter)

    # ── Fix 5: Load prior reasoning fingerprints ──────────────────────────────
    try:
        fingerprint_block = _build_fingerprint_block(cpmrn, encounter, db)
    except Exception:
        logger.exception("problem_tracker: fingerprint load failed for %s enc=%d", cpmrn, encounter)
        fingerprint_block = ""

    # ── Phase A: delta attention header ──────────────────────────────────────
    # Runs first so _delta_cats is available for the protocol engine below.
    _delta_header = ""
    _delta_cats: "set[str] | None" = None
    if delta:
        try:
            from tools.radar_sync.delta_scope import delta_summary_line, relevant_categories as _rel_cats
            _delta_header = delta_summary_line(delta)
            _delta_cats = _rel_cats(delta)
            logger.info(
                "problem_tracker: delta-scope categories=%s for %s enc=%d",
                sorted(_delta_cats) if _delta_cats != {"*"} else ["*"],
                cpmrn, encounter,
            )
        except Exception:
            logger.exception("problem_tracker: delta_scope header failed for %s enc=%d", cpmrn, encounter)

    # ── Unified protocol engine — guidance + context gate ─────────────────────
    # Loads all protocols once, then derives:
    #   _category_block  — guidance text injected into system prompt
    #   gate_block       — permissive context gate injected into user message
    # Replaces the separate _load_monitoring_protocols / _match_protocols /
    # _build_gate_block inline code AND the clinical_rule_blocks import.
    try:
        from tools.radar_sync.protocol_engine import (
            load_protocols as _load_protocols,
            build_injection as _build_injection,
        )
        _all_protocols = _load_protocols(db)
        _category_block, gate_block = _build_injection(
            _all_protocols, problems, prefetch_block, _delta_cats,
            cpmrn, encounter, db, snapshot_at,
        )
    except Exception:
        logger.exception("problem_tracker: protocol_engine failed for %s enc=%d — continuing without guidance/gate", cpmrn, encounter)
        _all_protocols = []
        _category_block = ""
        gate_block = ""

    # Build the stored state summary for the model
    stored_names = [
        d["problem_name"]
        for d in db["patient_problems"].find(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"problem_name": 1},
        )
    ]
    new_names = [p["name"] for p in problems if p["name"] not in stored_names]

    problem_block = "\n".join(
        f"  {i+1}. {p['name']} [{p.get('status','?').upper()}]"
        + (f" (secondary to: {p['cause']})" if p.get("cause") else "")
        + f"\n     Current state: {p.get('current_state', 'not specified')}\n"
        f"     Management: {p.get('management', 'not specified')}"
        for i, p in enumerate(problems)
    )

    new_block = (
        f"\nNEW problems (no prior state): {', '.join(new_names)}"
        if new_names else "\nAll problems have prior state."
    )

    context_overrides = _get_clinical_context_overrides(problems, db)

    try:
        coexisting_block = _build_coexisting_block(cpmrn, encounter, db)
    except Exception:
        logger.exception("problem_tracker: coexisting block failed for %s enc=%d", cpmrn, encounter)
        coexisting_block = ""

    # NOTE: the Pass-1 screener flag is handled upstream by evaluate_screener_flag()
    # (a focused, separate call). Any confirmed-new problem is already in `problems`,
    # so the main loop assesses it like any other — no in-prompt screener block here.

    # ── Lab alert rules — dynamic system prompt injection ─────────────────────
    # Load all rules from GCS, filter to labs present in this patient's prefetch
    # data, and append as a LAB ALERT FLOORS block to the system prompt.
    try:
        from tools.radar_sync.lab_alert_rules import (
            filter_rules_for_patient as _filter_lab_rules,
            format_prompt_block as _format_lab_block,
            load_rules as _load_lab_rules,
        )
        _all_lab_rules = _load_lab_rules(db)
        _patient_lab_rules = _filter_lab_rules(_all_lab_rules, prefetch_block)
        _lab_alert_block = _format_lab_block(_patient_lab_rules)
        if _patient_lab_rules:
            logger.info(
                "problem_tracker: lab_alert_rules — %d/%d rules matched for %s enc=%d (%s)",
                len(_patient_lab_rules), len(_all_lab_rules),
                cpmrn, encounter,
                ", ".join(r.get("lab", "?") for r in _patient_lab_rules),
            )
    except Exception:
        logger.exception("problem_tracker: lab_alert_rules injection failed for %s enc=%d", cpmrn, encounter)
        _lab_alert_block = ""

    # ── Symptom alert rules — dynamic system prompt injection ─────────────────
    # Load per-symptom objective criteria from GCS, filter to problems this patient
    # actually has, and append as a SYMPTOM ALERT CRITERIA block.
    try:
        from tools.radar_sync.symptom_alert_rules import (
            filter_rules_for_patient as _filter_symptom_rules,
            format_prompt_block as _format_symptom_block,
            load_rules as _load_symptom_rules,
        )
        _all_symptom_rules = _load_symptom_rules(db)
        _patient_symptom_rules = _filter_symptom_rules(_all_symptom_rules, problems)
        _symptom_alert_block = _format_symptom_block(_patient_symptom_rules)
        if _patient_symptom_rules:
            logger.info(
                "problem_tracker: symptom_alert_rules — %d/%d rules matched for %s enc=%d (%s)",
                len(_patient_symptom_rules), len(_all_symptom_rules),
                cpmrn, encounter,
                ", ".join(r.get("problem", "?") for r in _patient_symptom_rules),
            )
    except Exception:
        logger.exception("problem_tracker: symptom_alert_rules injection failed for %s enc=%d", cpmrn, encounter)
        _symptom_alert_block = ""

    system_prompt = (
        _SYSTEM
        + ("\n\n" + _category_block if _category_block else "")
        + ("\n\n" + _lab_alert_block if _lab_alert_block else "")
        + ("\n\n" + _symptom_alert_block if _symptom_alert_block else "")
    )

    # Build forced follow-up context block if scheduler forced this run
    forced_block = ""
    if focus:
        lines = [
            "== FORCED FOLLOW-UP ==",
            "The scheduler has forced this run because the following planned rechecks are now overdue.",
            "For each item below, retrieve the latest value and assess whether it has normalised,",
            "is improving, or remains abnormal. Update the problem status and next_check accordingly.",
            "",
        ]
        for f in focus:
            label = f.get("label") or f.get("key") or f.get("type", "parameter")
            lines.append(f"  • {f['problem_name']} — overdue {f['type']} recheck: {label}")
        forced_block = "\n".join(lines)
        logger.info(
            "problem_tracker: forced follow-up block injected for %s enc=%d — %d overdue item(s)",
            cpmrn, encounter, len(focus),
        )

    # ── Clinical timeline ──────────────────────────────────────────────────────
    _timeline = clinical_timeline if clinical_timeline else empty_timeline()
    _timeline_rendered, _timeline_to_evict, _fold_instruction = render_for_tracker(
        _timeline, snapshot_at,
    )

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n"
        f"Snapshot time: {snapshot_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        + (f"{_delta_header}\n\n" if _delta_header else "")
        + (f"{forced_block}\n\n" if forced_block else "")
        + (f"CLINICAL CONTEXT RULES FOR THIS PATIENT:\n{context_overrides}\n\n"
           if context_overrides else "")
        + (f"{coexisting_block}\n\n" if coexisting_block else "")
        + (f"{prefetch_block}\n\n" if prefetch_block else "")
        + (f"{fingerprint_block}\n\n" if fingerprint_block else "")
        + (f"{gate_block}\n\n" if gate_block else "")
        + f"{_timeline_rendered}\n\n"
        + (f"{_fold_instruction}\n\n" if _fold_instruction else "")
        + f"Current problems from summary:\n{problem_block}\n{new_block}\n\n"
        + "Review each problem. The pre-fetched context above is your primary source — "
        + "only call tools for data not listed there. Call set_all_assessments when done."
    )

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    final_assessments: list[dict] = []
    _raw_timeline_updates: list[dict] = []
    _raw_prior_course: str | None = None

    for round_num in range(_MAX_TOOL_ROUNDS):
        tracer.start_round(round_num)
        try:
            force = round_num < _MAX_TOOL_ROUNDS - 1
            resp = client.create_message(
                messages=messages,
                tools=_TOOLS,
                system=system_prompt,
                max_tokens=16000,  # must exceed thinking_budget (8000) + tool call output
                force_tool=force,
                thinking_budget=_THINKING_BUDGET,
            )
        except Exception:
            logger.exception("problem_tracker: LLM call failed on round %d for %s", round_num, cpmrn)
            tracer.end_round()
            break

        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.thinking_tokens)

        assistant_parts: list[dict] = []
        tool_calls: list[dict] = []

        for block in resp.content:
            if block.type == "thinking":
                tracer.log_thinking(block.text)
            elif block.type == "tool_use":
                tool_calls.append(block)
                tracer.log_tool_call(block.name, block.input)
                part: dict = {
                    "type": "tool_use",
                    "name": block.name,
                    "input": block.input,
                    "id": block.id,
                }
                if block.thought_signature:
                    part["thought_signature"] = block.thought_signature
                assistant_parts.append(part)
            else:
                tracer.log_text(block.text)
                assistant_parts.append({"type": "text", "text": block.text})

        if assistant_parts:
            messages.append({"role": "assistant", "content": assistant_parts})

        if not tool_calls:
            tracer.end_round()
            break

        tool_results: list[dict] = []
        _raw_timeline_updates: list[dict] = []
        _raw_prior_course: str | None = None
        for tc in tool_calls:
            if tc.name == "set_all_assessments":
                raw_assessments = tc.input.get("assessments", [])
                # Collect timeline fields from the tool input
                _raw_timeline_updates = tc.input.get("timeline_updates") or []
                _emitted_prior = tc.input.get("prior_course")
                _raw_prior_course = _emitted_prior if isinstance(_emitted_prior, str) else None
                # Resolve cited_note_indices → structured citation objects
                for a in raw_assessments:
                    indices = a.pop("cited_note_indices", None) or []
                    a["cited_notes"] = [
                        {
                            "timestamp": session_chunks[i].note_time,
                            "note_type": session_chunks[i].note_type,
                            "author":    session_chunks[i].author,
                            "quote":     session_chunks[i].text[:200].strip(),
                        }
                        for i in indices
                        if isinstance(i, int) and 0 <= i < len(session_chunks)
                    ]
                final_assessments = raw_assessments
                logger.info(
                    "problem_tracker: assessments for %s: %s",
                    cpmrn,
                    [(a.get("problem_name"), a.get("clinical_status"), a.get("should_alert")) for a in final_assessments if a.get("problem_name")],
                )
                tool_results.append({
                    "type": "tool_result", "name": tc.name, "id": tc.id,
                    "content": "Assessments recorded.",
                })
            else:
                result_text = _run_tool(tc.name, tc.input, cpmrn, encounter, db, session_chunks)
                tracer.log_tool_result(tc.name, result_text)
                tool_results.append({
                    "type": "tool_result", "name": tc.name, "id": tc.id,
                    "content": result_text,
                })

        # end_round AFTER tool results are logged so they appear in the trace
        tracer.end_round()

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if final_assessments:
            break

    # ── Persist + alert ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    alerts_sent: list[str] = []
    alerts_suppressed: list[str] = []

    if not final_assessments:
        logger.warning("problem_tracker: no assessments returned for %s — skipping upsert", cpmrn)
        tracer.save(final_output={"error": "no_assessments"})
        return {"problem_tracker": "no_assessments", "clinical_timeline": _timeline}

    # Phase 1 — evaluate eligibility, upsert all problems, collect what needs alerting.
    # Alerting is deferred so all problems for this patient go in one batched message.
    to_alert: list[tuple[dict, str]] = []   # (assessment, alert_id)
    _lab_lag = _compute_lab_upload_lag(delta)

    # Audit dict — one entry per assessed problem, returned for dashboard run-audit table.
    _problem_audit: dict[str, dict] = {}

    # Pre-compute vital staleness once for the whole batch.
    # Used by the hard suppression gate below — avoids a DB call per problem.
    _vital_is_stale = False
    _latest_vt = None
    try:
        from tools.radar_sync.status_classifier import _get_latest_vital_ts
        _latest_vt = _get_latest_vital_ts(cpmrn, encounter)
        if _latest_vt is not None and snapshot_at is not None:
            _snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
            _vital_is_stale = (_snap - _latest_vt).total_seconds() / 3600 > _VITAL_STALENESS_HOURS
    except Exception:
        logger.exception("problem_tracker: vital staleness pre-check failed for %s enc=%d", cpmrn, encounter)

    for assessment in final_assessments:
        problem_name = assessment.get("problem_name", "")
        if not problem_name:
            logger.warning(
                "problem_tracker: skipping malformed assessment (no problem_name) for %s enc=%d — keys=%s",
                cpmrn, encounter, list(assessment.keys()),
            )
            continue
        should_alert = assessment.get("should_alert", False)
        will_alert   = False
        alert_id     = ""

        _problem_audit[problem_name] = {
            "problem_name":    problem_name,
            "clinical_status": assessment.get("clinical_status", "stable"),
            "model_should_alert": should_alert,
            "alert_sent":      False,
            "suppression_rule": "none",
            "alert_reason":    assessment.get("alert_reason", ""),
            "tracker_reasoning": assessment.get("addressed_evidence", "") or assessment.get("alert_reason", ""),
            "next_check":      assessment.get("next_check"),
        }

        # Hard gate: being_addressed=True must always mean should_alert=False.
        # If the model set both (it sometimes does for "critical" acute events),
        # override here — alerting on something already being actively managed
        # adds no value and degrades trust in the pipeline.
        if should_alert and assessment.get("being_addressed", False):
            should_alert = False
            logger.warning(
                "problem_tracker: alert suppressed for '%s' %s enc=%d — "
                "model set being_addressed=True but also should_alert=True (rule violation); "
                "suppressing as problem is already being managed",
                problem_name, cpmrn, encounter,
            )
            alerts_suppressed.append(problem_name)
            _problem_audit[problem_name]["suppression_rule"] = "being_addressed"
            _upsert_problem(cpmrn, encounter, assessment, now, False, db)
            continue

        if should_alert:
            clinical_status = assessment.get("clinical_status", "stable")

            # ── Gate 0: Permissive-window suppression ─────────────────────────
            # If the model set verdict=permissive_active, suppress regardless of
            # other rules. This gate runs first and is the only one that can
            # silence a permissive-window problem.
            _gate = assessment.get("context_gate") or {}
            _gate_verdict = _gate.get("verdict", "")
            if _gate_verdict == "permissive_active":
                alerts_suppressed.append(problem_name)
                _problem_audit[problem_name]["suppression_rule"] = f"permissive_window:{_gate.get('scenario', '?')}"
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d "
                    "(permissive_active — scenario=%s)",
                    problem_name, cpmrn, encounter, _gate.get("scenario", "?"),
                )
                # Write permissive-window suppression event to BigQuery
                try:
                    import sys as _sys
                    from pathlib import Path as _Path
                    _root = _Path(__file__).resolve().parents[2]
                    for _p in [str(_root / "app"), str(_root)]:
                        if _p not in _sys.path:
                            _sys.path.insert(0, _p)
                    from backend.services.bq_store import get_bq_store
                    get_bq_store().insert_suppressed_event({
                        "CPMRN":              cpmrn,
                        "encounter":          encounter,
                        "problem_name":       problem_name,
                        "suppressed_at":      now,
                        "suppression_reason": "permissive_window",
                        "gate_scenario":      _gate.get("scenario", ""),
                        "gate_valid_until":   str(_gate.get("valid_until", "")),
                    })
                except Exception:
                    logger.exception(
                        "problem_tracker: permissive suppression event write failed for '%s' %s",
                        problem_name, cpmrn,
                    )
                _upsert_problem(cpmrn, encounter, assessment, now, False, db)
                continue  # skip remaining gates for this problem

            # ── Gate 0b: Code-side GCS stability gate ─────────────────────────
            # Catches cases where the model created a new GCS problem (or renamed
            # one) and therefore skipped the protocol gate evaluation. If the
            # daysGCS vital delta over the last 6 hours is ≥ 0 (stable or
            # improving), suppress — only a negative delta (drop ≥ 2 pts) should
            # alert. This mirrors the established-low-gcs protocol's direction rule.
            _gcs_protocol_keywords = ("gcs", "low gcs", "reduced gcs", "altered consciousness",
                                      "neurological deterioration", "coma", "encephalopathy")
            _problem_lc = problem_name.lower()
            _gate_not_active = _gate_verdict not in ("permissive_active",)
            # Also trigger for any problem where the model set next_check.vital_key="GCS",
            # e.g. "Acute Intracranial Hemorrhage" tracked via GCS — problem name won't
            # contain GCS keywords but the alert is still GCS-driven.
            _is_gcs_driven = (assessment.get("next_check") or {}).get("vital_key", "").upper() == "GCS"
            if _gate_not_active and (any(kw in _problem_lc for kw in _gcs_protocol_keywords) or _is_gcs_driven):
                try:
                    from tools.radar_sync.status_classifier import _get_gcs_6h_delta
                    _gcs_delta = _get_gcs_6h_delta(cpmrn, encounter)
                    if _gcs_delta is not None and _gcs_delta >= 0:
                        alerts_suppressed.append(problem_name)
                        _problem_audit[problem_name]["suppression_rule"] = f"gcs_stability:delta={_gcs_delta:+d}"
                        logger.info(
                            "problem_tracker: alert suppressed for '%s' %s enc=%d "
                            "(code-side GCS gate — 6h delta=%+d, stable/improving)",
                            problem_name, cpmrn, encounter, _gcs_delta,
                        )
                        assessment["context_gate"] = {
                            "verdict": "permissive_active",
                            "scenario": "established_neurological_injury",
                            "rationale": f"Code-side gate: GCS 6h delta={_gcs_delta:+d} (stable/improving)",
                        }
                        _upsert_problem(cpmrn, encounter, assessment, now, False, db)
                        continue
                except Exception:
                    logger.exception(
                        "problem_tracker: code-side GCS gate failed for '%s' %s enc=%d — continuing",
                        problem_name, cpmrn, encounter,
                    )

            if clinical_status not in ("worsening", "critical"):
                alerts_suppressed.append(problem_name)
                _problem_audit[problem_name]["suppression_rule"] = f"below_floor:{clinical_status}"
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d "
                    "(status=%s — only worsening/critical may alert)",
                    problem_name, cpmrn, encounter, clinical_status,
                )
            elif _should_suppress_alert(cpmrn, encounter, problem_name, db):
                alerts_suppressed.append(problem_name)
                _problem_audit[problem_name]["suppression_rule"] = "cooldown"
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d (cooldown)",
                    problem_name, cpmrn, encounter,
                )
            elif _all_cited_notes_stale(assessment, snapshot_at):
                alerts_suppressed.append(problem_name)
                _problem_audit[problem_name]["suppression_rule"] = "stale_notes"
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d "
                    "(all cited notes >%dh old — chronic audit issue, not urgent alert)",
                    problem_name, cpmrn, encounter, _CITED_NOTE_STALENESS_HOURS,
                )
            elif _vital_is_stale and assessment.get("next_check", {}).get("type") == "vital":
                # Hard gate: next_check is a vital AND vitals are stale — the model
                # was tracking a vital-sign problem (tachycardia, hypotension, etc.)
                # whose driving data is too old to represent current haemodynamic state.
                alerts_suppressed.append(problem_name)
                _problem_audit[problem_name]["suppression_rule"] = "stale_vitals"
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d "
                    "(stale vitals — next_check.type=vital, vitals >%dh old)",
                    problem_name, cpmrn, encounter, _VITAL_STALENESS_HOURS,
                )
            elif assessment.get("next_check", {}).get("type") == "lab":
                # Hard gate: if the lab driving this alert is too old, suppress.
                # Threshold is per-lab from _staleness_overrides (GCS-backed), falling back to _LAB_STALENESS_HOURS.
                nc_lab = (assessment.get("next_check") or {}).get("lab_name") or (assessment.get("next_check") or {}).get("key", "")
                _lab_is_stale = False
                _lab_staleness_limit = _staleness_overrides.get(nc_lab.lower(), _LAB_STALENESS_HOURS) if nc_lab else _LAB_STALENESS_HOURS
                if nc_lab and snapshot_at is not None:
                    try:
                        from tools.radar_sync.status_classifier import _get_latest_lab_ts
                        _latest_lt = _get_latest_lab_ts(cpmrn, encounter, nc_lab)
                        if _latest_lt is not None:
                            _snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
                            _lab_is_stale = (_snap - _latest_lt).total_seconds() / 3600 > _lab_staleness_limit
                    except Exception:
                        logger.exception("problem_tracker: lab staleness check failed for '%s' %s enc=%d", problem_name, cpmrn, encounter)
                if _lab_is_stale:
                    alerts_suppressed.append(problem_name)
                    _problem_audit[problem_name]["suppression_rule"] = f"stale_lab:{nc_lab}"
                    logger.info(
                        "problem_tracker: alert suppressed for '%s' %s enc=%d "
                        "(stale lab '%s' — >%dh old, limit %dh)",
                        problem_name, cpmrn, encounter, nc_lab, _lab_staleness_limit, _lab_staleness_limit,
                    )
                else:
                    # ── Pre-send lab value cross-check ────────────────────────
                    _lab_parse = _extract_alert_lab_value(assessment.get("alert_title") or "")
                    if _lab_parse is not None:
                        _lv_name, _lv_claimed = _lab_parse
                        try:
                            from tools.radar_sync.status_classifier import _lookup_lab_value_in_snapshot
                            _lv_actual = _lookup_lab_value_in_snapshot(cpmrn, encounter, _lv_name)
                            if _lv_actual is not None:
                                _lv_tol = max(0.25 * _lv_actual, 3.0)
                                if abs(_lv_claimed - _lv_actual) > _lv_tol:
                                    alerts_suppressed.append(problem_name)
                                    _problem_audit[problem_name]["suppression_rule"] = f"lab_value_mismatch:{_lv_name}"
                                    logger.warning(
                                        "problem_tracker: alert suppressed for '%s' %s enc=%d — "
                                        "lab value mismatch: title claims %s=%.1f, snapshot shows %.1f",
                                        problem_name, cpmrn, encounter, _lv_name, _lv_claimed, _lv_actual,
                                    )
                                    _upsert_problem(cpmrn, encounter, assessment, now, False, db)
                                    continue
                        except Exception:
                            logger.exception(
                                "problem_tracker: lab value cross-check failed for '%s' %s enc=%d — continuing",
                                problem_name, cpmrn, encounter,
                            )
                    alert_id   = str(_uuid4())
                    will_alert = True
                    _problem_audit[problem_name]["alert_sent"] = True
                    _problem_audit[problem_name]["suppression_rule"] = "alerted"
                    assessment["_snapshot_at"] = snapshot_at.isoformat() if snapshot_at else None
                    if _latest_vt is not None and snapshot_at is not None:
                        _snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
                        assessment["_vital_age_hours"] = round((_snap - _latest_vt).total_seconds() / 3600, 1)
                    if _lab_lag:
                        assessment["_lab_upload_lag"] = _lab_lag
                    to_alert.append((assessment, alert_id))
                    alerts_sent.append(problem_name)
            else:
                # ── Pre-send vital freshness check ─────────────────────────────
                # If the alert is driven by a vital-sign next_check, fetch the
                # latest reading live from Radar. If a newer reading arrived after
                # the snapshot was taken AND it shows improvement, the alert is
                # already stale — suppress and let the next hourly run decide.
                _nc = assessment.get("next_check") or {}
                _vk = _nc.get("vital_key", "") if _nc.get("type") == "vital" else ""
                if _vk and _latest_vt is not None:
                    if _vital_improved_since_snapshot(cpmrn, encounter, _vk, problem_name, _latest_vt):
                        alerts_suppressed.append(problem_name)
                        _problem_audit[problem_name]["suppression_rule"] = f"improving_trend:{_vk}"
                        logger.info(
                            "problem_tracker: alert suppressed for '%s' %s enc=%d "
                            "(pre-send vital check — %s improved since snapshot)",
                            problem_name, cpmrn, encounter, _vk,
                        )
                        _upsert_problem(cpmrn, encounter, assessment, now, False, db)
                        continue

                # ── Pre-send lab value cross-check ────────────────────────────
                _lab_parse = _extract_alert_lab_value(assessment.get("alert_title") or "")
                if _lab_parse is not None:
                    _lv_name, _lv_claimed = _lab_parse
                    try:
                        from tools.radar_sync.status_classifier import _lookup_lab_value_in_snapshot
                        _lv_actual = _lookup_lab_value_in_snapshot(cpmrn, encounter, _lv_name)
                        if _lv_actual is not None:
                            _lv_tol = max(0.25 * _lv_actual, 3.0)
                            if abs(_lv_claimed - _lv_actual) > _lv_tol:
                                alerts_suppressed.append(problem_name)
                                _problem_audit[problem_name]["suppression_rule"] = f"lab_value_mismatch:{_lv_name}"
                                logger.warning(
                                    "problem_tracker: alert suppressed for '%s' %s enc=%d — "
                                    "lab value mismatch: title claims %s=%.1f, snapshot shows %.1f",
                                    problem_name, cpmrn, encounter, _lv_name, _lv_claimed, _lv_actual,
                                )
                                _upsert_problem(cpmrn, encounter, assessment, now, False, db)
                                continue
                    except Exception:
                        logger.exception(
                            "problem_tracker: lab value cross-check failed for '%s' %s enc=%d — continuing",
                            problem_name, cpmrn, encounter,
                        )
                alert_id   = str(_uuid4())
                will_alert = True
                _problem_audit[problem_name]["alert_sent"] = True
                _problem_audit[problem_name]["suppression_rule"] = "alerted"
                # Inject snapshot_at so the card builder can show timestamps in IST
                assessment["_snapshot_at"] = snapshot_at.isoformat() if snapshot_at else None
                if _latest_vt is not None and snapshot_at is not None:
                    _snap = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
                    assessment["_vital_age_hours"] = round((_snap - _latest_vt).total_seconds() / 3600, 1)
                if _lab_lag:
                    assessment["_lab_upload_lag"] = _lab_lag
                to_alert.append((assessment, alert_id))
                alerts_sent.append(problem_name)

        _upsert_problem(cpmrn, encounter, assessment, now, will_alert, db)

        # For problems where the model itself set should_alert=False, record the model reason.
        if not assessment.get("should_alert", False) and _problem_audit[problem_name]["suppression_rule"] == "none":
            _problem_audit[problem_name]["suppression_rule"] = "model_suppressed"

        # Suppression record — worsening/critical problems where the model
        # found a documented plan and therefore did NOT alert.
        clinical_status_val = assessment.get("clinical_status", "stable")
        if (
            clinical_status_val in ("worsening", "critical")
            and assessment.get("being_addressed", False)
            and not assessment.get("should_alert", False)
        ):
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _root = _Path(__file__).resolve().parents[2]
                for _p in [str(_root / "app"), str(_root)]:
                    if _p not in _sys.path:
                        _sys.path.insert(0, _p)
                from backend.services.bq_store import get_bq_store
                get_bq_store().insert_suppressed_event({
                    "CPMRN":              cpmrn,
                    "encounter":          encounter,
                    "problem_name":       problem_name,
                    "suppressed_at":      now,
                    "suppression_reason": "being_addressed",
                })
            except Exception:
                logger.exception("problem_tracker: study_suppressed_events write failed for '%s' %s", problem_name, cpmrn)

    # Phase 2 — deduplicate and send one batched message for all alerting problems.
    to_alert = _suppress_redundant_alerts(to_alert)
    for assessment, _ in to_alert:
        _maybe_append_charting_caveat(cpmrn, encounter, assessment)
        try:
            from tools.radar_sync.insulin_advice import attach_insulin_order
            attach_insulin_order(cpmrn, encounter, assessment)
            # Use insulin-recommended timing to override the generic lab_default next_check window.
            _insulin_data = assessment.get("_insulin_order") or {}
            _next_grbs_h = (_insulin_data.get("reco") or {}).get("next_grbs_after")
            if _next_grbs_h and isinstance(_next_grbs_h, (int, float)):
                try:
                    db["patient_problems"].update_one(
                        {
                            "CPMRN": cpmrn, "encounter": encounter,
                            "problem_name": assessment.get("problem_name"),
                            "next_check": {"$ne": None},
                        },
                        {"$set": {"next_check.due_after": snapshot_at + timedelta(hours=int(_next_grbs_h))}},
                    )
                    logger.info(
                        "problem_tracker: next_check.due_after set to +%dh for '%s' %s enc=%d (insulin timing)",
                        int(_next_grbs_h), assessment.get("problem_name", ""), cpmrn, encounter,
                    )
                except Exception:
                    logger.exception(
                        "problem_tracker: failed to patch next_check.due_after for '%s' %s",
                        assessment.get("problem_name", ""), cpmrn,
                    )
        except Exception:
            logger.exception(
                "problem_tracker: insulin_advice enrichment failed for '%s' %s",
                assessment.get("problem_name", ""), cpmrn,
            )
    if to_alert:
        try:
            from tools.radar_sync.chat_card_sender import (
                get_alert_recipients,
                send_batch_alert_cards,
            )
            recipients = get_alert_recipients(db)
            if recipients:
                sent = send_batch_alert_cards(
                    cpmrn, encounter, to_alert, structured_summary, recipients,
                )
                if sent:
                    logger.info(
                        "problem_tracker: batch alert sent for %s enc=%d — %s",
                        cpmrn, encounter, [p for p, _ in [(a.get("problem_name"), aid) for a, aid in to_alert]],
                    )
            else:
                # Legacy webhook fallback — still batched into one text message
                from tools.radar_sync.gchat_notifier import send_problem_alert
                cfg = db["app_settings"].find_one({"_id": "gchat_webhook"})
                if cfg and cfg.get("enabled") and cfg.get("url"):
                    for assessment, _ in to_alert:
                        send_problem_alert(
                            cpmrn, encounter, assessment, structured_summary, cfg["url"],
                        )
        except Exception:
            logger.exception("problem_tracker: batch alert failed for %s enc=%d", cpmrn, encounter)

        # BQ study records — one per problem, written after send attempt
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _root = _Path(__file__).resolve().parents[2]
            for _p in [str(_root / "app"), str(_root)]:
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
            from backend.services.bq_store import get_bq_store
            bq = get_bq_store()
            for assessment, alert_id in to_alert:
                bq.insert_alert({
                    "alert_id":     alert_id,
                    "CPMRN":        cpmrn,
                    "encounter":    encounter,
                    "problem_name": assessment.get("problem_name", ""),
                    "alert_title":        assessment.get("alert_title", ""),
                    "alert_reason":       assessment.get("alert_reason", ""),
                    "note_vs_objective":  assessment.get("note_vs_objective", ""),
                    "alerted_at":         now,
                    "match_status": "pending",
                })
        except Exception:
            logger.exception("problem_tracker: study_alerts BQ write failed for %s", cpmrn)

    # Enqueue documentation audits for any problems that match an audit protocol
    # and haven't been seen in the queue before. The BQ sweep query deduplicates
    # by queue_id so re-inserting on every run is safe.
    try:
        from tools.radar_sync.protocol_engine import match_for_audit as _match_for_audit
        from datetime import timedelta
        _audit_pairs = _match_for_audit(
            _all_protocols,
            [{"name": a["problem_name"], "clinical_status": a.get("clinical_status")} for a in final_assessments],
        )
        if _audit_pairs:
            import sys as _sys
            from pathlib import Path as _Path
            _root2 = _Path(__file__).resolve().parents[2]
            for _p in [str(_root2 / "app"), str(_root2)]:
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
            from backend.services.bq_store import get_bq_store as _get_bq_store
            _bq2 = _get_bq_store()
            for _prob_stub, _proto in _audit_pairs:
                _window_h = int(_proto["audit"].get("window_hours") or 12)
                _bq2.enqueue_documentation_audit({
                    "CPMRN":        cpmrn,
                    "encounter":    encounter,
                    "problem_name": _prob_stub["name"],
                    "protocol_id":  _proto["protocol_id"],
                    "detected_at":  now,
                    "audit_due_at": now + timedelta(hours=_window_h),
                })
    except Exception:
        logger.exception("problem_tracker: audit enqueue failed for %s enc=%d", cpmrn, encounter)

    tracer.save(final_output={
        "assessments": [(a["problem_name"], a.get("clinical_status"), a.get("should_alert")) for a in final_assessments],
        "alerts_sent":       alerts_sent,
        "alerts_suppressed": alerts_suppressed,
    })

    # Assemble updated clinical timeline from model-emitted events
    updated_timeline = apply_updates(
        timeline=_timeline,
        new_events=_raw_timeline_updates,
        emitted_prior=_raw_prior_course,
        to_evict=_timeline_to_evict,
    )

    return {
        "problem_tracker":  "ok",
        "problems_assessed": len(final_assessments),
        "alerts_sent":       alerts_sent,
        "alerts_suppressed": alerts_suppressed,
        "problem_details":   list(_problem_audit.values()),
        "clinical_timeline": updated_timeline,
    }
