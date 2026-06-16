# Hourly Pipeline Overview

The CDS pipeline runs once per hour via Cloud Scheduler. It screens all admitted patients in the monitored workspaces, tracks clinical problems, generates alert cards, and sends them to clinicians via Google Chat for rating.

## End-to-End Flow

```mermaid
flowchart TD
    A([Cloud Scheduler - hourly]) --> B[scheduler._collect_all]
    B --> C[Fetch admitted patients from Radar EMR]
    C --> D[pass1_screener - quick LLM triage]
    D -->|Has urgent problems| E[problem_tracker - main LLM analysis]
    D -->|No issues| Z([Skip patient])
    E --> F[Inject config from GCS]
    F --> E
    E --> G{Alert needed?}
    G -->|Yes| H[alert_cards.py - build rich card]
    G -->|No| Z
    H --> I[chat_card_sender.py - Google Chat API]
    I --> J[Clinician sees card in Google Chat]
    J --> K[Clinician rates 1-5 and adds comment]
    K --> L[alert-feedback endpoint - main.py]
    L --> M[(cds_study.study_alert_feedback)]
    E --> N[(cds_study.study_alerts)]
    E --> O[(GCS - pipeline_traces)]
    O --> P[study_cost_tracker]
    P --> Q[(cds_study.pipeline_run_costs)]
```

## Config Injection

Before the LLM analyses each patient, the pipeline injects relevant config from GCS into the system prompt:

```mermaid
flowchart LR
    GCS[(GCS Bucket)] --> A
    GCS --> B
    GCS --> C
    A[monitoring_protocols - permissive-hypertension etc.] -->|context-gate: suppress if permissive scenario applies| PT[problem_tracker LLM call]
    B[app_settings - lab_alert_rules.json] -->|alert floor: only fire if threshold crossed| PT
    C[app_settings - symptom_alert_rules.json] -->|objective criteria required| PT
```

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
