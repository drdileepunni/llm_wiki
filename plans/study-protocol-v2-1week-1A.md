# Study Protocol
## Prospective Validation of a ReAct LLM-Based Clinical Decision Support System in a Tele-ICU Setting

**Protocol version:** 2.0  
**Date:** 22 May 2026  
**Principal Investigator:** Dr. Dileep Unni, Cloudphysician  
**Study duration:** 7 days  
**Study unit:** Workspace 1A (multi-hospital tele-ICU, ~30 admitted patients)  
**Status:** Pending IEC approval and compute budget approval

---

## 1. Background and Rationale

### 1.1 The problem with existing ICU alert systems

Clinical decision support (CDS) tools in the ICU broadly fall into two categories:

1. **Rule-based early warning scores** (NEWS2, MEWS, SOFA) — transparent but insensitive to clinical context. They score a single snapshot of vital signs without knowing whether a problem is already being managed, resulting in high false-positive rates and alert fatigue.

2. **Machine learning classifiers** — higher aggregate accuracy in controlled retrospective datasets, but typically black-box outputs, brittle to data missingness, and unable to reason over free-text clinical notes where most ICU decision-making is documented.

Both share a critical failure mode: **alert fatigue**. They fire without knowing whether the clinical team has already identified and addressed the problem. In a multi-hospital tele-ICU, where a single remote physician monitors dozens of patients simultaneously, unfiltered alerts are unworkable.

### 1.2 A new approach: agentic LLM reasoning

Large language models (LLMs) with extended reasoning and tool-calling (ReAct loop) enable a qualitatively different CDS paradigm. Rather than passively scoring a feature vector, an LLM agent actively queries the patient record before reaching a conclusion:

- Retrieves vital sign trends, lab trajectories, fluid balance, and free-text clinical notes
- Determines whether each problem is already documented as being managed (citing the relevant note excerpt verbatim)
- Applies patient-specific clinical context rules (e.g., permissive hypertension targets in ischaemic stroke, accepted low SpO₂ in a weaning protocol)
- Produces auditable, human-readable justifications for every alert it fires or suppresses

This approach directly addresses alert fatigue: the system remains silent when a problem is already addressed, and speaks only when it finds a genuine unresolved concern.

### 1.3 The tele-ICU context

Cloudphysician operates a multi-hospital remote ICU monitoring network. Remote physicians manage patients across geographically dispersed hospitals without direct physical access. In this setting:

- Decision-making relies entirely on documented data and real-time vital signs
- A single physician monitors many patients simultaneously — alert volume directly impacts patient safety
- The "being addressed" dimension is clinically essential — the remote team must know whether the bedside team already has a plan before escalating
- Free-text note synthesis is a core cognitive task — the remote physician receives no verbal handover

This study prospectively validates the first LLM-based CDS system purpose-built for this tele-ICU context, measuring its diagnostic accuracy against an independent ground-truth reference standard.

---

## 2. System Description

### 2.1 Pipeline architecture

The system executes once per hour per admitted patient. Each cycle runs three sequential stages:

**Stage 1 — Status Classifier**  
A Gemini 2.5 Flash reasoning model with extended thinking (budget: 8,000 thinking tokens) reviews all problems preliminarily labelled "worsening" or "critical." It actively queries vital sign trends (requiring ≥2 consecutive deteriorating readings) and lab trends before confirming or downgrading the label. This prevents single-snapshot noise from propagating to the alert stage.

**Stage 2 — Problem Tracker (ReAct loop)**  
A second Gemini 2.5 Flash reasoning model takes the verified problem list and determines, for each active problem:
- Is the problem being addressed? (queries clinical notes via semantic FAISS search; requires a documented plan with verbatim citation)
- Is a monitoring checkpoint overdue? (e.g., post-transfusion Hb recheck due but not yet arrived)
- Should an alert fire? (only when: confirmed worsening/critical AND not addressed AND >8 hours since last alert for this problem)

Each fired alert includes: problem name, clinical status, baseline vs. current values, trend direction, verbatim cited note excerpt, and direct chart link.

**Stage 3 — False Negative Safety Net**  
A rule-based NEWS2 detector. If a problem is within its 8-hour cooldown window but NEWS2 has deteriorated significantly since the last alert (≥3 points on any single parameter or ≥6 total), the cooldown is overridden and the alert re-fires.

### 2.2 Model and infrastructure

| Component | Specification |
|---|---|
| Reasoning model | Gemini 2.5 Flash (Google AI) |
| Thinking budget | 8,000 tokens per ReAct call |
| Deployment | Python / FastAPI, self-hosted |
| Data storage | MongoDB (patient contexts, alerts, study data) |
| Note indexing | FAISS vector index, rebuilt per snapshot |
| Alert delivery | Google Chat webhook |
| Study data pipeline | Hourly SBAR sync from BigQuery; LLM semantic matching; automated metrics |

### 2.3 Alert suppression mechanism

The "being addressed" suppression is the key innovation. When the model finds a documented plan in recent clinical notes, it suppresses the alert and logs a suppression event. A secondary endpoint of this study measures what fraction of suppressed events correspond to a real high-urgency SBAR (i.e., how often the suppression was clinically correct).

---

## 3. Study Objectives

### 3.1 Primary objectives

1. **Sensitivity** — proportion of high-urgency clinical events (as defined by the SBAR reference standard) for which the system generated at least one alert in the preceding 6-hour window
2. **Specificity** — proportion of patient-encounters with no high-urgency SBAR for which the system generated no alert during the study period

### 3.2 Secondary objectives

1. Positive predictive value (PPV), negative predictive value (NPV), F1 score
2. **Alert lead time** — time between system alert and corresponding high-urgency SBAR creation (for matched TPs); primary question: does the system alert *before* the SBAR is raised?
3. **Explainability rating** — clinician rating of alert justifications on a 3-point scale (clear / partial / unclear)
4. **Suppression accuracy** — of all worsening/critical problems where the system suppressed an alert because it judged the problem as "being addressed," what fraction corresponded to a real SBAR within the matching window?
5. **Alert burden** — total alerts per patient-day; fraction suppressed by the "being addressed" mechanism
6. **Cost per event detected** — LLM API cost per true-positive alert

---

## 4. Study Design

Prospective observational cohort study. The LLM CDS system runs passively alongside standard clinical care. No patient management decisions are altered by participation in this study. Clinician response to alerts follows the existing standard workflow.

The study uses a **continuous automated pipeline** design: alert data, SBAR imports, matching, and metrics computation run automatically every hour. Manual clinician adjudication (via a dedicated web UI) is the only human-in-the-loop step.

---

## 5. Study Setting and Population

### 5.1 Setting

Workspace 1A of the Cloudphysician multi-hospital tele-ICU monitoring network. This workspace encompasses ICU and HDU patients across multiple partner hospitals, all monitored by the same remote physician team.

Current census at study initiation: approximately 30 admitted patients (exact number varies with admissions and discharges; new admissions auto-enroll hourly, discharges are auto-detected and deactivated).

### 5.2 Inclusion criteria

- All patients admitted to a workspace 1A monitored bed at any point during the 7-day study window
- Patient encounter active for ≥2 consecutive hours (to allow at least two pipeline cycles)

### 5.3 Exclusion criteria

- Patients whose data pipeline failed to sync for >50% of expected hourly snapshots during their admission (data completeness failure)
- NICU or PICU patients, if any are routed to workspace 1A (different physiological thresholds)

---

## 6. Study Period

| Milestone | Date |
|---|---|
| Study start (00:00 IST) | Monday, 26 May 2026 |
| Study end (23:59 IST) | Sunday, 1 June 2026 |
| Adjudication window | 2–6 June 2026 |
| Analysis and write-up | 6 June 2026 onwards |
| Target submission | August 2026 |

**Duration rationale:** Seven days provides a larger patient-hours base than the 48-hour pilot, captures day-of-week variation in SBAR patterns and census, and is sufficient to estimate sensitivity and specificity with Wilson 95% CIs of ±15% or better (estimated, based on expected event rates).

---

## 7. Reference Standard and Outcome Definitions

### 7.1 Alert classification — manual adjudication

All system alerts generated during the study window will be reviewed by the principal investigator using a web-based adjudication interface. Each alert is assessed against the full patient chart at the time the alert fired. The reviewer records:

- **Verdict:** Appropriate (would have been clinically useful to act on) or Inappropriate (already addressed, clinically trivial, or factually incorrect)
- **Explainability rating:** 1 = clear and actionable, 2 = understandable but incomplete, 3 = unclear or misleading
- **Free-text notes** (optional)

### 7.2 Event detection — SBAR reference standard

High-urgency SBAR notifications (`urgency = 'High'`) from the BigQuery table `prod-tech-project1-bv479-zo027.patient.latest_sbar_fact` are used as the independent ground-truth reference standard for clinical deterioration events.

These SBARs are generated by the nursing documentation system and represent urgent clinical events independently flagged by bedside staff for physician review, most commonly abnormal arterial blood gas (ABG) results and renal function tests — the primary physiological domains in which rapid deterioration occurs.

**SBAR-alert matching:**  
An LLM-based semantic matching step (Gemini 2.5 Flash) determines whether a given SBAR and a given alert refer to the same clinical problem for the same patient. A SBAR is matched to an alert if:
- Same patient (CPMRN) and encounter
- Alert fired within [SBAR creation − 6h, SBAR creation + 2h]
- LLM semantic similarity score ≥ 0.70 (on a 0–1 scale)

The asymmetric window captures the primary scenario (system alerts in advance of the SBAR) while allowing concurrent detection.

**2×2 classification:**

|  | Alert fired | No alert |
|---|---|---|
| **High SBAR exists** | TP (confirmed by adjudication) | FN |
| **No High SBAR** | FP (confirmed by adjudication) | TN |

- **TP** — alert fired AND clinician adjudicated "Appropriate" AND matched to a high-urgency SBAR
- **FP** — alert fired AND clinician adjudicated "Inappropriate," OR alert fired but no matching SBAR within window
- **FN** — high-urgency SBAR created with no matching system alert within the window
- **TN** — patient-encounter hour with no alert and no high-urgency SBAR

### 7.3 SBAR scope restriction

Only high-urgency SBARs from modules `vitals`, `summary`, `documents`, and `intake-output` are included. Modules `orders`, `mar`, and `notes` are excluded as they do not represent primary physiological deterioration events.

---

## 8. Statistical Analysis

### 8.1 Primary analysis

From the 2×2 matrix:

- **Sensitivity** = TP / (TP + FN)
- **Specificity** = TN / (TN + FP)
- **PPV** = TP / (TP + FP)
- **NPV** = TN / (TN + FN)
- **F1** = 2 × PPV × Sensitivity / (PPV + Sensitivity)

All proportions reported with **95% Wilson score confidence intervals**.

### 8.2 Lead time analysis

For all matched TPs, lead time = SBAR creation time − system alert time (minutes). Reported as median and IQR. Negative values indicate the system alerted *before* the SBAR was raised (early warning). Positive values indicate post-hoc detection.

### 8.3 Suppression analysis

For all worsening/critical problems where the "being addressed" suppression was triggered: proportion that had a corresponding high-urgency SBAR within the matching window. A high suppression-FN rate would indicate the system is incorrectly silencing real events; a low rate validates the suppression mechanism.

### 8.4 Sample size considerations

Based on a census of ~30 patients over 7 days (168 patient-encounters), with an estimated 0.5–1.0 high-urgency SBARs per patient per day (yielding ~100–200 event-hours), Wilson CIs for a sensitivity of 0.80 are expected to be approximately ±8% at n=100 events. This is adequate for a prospective proof-of-concept study.

---

## 9. Data Management

All study data are stored in a self-hosted MongoDB instance within the Cloudphysician infrastructure. No patient data leaves the network perimeter. Study collections (`study_alerts`, `study_sbar_import`, `study_adjudications`, `study_metrics_snapshots`) are append-only after initial write; no retrospective modification of alert records is possible.

The adjudication interface is accessible only over VPN by the principal investigator.

---

## 10. Ethical Considerations

This study is observational. The LLM CDS system runs as a passive monitoring layer alongside existing clinical workflows. No patient management protocol is altered. Clinicians receive alerts through the existing Google Chat channel; their decision to act on or ignore any alert remains entirely at their discretion.

Patient data are processed within Cloudphysician's existing clinical data infrastructure, governed by existing data processing agreements with partner hospitals. No new data collection is introduced. An IEC waiver of consent is requested on the grounds that: (a) the study involves no intervention; (b) individual patient-level results are not reported; (c) the data pipeline is already in clinical use.

---

## 11. Compute and API Cost Budget

### 11.1 Observed cost (from live runs, 22 May 2026)

Two complete hourly runs have been executed over the study unit (workspace 1A, ~30 patients). Observed costs:

| Run | Patients | Input tokens | Output tokens | Thinking tokens | **Cost (USD)** |
|---|---|---|---|---|---|
| 2026-05-22 19:00 UTC | 30 | 741,419 | 21,475 | 141,321 | **$0.5502** |
| 2026-05-22 20:00 UTC | 31 | 758,419 | 24,031 | 142,028 | **$0.5540** |
| **Average per run** | **~30** | **749,919** | **22,753** | **141,675** | **$0.5521** |

Token split by step (average):

| Step | Input | Output | Thinking | Cost |
|---|---|---|---|---|
| `problem_tracker` | 621,822 | 18,873 | 106,465 | $0.4193 |
| `status_classifier` | 128,097 | 3,880 | 35,210 | $0.1329 |
| **Total** | **749,919** | **22,753** | **141,675** | **$0.5521** |

Pricing model: Gemini 2.5 Flash — Input $0.075/1M, Output $0.30/1M, Thinking $3.50/1M.

### 11.2 Projected cost for 1-week study

| Period | Runs | Projected cost |
|---|---|---|
| 7-day study (168 runs) | 168 | **$92.75** |
| + 20% contingency buffer | — | $18.55 |
| **Total budget requested** | — | **$111.30 USD (~₹9,300)** |

### 11.3 Notes

- Thinking tokens account for ~95% of the output cost. This is by design — the extended reasoning is what enables context-sensitive suppression.
- Cost scales linearly with patient census. If census rises above 40, the weekly cost may reach ~$120–$130. The contingency buffer covers this.
- The study_matcher LLM matching step (Gemini 2.5 Flash, no thinking) adds a small additional cost (~$0.01–0.02/run) not yet captured in the above, as it runs only when new SBARs arrive.
- No other API costs are incurred (FAISS index is self-hosted; MongoDB is self-hosted; BigQuery reads are covered by the existing GCP project budget).

---

## 12. Timeline

| Week | Activity |
|---|---|
| 22–25 May 2026 | IEC application, budget approval, dry-run validation |
| 26 May – 1 June 2026 | **Active data collection** (automated, 24×7) |
| 2–6 June 2026 | Adjudication (PI reviews all alerts via web UI) |
| 7–14 June 2026 | Statistical analysis, figure generation |
| 15–30 June 2026 | Manuscript draft |
| July 2026 | Internal review, co-author revisions |
| August 2026 | Submission target |

---

## 13. Target Journals

Primary: *npj Digital Medicine*, *The Lancet Digital Health*, *JAMIA*  
Secondary: *Critical Care Medicine*, *Intensive Care Medicine*

---

## 14. Anticipated Impact

If the system demonstrates sensitivity ≥0.75 with specificity ≥0.90, it would represent a clinically meaningful advance over rule-based early warning systems (NEWS2 sensitivity ~0.60–0.65 in comparable populations) while providing the contextual suppression and explainability that ML classifiers lack. The prospective real-world design — fully automated, continuous, 24×7 — directly addresses the implementation gap between retrospective AI benchmarks and clinical deployment.

---

*Protocol prepared by Dr. Dileep Unni, Cloudphysician. For queries: dileep.unni@cloudphysician.net*
