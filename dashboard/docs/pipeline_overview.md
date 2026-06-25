# Hourly Pipeline Overview

The CDS pipeline runs once per hour via Cloud Scheduler. It screens all admitted patients in the monitored workspaces, tracks clinical problems, generates alert cards, and sends them to clinicians via Google Chat for rating.

## End-to-End Flow

LLM nodes are highlighted in purple — these are the points where model errors, prompt issues, or attention failures can affect patient safety. Cost-free gates are in grey; they short-circuit the pipeline before any LLM call.

```mermaid
flowchart TD
    classDef llm fill:#6d28d9,color:#fff,stroke:#4c1d95,stroke-width:2px
    classDef store fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef infra fill:#374151,color:#fff,stroke:#6b7280
    classDef gate fill:#1c1917,color:#fff,stroke:#57534e
    classDef bypass fill:#065f46,color:#fff,stroke:#047857

    A([Cloud Scheduler · hourly]):::infra --> B[scheduler._collect_all]:::infra
    B --> C[Fetch admitted patients from Radar EMR]:::infra
    C --> DELTA[extract_delta · free\ncompare chart vs last_llm_run_at cutoff]:::gate

    DELTA -->|new_vitals=0 new_labs=0\nnew_notes=0 io_changed=False| DGATE[Delta gate\nskip — no new data]:::bypass

    DELTA -->|vitals-only delta\nall vitals score 0 on NEWS2| VGATE[Gate 2.5 · vital-normal gate\nskip — vitals all normal]:::bypass

    DELTA -->|vitals-only delta\nany vital abnormal| VFORCE[Gate 2.5 · vital-normal gate\nforce_full_vitals → full analysis]:::gate

    DELTA -->|only new glucose labs\nno vitals/notes/reports| GGATE[Gate 2.6 · glucose-only gate\nbypass full pipeline]:::gate
    GGATE --> GINSULIN[insulin_advice.py\ngather_inputs + compute]:::infra
    GINSULIN --> GCHAT2[Alert card sent\nnext_check.due_after = now + next_grbs_after]:::infra

    DELTA -->|overdue glucose next_check only\nno generic overdue checks| GOGATE[Glucose-only forced recheck\nno LLM — same cheap path]:::gate
    GOGATE --> GINSULIN

    DELTA -->|new ABG\nany value crosses threshold| ABGFORCE[Gate 2.7 · ABG gate\nforce_full_abg → full analysis]:::gate

    DELTA -->|new non-ABG non-glucose lab| LABFORCE[Gate 2.8 · other-lab gate\nforce_full_other_lab → full analysis]:::gate

    DELTA -->|new notes only\nno upstream gate fired| PASS1[Pass 1 screener · notes only\ngemini-3.1-flash-lite · no thinking\nreads note text, decides relevance]:::llm
    PASS1 -->|needs_full=False| SKIP([Skip Pass 2]):::bypass
    PASS1 -->|needs_full=True| SUM

    DELTA -->|io_changed=True\nno new vitals/labs/notes| SUM
    DELTA -->|anything else| SUM[summary_updater\ngemini-3.1-flash-lite · no thinking]:::llm

    VFORCE --> SUM
    ABGFORCE --> SUM
    LABFORCE --> SUM

    SUM --> CLASS[status_classifier\ngemini-2.5-flash · thinking]:::llm
    CLASS --> PT[problem_tracker\ngemini-2.5-flash · thinking · ReAct loop]:::llm
    PT --> G{Alert needed?}
    G -->|Yes| INS[insulin_advice.py\nif glucose problem]:::infra
    INS --> CARD[chat_card_sender.py · Google Chat]:::infra
    G -->|No| SKIP
    CARD --> CLINICIAN[Clinician sees card]:::infra
    CLINICIAN --> FB[Rating 1–5 + comment]:::infra
    FB --> FBE[alert-feedback endpoint]:::infra
    FBE --> FBT[(study_alert_feedback)]:::store
    PT --> SA[(study_alerts)]:::store
    PT --> TR[(GCS · pipeline_traces)]:::store
    TR --> COST[study_cost_tracker]:::infra
    COST --> RC[(pipeline_run_costs)]:::store
```

## Delta Extraction

At the start of every patient run, `delta_extractor.extract_delta()` compares the current chart snapshot against the `last_llm_run_at` cutoff timestamp stored in `snapshot_schedule`.

```
new_vitals          — vital readings with timestamp > cutoff
new_labs            — lab documents with reportedAt > cutoff
new_notes           — note content entries with timestamp > cutoff
delta_orders        — always the full current active/pending orders (no diff possible)
io_last_24h         — full 24h I/O aggregate (intake_ml, output_ml, balance_ml)
io_changed          — True when io_last_24h differs from the value stored at last run
new_report_findings — injected later by analyze_new_reports (Phase 2)
```

`io_changed` is computed by comparing the current 24h aggregate against `last_io_aggregate` stored in `snapshot_schedule` after the previous run. The raw IO structure (`chart.io.days[].hours[].minutes[]`) has no per-entry timestamps; the aggregate comparison is the only reliable way to detect a new entry. When `io_changed=True`, the **full 24h aggregate** is what the LLM sees — never a single isolated IO reading.

### First-run seeding behaviour

On the very first run for a newly enrolled patient, there is no `last_llm_run_at` cutoff (`cutoff: none` in the log). Rather than returning the entire chart history, the extractor returns a **shallow seed**:

| Field | First-run behaviour |
|---|---|
| `new_vitals` | Newest **6** readings only (`vitals[:6]`, array is newest-first) |
| `new_labs` | Newest **10** lab documents (`docs[-10:]`, array is oldest-first) |
| `new_notes` | Last **3 substantive** notes (>100 chars; falls back to last 3 if none qualify) |
| `new_report_findings` | Empty — populated later if reports are processed |

This means patients enrolled with a long ICU history will have a narrow delta on their first run. The rolling summary updater fills in the broader clinical picture, but the delta itself is always shallow at enrollment.

**Implication:** patients whose Radar chart has no vitals, no labs, and no qualifying notes at enrollment will show `{new_vitals: 0, new_labs: 0, new_notes: 0}` and be skipped by the delta gate on that first run. This is expected — they will process normally on the next run once clinical data is recorded.

### Delta-scope scoping (Phases A/B/C)

After extraction, `delta_scope.classify_delta()` analyses the delta to derive a **blast radius** — the set of problems and rules this delta could plausibly move.

| Delta content | `is_wildcard` | Phase B rule-block scoping | Phase C problem pruning |
|---|---|---|---|
| New notes or report findings | **Yes** | All blocks injected | All problems assessed |
| Non-ABG, non-glucose labs | No | Only blocks for matched categories | Only blast-radius problems |
| ABG labs | No | Respiratory block injected | Only respiratory/acid-base problems |
| Vitals only (abnormal) | No | Only vital-driven blocks | Only vital-driven problems |
| IO only | No | Renal block only | Only renal/fluid problems |
| Glucose labs only | No | **Bypassed** (Gate 2.6 fires first) | **Bypassed** |
| No new data | No | No expensive run (delta gate fires) | — |

**Safety invariant:** any delta containing `new_notes` or `new_report_findings` is a wildcard — unstructured text can introduce new problems, plan changes, or care-gaps that keyword matching cannot anticipate. Wildcard deltas always produce a full pipeline run with no scoping.

**Phase C (problem pruning)** is **active** (`DELTA_SCOPE_PRUNE=1` set in production). The problem_tracker only receives the blast-radius set; `would_prune` problems are excluded from the LLM call entirely. This reduces token cost for single-vital or single-lab runs where unrelated problems have no new data to re-assess.

## Bypass Gates (zero LLM cost)

| Gate | Condition | Outcome |
|---|---|---|
| **Delta gate** | `new_vitals=0 AND new_labs=0 AND new_notes=0 AND io_changed=False` (and no overdue checks) | Skip entire LLM pipeline; update `next_run_at` |
| **Gate 2.5 · vital-normal (skip)** | Delta is vitals-only AND all new vitals score 0 on NEWS2 | Skip LLM; schedule next run in 1h |
| **Gate 2.5 · vital-abnormal (force)** | Delta is vitals-only AND any vital is abnormal | `force_full_vitals` → bypass screener, run full Pass 2 |
| **Gate 2.6 · glucose-only** | Delta is labs-only AND all new lab docs are glucose-named (RBS/CBG/GRBS/Blood Glucose) | Skip Pass 1 + Pass 2; call `insulin_advice` directly; send alert card |
| **Gate 2.7 · ABG threshold** | New ABG lab AND any of pH/pO2/paCO2/lactate/bicarb crosses threshold | `force_full_abg` → bypass screener, run full Pass 2 |
| **Gate 2.7 · ABG no threshold** | New ABG lab AND no value crosses threshold | No action; notes screener may still fire if new notes also present |
| **Gate 2.8 · other lab** | Any new lab that is neither ABG nor glucose | `force_full_other_lab` → bypass screener, run full Pass 2 |
| **Pass-1 screener skip (no notes)** | No upstream gate fired AND no new notes in delta | Skip screener and Pass 2; update schedule |
| **Glucose-only forced recheck** | Overdue `next_check` is glucose only (no other overdue generic labs) | Same cheap path as Gate 2.6 — no LLM; `next_check.due_after` set from `next_grbs_after` |
| **Generic forced recheck** | Overdue `next_check` is a non-glucose lab (creatinine, Hb, etc.) | Bypass cadence + delta + screener; run full `track_problems` with `focus=overdue items` |

Gate 2.6 fires only when the incoming lab document is **named** as a standalone glucose test. A panel document (e.g. "Renal Function Test") that happens to contain a glucose attribute does not trigger it — it falls through to Gate 2.8, which triggers a full run.

**ABG thresholds (Gate 2.7):**

| Parameter | Flag threshold |
|---|---|
| pH | < 7.30 or > 7.50 |
| pO2 | < 60 mmHg |
| paCO2 | > 50 mmHg or < 30 mmHg |
| Lactate | > 2 mmol/L |
| Bicarb (HCO3) | < 15 or > 35 mmol/L |

Thresholds are intentionally lax — this is a screening gate and false negatives (missing a borderline ABG) are more dangerous than false positives (running an unnecessary Pass 2).

## Pass-1 Screener — Notes Only

The Pass-1 screener (`pass1_screener.py`) is called **only when** all of the following are true:
- No upstream gate fired a force flag (`force_full_vitals`, `force_full_abg`, `force_full_other_lab`, `force_expensive`)
- At least one new note is present in the delta

Its sole job is to read the note text and decide if there is anything clinically significant — a new problem, treatment failure, plan change, or worrying finding described by the treating team. It does not re-evaluate vitals or labs (those are handled deterministically by Gates 2.5–2.8).

| Screener outcome | Action |
|---|---|
| `needs_full_analysis=true` | Run summary_updater → status_classifier → problem_tracker |
| `needs_full_analysis=false` | Skip Pass 2; update schedule; run fn_detector |
| Screener fails / exception | Default to `needs_full_analysis=false` (safe — avoids burning expensive model on an uncertain call) |

Each gate has a distinct responsibility — vitals to Gate 2.5, ABG to Gate 2.7, other labs to Gate 2.8, free text to the screener. No overlap.

## Clinical Timeline

The problem tracker maintains a **per-patient clinical event timeline** stored in `patient_contexts.clinical_timeline`. It is an ordered record of what happened and when — complementary to `structured_summary` ("what is true now") and designed to prevent temporal-reasoning errors like flagging clinical progression as a contradiction.

### Two-zone model

| Zone | Field | Description |
|---|---|---|
| **Recent events** | `recent_events[]` | Full-fidelity structured list, never compressed. The model reads this directly to establish temporal order. |
| **Prior course** | `prior_course` | Short prose (≤1500 chars) holding everything that has aged out of the recent window. Appended incrementally — oldest portions condensed only if the cap is exceeded. |

Each event carries:
```json
{ "t": "2026-06-25T09:20:00Z", "event": "Oral nutrition resumed, tolerating liquids",
  "problem": "Subacute Intestinal Obstruction", "kind": "status_change",
  "source": "note@2026-06-25T09:20" }
```
`source` is mandatory — it anchors every event to real data and prevents hallucination.

### Windowing (fold-on-eviction)

An event stays in `recent_events` while **both** hold: within the last **7 days** AND within the last **30 events**. Failing either evicts it into `prior_course`. The fold is **append-only and rare** — it only fires when something actually ages out, and it preserves the existing `prior_course` text verbatim before appending new clauses.

**No-data-loss guarantee:** events are dropped from `recent_events` only when the model successfully emitted an updated `prior_course`. If the fold failed, the events remain and retry next run.

### How it flows

```mermaid
flowchart LR
    classDef llm fill:#6d28d9,color:#fff,stroke:#4c1d95,stroke-width:2px
    classDef store fill:#1e3a5f,color:#fff,stroke:#1e40af

    GCS[("patient_contexts\nclinical_timeline")]:::store
    SC["status_classifier\nreads timeline (read-only)\nfor temporal context"]:::llm
    PT["problem_tracker\nreads + updates timeline\nemits timeline_updates\n+ conditional prior_course"]:::llm
    GCS2[("patient_contexts\nclinical_timeline updated")]:::store

    GCS -->|render — read-only| SC
    GCS -->|render_for_tracker| PT
    PT -->|apply_updates → persist| GCS2
```

The timeline is **read by both** `status_classifier` and `problem_tracker`, but **only maintained by** the tracker. It is emitted as part of the existing `set_all_assessments` tool call — no extra LLM call, no added cost.

### Why this fixes temporal reasoning errors

Previously the tracker reasoned over a flat, unordered bag of notes. Seeing an old NPO note and a new "tolerating oral" note together triggered a "contradiction" alert. With the timeline, the temporal ordering is explicit: the model sees the progression and treats it as clinical advancement, not a care-gap.

The CARE-GAP criterion now applies **only** when two notes from the same care period describe incompatible active orders, or the most recent note itself documents an unresolved conflict.

## Problem Tracker Prompt Assembly

Before each LLM call, the pipeline assembles the system prompt in layers. Each layer is a potential failure point — a missing block means the model reasons without that rule; a conflicting block causes double-alerting or missed suppression.

```mermaid
flowchart TB
    classDef always fill:#065f46,color:#fff,stroke:#047857
    classDef conditional fill:#92400e,color:#fff,stroke:#b45309
    classDef gcs fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef assembly fill:#1f2937,color:#fff,stroke:#4b5563,stroke-width:2px
    classDef llm fill:#6d28d9,color:#fff,stroke:#4c1d95,stroke-width:2px

    CORE["① Core _SYSTEM · always injected (~6,580 tok)\n─────────────────────────────\nAlert decision ladder\nVital sign alert floors + SF ratio gate\nMulti-vital crisis override\nGCS delta rule\nNote freshness rule (labs only)\nHyperglycemia always-alert exception\nLab staleness / vital staleness hard blocks\nOutput contract"]:::always

    DELTA_HDR["② Delta header · Phase A\n─────────────────────────────\n'DELTA THIS RUN: 1 new lab (K 5.8)…'\nPrepended to user message\nDirects attention to what changed"]:::conditional

    DOMAIN["③ Protocol guidance blocks · Phase B conditional\n─────────────────────────────\nNeuro (guidance-neuro) → GCS delta reasoning\nRespiratory (guidance-respiratory) → SF ratio, FiO2 rule\nRenal (guidance-renal) → get_io, oliguria charting rule\nSymptom (guidance-symptom) → objective evidence required\nCausal (guidance-causal-secondary) → secondary problems\nTachycardia → HR>100 entry, unstable/stable branches\n\nAll in monitoring_protocols (GCS/Mongo-editable).\nInjected by protocol_engine.build_injection() when\napplies_when keywords match problem names/prefetch."]:::conditional

    PROTO["④ Monitoring protocols · permissive gates · from GCS\n─────────────────────────────\npermissive-hypertension · established-low-gcs\npermissive-respiratory · haemoglobin-alert-criteria\nlactate-alert-criteria · acknowledged-myocardial-injury\n\nContext gate: if scenario applies → suppress alert\nSame protocol doc can carry guidance + gate + audit."]:::gcs

    LAB["⑤ Lab alert rules · from GCS\n─────────────────────────────\nPer-lab floor / ceiling / delta thresholds\nFiltered to labs present in this patient's data"]:::gcs

    SYM["⑥ Symptom alert rules · from GCS\n─────────────────────────────\nObjective criteria required per symptom type\nFiltered to problems this patient has"]:::gcs

    SP["system_prompt = ① + ③ + ④ + ⑤ + ⑥\nuser_msg = ② + prefetch + problem list\n─────────────────────────────\nMissing layer = model reasons without that rule\nConflicting layer = double-alert or missed suppression"]:::assembly

    PT["problem_tracker LLM call\ngemini-2.5-flash · thinking"]:::llm

    CORE --> SP
    DELTA_HDR --> SP
    DOMAIN --> SP
    PROTO --> SP
    LAB --> SP
    SYM --> SP
    SP --> PT
```

## Alert Decision Ladder

For each problem, the model walks this ladder top-to-bottom. The **first matching step wins** — lower steps are never reached once a match fires.

```mermaid
flowchart TD
    classDef gate fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef suppress fill:#065f46,color:#fff,stroke:#047857
    classDef alert fill:#7f1d1d,color:#fff,stroke:#991b1b
    classDef check fill:#1c1917,color:#fff,stroke:#57534e
    classDef override fill:#78350f,color:#fff,stroke:#b45309

    MVC{"MULTI-VITAL CRISIS OVERRIDE\n2+ vitals simultaneously below floors?\n(e.g. SpO2<92% AND MAP<65)"}:::override
    MVC -->|Yes — bypass all suppression| ALERT5

    S0{"STEP 0 — Evidence-type gate\nIs this problem alert-worthy at all?"}:::gate
    S0V["VITAL: current value must cross floor\nAND trend flat or worsening"]:::check
    S0L["LAB: single clearly-abnormal value\nvs. baseline · not stale >24h"]:::check
    S0SY["SYMPTOM: objective corroboration required"]:::check
    S0CG["CARE-GAP: ≥2 contradicting notes cited"]:::check
    FAIL0(["→ clinical_status=stable · should_alert=False"]):::suppress

    S1{"STEP 1 — Permissive context gate\nActive protocol scenario matches?"}:::gate
    SUPP1(["→ should_alert=False\nHighest precedence"]):::suppress

    S2{"STEP 2 — Team actively responding\nright now?"}:::gate
    SUPP2(["→ being_addressed=True · should_alert=False"]):::suppress

    S3{"STEP 3 — Plan note < 24h old?\nLABS ONLY — not applicable to vitals\nNot applicable to CARE-GAP"}:::gate
    SUPP3(["→ being_addressed=True · should_alert=False"]):::suppress

    S4{"STEP 4 — Plan note within\nresponse-buffer window?"}:::gate
    SUPP4(["→ being_addressed=True · should_alert=False\nset next_check"]):::suppress

    S5{"STEP 5 — Plan note > 24h old\nAND still worsening?"}:::gate
    ALERT5(["→ should_alert=True\nalert_reason: Treatment inadequate"]):::alert

    S6{"STEP 6 — No plan at all?"}:::gate
    ALERT6(["→ should_alert=True"]):::alert

    MVC --> S0
    S0 -->|Passes| S1
    S0 -->|Fails| FAIL0
    S0 --> S0V
    S0 --> S0L
    S0 --> S0SY
    S0 --> S0CG
    S1 -->|Yes| SUPP1
    S1 -->|No| S2
    S2 -->|Yes| SUPP2
    S2 -->|No| S3
    S3 -->|Yes| SUPP3
    S3 -->|No| S4
    S4 -->|Yes| SUPP4
    S4 -->|No| S5
    S5 -->|Yes| ALERT5
    S5 -->|No| S6
    S6 -->|Yes| ALERT6
    S6 -->|No| SUPP4
```

**Exceptions always in effect:**
- **Hyperglycemia** — Steps 3 and 4 are skipped; always `should_alert=True` when glucose is above threshold regardless of plan notes.
- **SF ratio persistence override** — SF ratio gate is bypassed if SpO2 < 92% in ≥2 consecutive readings.

**Global constraint:** never alert when `clinical_status` is stable, improving, or resolved — even if a `next_check` is overdue.

## Feedback Loop

```mermaid
flowchart LR
    PT[problem_tracker] -->|insert_alert| SA[(study_alerts)]
    Card[Alert card in Google Chat] -->|clinician rates| FB[(study_alert_feedback)]
    SA -->|JOIN on alert_id| DASH[This Dashboard]
    FB --> DASH
    DASH --> M1[Performance over time]
    DASH --> M2[Inter-rater agreement]
    DASH --> M3[By problem type]
```

## Key Tables

| Table | Purpose |
|---|---|
| `cds_study.study_alerts` | One row per alert generated |
| `cds_study.study_alert_feedback` | One row per clinician rating |
| `cds_study.pipeline_run_costs` | LLM token cost per hourly run |
| `cds_study.study_suppressed_events` | Alerts suppressed (not sent) |
| `cds_study.report_interpret_runs` | Cost and count per report interpretation cycle |

All tables are in project `patientview-9uxml`, dataset `cds_study`, region `asia-south1`.
