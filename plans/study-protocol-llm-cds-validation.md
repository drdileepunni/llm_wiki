# Study Protocol: Prospective Validation of a ReAct LLM-Based Clinical Decision Support System for ICU Deterioration Detection in a Multi-Hospital Tele-ICU Network

**Version:** 1.0  
**Date:** 2026-05-22  
**Principal Investigator:** Dileep Unni, Cloudphysician  
**Status:** Draft — pre-submission

---

## 1. Background and Rationale

### 1.1 The problem with existing CDS tools

Clinical decision support (CDS) tools in the ICU typically fall into two categories:

1. **Rule-based early warning scores** (NEWS2, MEWS, SOFA) — transparent but insensitive to nuance; they score a single snapshot of vital signs without considering clinical context, treatment trajectory, or whether a problem is already being managed.
2. **Machine learning classifiers** — higher accuracy in controlled datasets but black-box outputs, brittle to data missingness, and unable to read free-text clinical notes where most ICU decision-making is documented.

Both approaches share a critical failure mode: **alert fatigue**. They fire without knowing whether the clinical team has already identified and addressed the problem, generating noise that erodes clinician trust over time.

### 1.2 A new paradigm: agentic reasoning with tool use

Large language models (LLMs) with extended reasoning and tool-calling capabilities enable a fundamentally different approach. Rather than passively scoring a feature vector, an LLM agent can:

- **Actively query** vital sign trends, lab trajectories, fluid balance charts, and free-text clinical notes before reaching a conclusion
- **Reason about whether a problem is being addressed** — by searching notes for documented plans and citing the relevant excerpt
- **Apply clinical context rules** — e.g., permissive hypertension targets in ischaemic stroke — which are impossible to encode in a static scoring system
- **Produce auditable, cited justifications** for every alert, naming the baseline value, current value, trend direction, and supporting note quote

### 1.3 The tele-ICU context

Cloudphysician operates a multi-hospital remote ICU network. Remote physicians simultaneously monitor patients across geographically dispersed hospitals. In this setting:

- Physical examination is unavailable — decision-making relies entirely on documented data
- Physician attention is distributed across many patients simultaneously — alert fatigue is amplified
- The "being addressed" dimension is especially critical — the remote team needs to know whether the local bedside team already has a plan before escalating remotely
- Free-text note reading is uniquely valuable — the remote physician has no verbal handover, making note synthesis a core cognitive task

This study prospectively validates an LLM-based CDS system purpose-built for this context.

---

## 2. System Description

### 2.1 Architecture

The system runs as a scheduled pipeline executing once per hour per admitted patient across all monitored hospitals. Each pipeline cycle consists of three sequential stages:

**Stage 1 — Status Classifier** (`status_classifier.py`)  
A Gemini 2.5 Flash reasoning model reviews any problems preliminarily labelled "worsening" or "critical" from the structured patient summary. It actively queries vital sign trends (≥2 consecutive readings required for vital deterioration) and lab trends before confirming or downgrading the label. This prevents single-snapshot noise from propagating to the alert stage.

**Stage 2 — Problem Tracker** (`problem_tracker.py`)  
A ReAct (Reasoning + Acting) loop that takes the verified problem list and determines, for each problem:
- Is the problem being addressed? (queries clinical notes via semantic search; requires a documented plan)
- Is a monitoring checkpoint overdue? (checks whether a previously expected result — e.g., post-transfusion Hb — has arrived)
- Should an alert fire? (only if: worsening/critical status AND not addressed AND not within the 8-hour cooldown window)

Alerts include: problem name, clinical status, baseline vs. current values, trend direction, and a verbatim quoted excerpt from the note that confirms or refutes a documented plan.

**Stage 3 — False Negative Safety Net** (`fn_detector.py`)  
A rule-based detector that computes a NEWS2 score from the latest vital signs snapshot. If an alert was suppressed by the 8-hour cooldown but NEWS2 has risen meaningfully since the last alert (≥3 points on a single parameter, or ≥6 points total), the cooldown is broken and the alert fires.

### 2.2 Tools available to the reasoning model

| Tool | Data source | Purpose |
|---|---|---|
| `get_vital_trend` | Stored hourly snapshots | HR, BP, MAP, SpO2, RR, FiO2, Temp — newest first |
| `get_lab_trend` | Lab documents from EMR | Hb, Creatinine, K, Na, Lactate, WBC, etc. |
| `get_io` | I/O chart from EMR | Per-hour intake, urine output, drain, net fluid balance |
| `query_patient_notes` | FAISS-indexed clinical notes | Semantic search for plans, treatment decisions |
| `get_problem_state` | MongoDB (prior assessment) | Prior being_addressed status, overdue next_check |
| `set_all_assessments` | — | Commits final assessment for all problems (called once) |

### 2.3 Alert output

Alerts are delivered via Google Chat webhook with:
- Patient identifier and encounter
- Problem name and clinical status
- Alert reason (baseline → current → trend)
- Cited note excerpt (timestamp, author, verbatim quote ≤200 chars)
- Direct link to patient chart

---

## 3. Study Objectives

### 3.1 Primary objective

To determine the sensitivity and specificity of the LLM-based CDS system for detecting actionable clinical deterioration events in remotely-monitored ICU patients, using clinician adjudication and high-urgency SBAR events as the reference standard.

### 3.2 Secondary objectives

1. Positive predictive value (PPV), negative predictive value (NPV), and F1 score
2. Inter-rater agreement (Cohen's κ) among adjudicating clinicians
3. Alert lead time — time from system alert to corresponding high-urgency SBAR creation
4. Alert burden reduction attributable to the "being addressed" suppression mechanism
5. Explainability rating — clinician rating of alert reasons on a structured scale

---

## 4. Study Design

Prospective observational cohort study.  
The system runs passively alongside standard clinical care. No patient management decisions are altered by this study. Clinician response to alerts follows standard workflow.

---

## 5. Study Setting and Population

### 5.1 Setting

All ICU and HDU units across the Cloudphysician multi-hospital network, monitored during the study period.

### 5.2 Inclusion criteria

- All patients admitted to a monitored ICU or HDU during the study period
- Patient encounter active at any point during the 48-hour study window

### 5.3 Exclusion criteria

- NICU/PICU patients (different physiology and alert thresholds)
- Patients whose data pipeline failed to sync during the study window (data completeness <50% of expected hourly snapshots)

---

## 6. Study Period

**Data collection window:** Monday 2026-05-25 00:00 IST through Tuesday 2026-05-27 00:00 IST (48 hours)

**Adjudication window:** 2026-05-27 through 2026-05-30 (3 days post-collection)

**Analysis and write-up:** 2026-05-30 onwards

---

## 7. Reference Standard and Outcome Definitions

### 7.1 Alert classification (TP vs FP) — manual adjudication

All system alerts generated during the study window will be independently reviewed by three clinicians (principal investigator + two co-investigators). Each reviewer assesses the alert in the context of the full patient chart at the time of the alert and records a binary verdict:

- **True Positive (TP):** The alert identified a genuine clinical problem requiring action that was not fully managed at the time of the alert. The concern was clinically valid and the alert would have been appropriate to act upon.
- **False Positive (FP):** The alert fired but the problem was either already fully addressed, not clinically significant, or factually incorrect based on the available data.

Each alert is reviewed independently. Agreement is quantified using Cohen's κ. Discordant verdicts are resolved by majority vote (2-of-3).

### 7.2 Event detection (FN vs TN) — SBAR reference standard

High-urgency SBAR events created during the study window are exported from the analytics database (`latest_sbar_fact`, `urgency = 'High'`). These represent clinical events independently identified and flagged by bedside nursing or automated document scanning as requiring urgent physician review.

**Matching algorithm:**  
A system alert is matched to a high-urgency SBAR if:
- Same patient (CPMRN) and encounter
- Alert fired within the window: [SBAR creation time − 6 hours, SBAR creation time + 2 hours]

This 8-hour asymmetric window reflects the primary goal of early warning (alert should precede SBAR) while allowing 2 hours for concurrent detection.

**Classification using the matched dataset:**

| | Alert fired | No alert |
|---|---|---|
| **High SBAR exists** | TP (confirmed by adjudication) | FN |
| **No High SBAR** | FP (confirmed by adjudication) | TN |

**False Negative (FN):** A high-urgency SBAR with no matching system alert within the window. These represent deterioration events the system failed to detect.

**True Negative (TN):** Patient-hours where no alert fired and no high-urgency SBAR was created. TN is defined at the patient-encounter level: an encounter where the system was silent throughout the study window and no high-urgency SBAR was created.

### 7.3 Scope restriction

Only high-urgency SBARs from clinically relevant modules are included in the reference standard: `vitals`, `summary`, `documents` (labs), `intake-output`. SBARs from `orders`, `mar`, and `notes` modules are excluded as they do not represent physiological deterioration events.

---

## 8. Data Collection and Storage

### 8.1 Study data in MongoDB

All study data will be stored in the following MongoDB collections:

**`study_alerts`** — one document per system alert fired during the study window:
```
{
  study_id: "2026-05-25",
  alert_id: ObjectId,
  CPMRN: str,
  encounter: int,
  problem_name: str,
  clinical_status: str,
  alert_reason: str,
  cited_notes: [...],
  alerted_at: datetime,
  hospital_id: str,
  unit_name: str
}
```

**`study_adjudications`** — one document per reviewer per alert:
```
{
  alert_id: ObjectId (ref study_alerts),
  reviewer_email: str,
  verdict: "TP" | "FP",
  reviewer_notes: str,
  reviewed_at: datetime
}
```

**`study_sbar_import`** — high-urgency SBARs imported from BigQuery for the study window:
```
{
  sbar_id: str,
  CPMRN: str,
  encounter: str,
  hospital_name: str,
  unit_name: str,
  urgency: "High",
  issues: str,
  module: str,
  create_date_time: datetime,
  is_reviewed: bool,
  reviewer_name: str,
  action: str
}
```

**`study_results`** — final joined 2x2 matrix per patient encounter:
```
{
  CPMRN: str,
  encounter: int,
  alerts_fired: int,
  high_sbars: int,
  matched_tps: int,
  unmatched_fps: int,
  unmatched_fns: int,
  adjudicated_verdicts: {...}
}
```

### 8.2 SBAR export script

At the close of the study window (2026-05-27 00:00 IST), a script will:
1. Query BigQuery for all high-urgency SBARs in the study period filtered to relevant modules
2. Import and store them in `study_sbar_import` in MongoDB
3. Run the matching algorithm to populate `study_results`

---

## 9. Statistical Analysis

### 9.1 Primary analysis

From the final 2x2 matrix:

- **Sensitivity** = TP / (TP + FN)
- **Specificity** = TN / (TN + FP)
- **PPV** = TP / (TP + FP)
- **NPV** = TN / (TN + FN)
- **F1** = 2 × (PPV × Sensitivity) / (PPV + Sensitivity)
- **95% confidence intervals** using the Wilson score method

### 9.2 Inter-rater agreement

Cohen's κ calculated for each pair of reviewers and for all three collectively (Fleiss' κ). κ ≥ 0.6 is considered acceptable; κ ≥ 0.8 is considered strong.

### 9.3 Secondary analyses

- **Lead time distribution** — for matched TPs only: histogram of (SBAR creation time − alert time). Median and IQR reported. Negative lead time means the system alerted after the SBAR was created.
- **Suppression analysis** — among worsening/critical problems where `being_addressed = True` and alert was suppressed: what % had a corresponding high-urgency SBAR? (This quantifies the alert burden reduction without false negative cost.)
- **Explainability sub-study** — adjudicators rate each alert_reason on a 3-point scale: (1) clear and actionable, (2) understandable but incomplete, (3) unclear or misleading. Proportion rated ≥2 reported.

### 9.4 Unit of analysis

Primary analysis unit: **the individual alert** (not the patient encounter). Sensitivity and FN calculations use patient-encounter as the unit.

---

## 10. Ethical Considerations

### 10.1 Study type

Prospective observational study. The CDS system runs passively; no patient management decisions are altered by participation. All patients receive standard care regardless of study status.

### 10.2 Data handling

- All data stored in institutional infrastructure (MongoDB, GCP) under existing data governance agreements
- No patient data is exported outside the institutional environment for this study
- Reviewer identities are stored but not disclosed in publications — reviewers are identified by role only (Reviewer A, B, C)
- No PHI in the published protocol or manuscript; patient identifiers are pseudonymised (CPMRN used internally, not in publications)

### 10.3 Ethics committee submission

This protocol will be submitted to the relevant Institutional Ethics Committee (IEC) prior to data collection. Given the observational, non-interventional design with no patient contact, expedited review is anticipated.

---

## 11. Limitations

1. **Two-day study window** — small sample size; results may not generalise across seasonal or epidemiological variation in case mix
2. **Single operator** — all hospitals are managed by one group of physicians; generalisability to other tele-ICU networks requires external validation
3. **SBAR as ground truth** — high-urgency SBARs capture events that reached administrative documentation, not all true deterioration events; some FNs may represent real misses, others may represent events managed before SBAR creation
4. **Temporal matching uncertainty** — the ±window for alert-SBAR matching is operationally motivated but arbitrary; sensitivity analyses with different windows should be reported
5. **ABG/RFT dominance** — high-urgency SBARs are predominantly ABG and RFT results; sensitivity for vital-sign-based deterioration (haemodynamic instability, respiratory failure not captured by labs) may be underestimated

---

## 12. Expected Outputs

1. Sensitivity, specificity, PPV, NPV, F1 score with 95% CIs
2. Inter-rater Kappa (adjudication reliability)
3. Lead time distribution (early warning characterisation)
4. Alert suppression analysis (quantifying "being addressed" benefit)
5. Explainability sub-study results

---

## 13. Publication Plan

Target journals (in priority order):
1. *npj Digital Medicine* (Nature Portfolio) — highest visibility for LLM-in-healthcare work
2. *Critical Care Medicine* — target audience is ICU clinicians
3. *JAMIA* — informatics-focused audience
4. *NEJM AI* — highest impact if results are strong

Pre-submission: post to medRxiv as preprint on protocol submission.

---

## 14. Infrastructure Checklist (pre-study)

- [ ] `study_alerts` collection created and problem tracker writing to it during study window
- [ ] `study_adjudications` collection created with adjudication UI or structured form
- [ ] BigQuery SBAR export script tested and ready
- [ ] MongoDB matching script tested on synthetic data
- [ ] All three adjudicators briefed on TP/FP definitions
- [ ] NEWS2 baseline recorded at time of each alert (for FN safety net comparison)
- [ ] Study window confirmed: 2026-05-25 00:00 IST — 2026-05-27 00:00 IST

---

*Protocol version 1.0 — Dileep Unni, Cloudphysician, 2026-05-22*
