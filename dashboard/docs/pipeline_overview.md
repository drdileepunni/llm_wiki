# Hourly Pipeline Overview

The CDS pipeline runs once per hour via Cloud Scheduler. It screens all admitted patients in the monitored workspaces, tracks clinical problems, generates alert cards, and sends them to clinicians via Google Chat for rating.

## End-to-End Flow

LLM nodes are highlighted in purple — these are the points where model errors, prompt issues, or attention failures can affect patient safety.

```mermaid
flowchart TD
    classDef llm fill:#6d28d9,color:#fff,stroke:#4c1d95,stroke-width:2px
    classDef store fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef infra fill:#374151,color:#fff,stroke:#6b7280

    A([Cloud Scheduler - hourly]):::infra --> B[scheduler._collect_all]:::infra
    B --> C[Fetch admitted patients from Radar EMR]:::infra
    C --> D[pass1_screener\ngemini-3.1-flash-lite · no thinking]:::llm
    D -->|Has urgent problems| E[evaluate_screener_flag\ngemini-3.1-flash-lite · focused new-finding check]:::llm
    D -->|No issues| Z([Skip patient])
    E -->|New problem confirmed| EP[Inject new problem into list]
    E -->|Maps to existing / ignore| F
    EP --> F[problem_tracker\ngemini-3.1-flash-lite · full ReAct loop]:::llm
    F --> GCS[Inject config from GCS]:::infra
    GCS --> F
    F --> G{Alert needed?}
    G -->|Yes| H[alert_cards.py - build rich card]
    G -->|No| Z
    H --> I[chat_card_sender.py - Google Chat API]:::infra
    I --> J[Clinician sees card in Google Chat]
    J --> K[Clinician rates 1-5 and adds comment]
    K --> L[alert-feedback endpoint]:::infra
    L --> M[(study_alert_feedback)]:::store
    F --> N[(study_alerts)]:::store
    F --> O[(GCS - pipeline_traces)]:::store
    O --> P[study_cost_tracker]
    P --> Q[(pipeline_run_costs)]:::store
```

## Problem Tracker Prompt Assembly

Before each LLM call, the pipeline assembles the system prompt in layers. Each layer is a potential failure point — a missing block means the model reasons without that rule; a conflicting block causes double-alerting or missed suppression.

```mermaid
flowchart TB
    classDef always fill:#065f46,color:#fff,stroke:#047857
    classDef conditional fill:#92400e,color:#fff,stroke:#b45309
    classDef gcs fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef assembly fill:#1f2937,color:#fff,stroke:#4b5563,stroke-width:2px
    classDef llm fill:#6d28d9,color:#fff,stroke:#4c1d95,stroke-width:2px

    CORE["① Core _SYSTEM · always injected\n─────────────────────────────\nAlert decision ladder\nVital sign alert floors\nGCS delta rule — no alert without ≥2pt drop in 6h\nNote freshness rule — notes <24h are always active plans\nLab staleness / vital staleness hard blocks\nOutput contract"]:::always

    DOMAIN["② Domain rule blocks · conditional\n─────────────────────────────\nNeuro → GCS delta reasoning\nRespiratory → SF ratio, FiO2 rule\nRenal → get_io, oliguria charting rule\nSymptom → objective evidence required\nCausal → secondary problem reasoning\n\nInjected only when patient has a matching problem\nKeyword match against problem names + prefetch"]:::conditional

    PROTO["③ Monitoring protocols · from GCS\n─────────────────────────────\npermissive-hypertension · established-low-gcs\npermissive-respiratory · haemoglobin-alert-criteria\nlactate-alert-criteria\n\nContext gate: if scenario applies → suppress alert"]:::gcs

    LAB["④ Lab alert rules · from GCS\n─────────────────────────────\nPer-lab floor / ceiling / delta thresholds\nFiltered to labs present in this patient's data"]:::gcs

    SYM["⑤ Symptom alert rules · from GCS\n─────────────────────────────\nObjective criteria required per symptom type\nFiltered to problems this patient has"]:::gcs

    SP["system_prompt = ① + ② + ③ + ④ + ⑤\n─────────────────────────────\nSingle string passed to LLM\nMissing layer = model reasons without that rule\nConflicting layer = double-alert or missed suppression"]:::assembly

    PT["problem_tracker LLM call\ngemini-3.1-flash-lite"]:::llm

    CORE --> SP
    DOMAIN --> SP
    PROTO --> SP
    LAB --> SP
    SYM --> SP
    SP --> PT
```

## Alert Decision Ladder

For each problem, the model walks this ladder top-to-bottom. The **first matching step wins** — lower steps are never reached once a match fires. This is the core logic inside the `problem_tracker` LLM call.

```mermaid
flowchart TD
    classDef gate fill:#1e3a5f,color:#fff,stroke:#1e40af
    classDef suppress fill:#065f46,color:#fff,stroke:#047857
    classDef alert fill:#7f1d1d,color:#fff,stroke:#991b1b
    classDef check fill:#1c1917,color:#fff,stroke:#57534e

    S0{"STEP 0 — Evidence-type gate\nIs this problem alert-worthy at all?"}:::gate

    S0V["VITAL-driven\nCurrent value must cross floor\nAND trend flat or worsening"]:::check
    S0L["LAB-driven\nSingle clearly-abnormal value\nvs. patient baseline · not stale >24h"]:::check
    S0SY["SYMPTOM-driven\nObjective corroboration required\n(score, vital correlate, or imaging)"]:::check
    S0CG["CARE-GAP\n≥2 specific notes contradicting each other\ncited explicitly"]:::check

    FAIL0(["→ clinical_status=stable\nshould_alert=False"]):::suppress

    S1{"STEP 1 — Permissive context gate\nActive protocol scenario matches?"}:::gate
    SUPP1(["→ should_alert=False\nHighest precedence — overrides all below"]):::suppress

    S2{"STEP 2 — Team actively responding\nright now?\n(ACLS, emergency intubation,\nvasopressors being titrated)"}:::gate
    SUPP2(["→ being_addressed=True\nshould_alert=False"]):::suppress

    S3{"STEP 3 — Most recent plan note\n< 24h old?\n(except CARE-GAP problems)"}:::gate
    SUPP3(["→ being_addressed=True\nshould_alert=False\nno treatment-inadequate override"]):::suppress

    S4{"STEP 4 — Plan note within\nresponse-buffer window\nfor its intervention type?"}:::gate
    SUPP4(["→ being_addressed=True\nshould_alert=False\nset next_check to monitor response"]):::suppress

    S5{"STEP 5 — Plan note > 24h old\nAND problem still worsening?"}:::gate
    ALERT5(["→ being_addressed=False\nshould_alert=True\nalert_reason: Treatment inadequate"]):::alert

    S6{"STEP 6 — No documented plan\nexists at all?"}:::gate
    ALERT6(["→ should_alert=True"]):::alert

    S0 -->|Passes gate| S1
    S0 -->|Fails gate| FAIL0
    S0 --> S0V
    S0 --> S0L
    S0 --> S0SY
    S0 --> S0CG

    S1 -->|Yes — scenario active| SUPP1
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
    S6 -->|No plan gap either| SUPP4
```

**Global constraint (applies at every step):** Never alert when `clinical_status` is stable, improving, or resolved — even if a `next_check` is overdue.

## Feedback Loop

Clinician ratings flow back into BigQuery and surface in this dashboard:

```mermaid
flowchart LR
    PT[problem_tracker] -->|insert_alert| SA[(study_alerts)]
    Card[Alert card sent to Google Chat] -->|clinician rates| FB[(study_alert_feedback)]
    SA -->|JOIN on alert_id| DASH[This Dashboard]
    FB --> DASH
    DASH --> M1[Metrics - performance over time]
    DASH --> M2[Inter-rater agreement]
    DASH --> M3[By problem type]
```

## Key Tables

| Table | Purpose |
|---|---|
| `cds_study.study_alerts` | One row per alert generated |
| `cds_study.study_alert_feedback` | One row per clinician rating |
| `cds_study.pipeline_run_costs` | LLM token cost per hourly run |
| `cds_study.study_suppressed_events` | Alerts that were suppressed (not sent) |

All tables are in project `patientview-9uxml`, dataset `cds_study`, region `asia-south1`.
