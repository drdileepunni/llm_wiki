# Config Reference

All pipeline configuration is stored as JSON blobs in the GCS bucket `patientview-cds-pipeline-ops`. No code change or redeploy is needed to update config — just upload a new JSON blob.

## GCS Layout

```
patientview-cds-pipeline-ops/
├── monitoring_protocols/
│   ├── permissive-hypertension.json          <- 7 scenarios: BP suppression
│   ├── established-low-gcs.json              <- 4 scenarios: chronic low GCS suppression
│   ├── permissive-respiratory.json           <- 2 scenarios: post-op / baseline hypoxia
│   ├── haemoglobin-alert-criteria.json       <- 2 scenarios: Hb drop thresholds
│   └── lactate-alert-criteria.json           <- 2 scenarios: lactate + MAP hypoperfusion
├── app_settings/
│   ├── lab_alert_rules.json           <- per-lab thresholds
│   ├── symptom_alert_rules.json       <- objective criteria for symptoms
│   ├── alert_recipients.json          <- who gets the Google Chat alerts
│   ├── gchat_webhook.json             <- Chat space webhook URL
│   └── monitored_workspaces.json      <- which ICU workspaces to screen
└── clinical_context_rules.json        <- clinical context injection rules
```

## Monitoring Protocols

Monitoring protocols define **permissive scenarios** — situations where an elevated value (e.g. high BP) is *intentional* and should not trigger an alert.

Each protocol has:
- `applies_when` — substrings matched against the problem name (case-insensitive)
- `gate_question` — the question posed to the model to classify the scenario
- `scenarios[]` — each with a `band_description`, `window`, and `invalidate_if` list
- `escalation_target_after_window` — guidance once the permissive window closes

Seed with: `source .venv/bin/activate && GCS_BUCKET=patientview-cds-pipeline-ops python -m tools.radar_sync.seed_monitoring_protocols`

**Current protocols:**

| Slug | Scenarios | Suppresses alerts when… |
|---|---|---|
| `permissive-hypertension` | 7 | BP elevation is expected/managed |
| `established-low-gcs` | 4 | GCS is chronically low with no acute change |
| `permissive-respiratory` | 2 | Post-op hypoxia/tachypnea or known baseline low SpO2 |
| `haemoglobin-alert-criteria` | 2 | Hb stable (drop ≤1 g/dL in 24h and above floor) |
| `lactate-alert-criteria` | 2 | Lactate 2–4 with no MAP <65; or MAP <65 with lactate already worked up |

## Lab Alert Rules

Rules live at `app_settings/lab_alert_rules.json`. Each rule:

| Field | Description |
|---|---|
| `lab` | Canonical lab name |
| `aliases` | Additional names matched in prefetch data |
| `absolute_floor` | Alert if current value is below this |
| `absolute_ceiling` | Alert if current value is above this |
| `delta_pct` | Alert if drop/rise is >= this % from prior value |
| `delta_abs` | Alert if drop/rise is > this absolute amount |
| `delta_direction` | `drop`, `rise`, or `both` |
| `logic` | `floor_OR_delta` or `floor_AND_delta` |

Seed with: `python -m tools.radar_sync.seed_lab_alert_rules`

## Symptom Alert Rules

Rules live at `app_settings/symptom_alert_rules.json`. Each rule:
- `problem` / `aliases` — matched against detected problem name
- `min_score` — minimum NRS/VAS score (if applicable)
- `objective_criteria` — at least one must be documented for an alert to fire
- `notes` — guidance to the model on edge cases

These prevent alerts on subjective complaints (pain, nausea) without objective clinical evidence.

## Operational Settings

| Setting | Key | Description |
|---|---|---|
| Alert recipients | `alert_recipients` | List of email addresses receiving Chat alerts |
| Webhook | `gchat_webhook` | Google Chat space URL |
| Workspaces | `monitored_workspaces` | List of workspace names (e.g. `["1A"]`) |

## Editing Config

All settings can be updated by uploading a new JSON file to the corresponding GCS path. No redeploy needed — the pipeline reads config fresh on every hourly run.

```bash
# Example: update lab alert rules
gsutil cp app_settings/lab_alert_rules.json \
  gs://patientview-cds-pipeline-ops/app_settings/lab_alert_rules.json
```
