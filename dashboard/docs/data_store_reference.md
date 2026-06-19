# Data Store Reference

All pipeline state lives in two stores: **GCS** (`patientview-cds-pipeline-ops`) for live patient
state and binary assets, and **BigQuery** (`cds_pipeline` dataset) for append-only audit trails.

> **Common key columns** — `CPMRN` (STRING) and `encounter` (INT64) appear in every collection
> and table. They are omitted from the field lists below for brevity.

> **Dev vs prod buckets** — `patientview-cds-pipeline-ops` is the production bucket used by Cloud
> Run. `cds-pipeline-ops` is the local dev fallback (nearly empty). Always prefix seed commands
> with `GCS_BUCKET=patientview-cds-pipeline-ops`.

---

## GCS — live patient state

Single file per patient, **overwritten** on every run.

### `patient_contexts/{CPMRN}_{encounter}.json`

Written by `summary_updater` (via scheduler). Updated every LLM run.

| Field | Description |
|---|---|
| `running_summary` | Plain-text narrative of the patient's clinical course |
| `last_snapshot_at` | ISO timestamp of the most recent chart snapshot used |
| `structured_summary` | Full object — see sub-fields below |
| `structured_summary.admission_narrative` | Admission context paragraph |
| `structured_summary.narrative` | Current overall narrative |
| `structured_summary.problems[]` | Active problem list (name, status, presenting_features, workup, management, current_state, plan_changing_event, cause) |
| `structured_summary.resolved_problems[]` | Problems marked resolved |
| `structured_summary.suggested_actions[]` | Suggested next steps |
| `lightweight_summary` | Cheap summary object — see sub-fields below |
| `lightweight_summary.overall_trajectory` | stable / improving / worsening |
| `lightweight_summary.pass1_flag` | Boolean — did pass-1 flag this patient? |
| `lightweight_summary.pass1_reason` | Reason string from pass-1 screener |
| `lightweight_summary.sensitivity_mode` | normal / high |
| `lightweight_summary.problems[]` | Condensed problem list (name, status, key_change, management_note) |
| `lightweight_summary.computed_at` | Timestamp |

⚠ **No version history** — single file is overwritten each run. Before/after comparison requires
reading `snapshots/` and `pipeline_traces/` to reconstruct context.

---

### `patient_problems/{CPMRN}_{encounter}.json`

Written by `problem_tracker.track_problems`. JSON **array** — one entry per active problem.
The entire array is rewritten on each tracker run.

| Field | Description |
|---|---|
| `problem_name` | Problem label |
| `clinical_status` | stable / worsening / improving / critical |
| `being_addressed` | Boolean — active management plan in place |
| `addressed_evidence` | Text justifying `being_addressed` |
| `last_assessed_at` | ISO timestamp of last tracker run |
| `first_detected_at` | ISO timestamp when problem was first added |
| `next_check` | Object — next follow-up trigger |
| `next_check.key` | Vital/lab key to watch (e.g. `HR`) |
| `next_check.label` | Human label (e.g. `HR post-intervention`) |
| `next_check.type` | vital / lab |
| `next_check.due_after` | ISO timestamp when next check is due |
| `reasoning_fingerprint` | Object — fingerprint of tracker reasoning for dedup |
| `reasoning_fingerprint.anchored_at` | Timestamp |
| `reasoning_fingerprint.reasoning_chain` | Abbreviated reasoning text |
| `last_alerted_at` | ISO timestamp of last alert fired for this problem |
| `last_alerted_news2` | NEWS2 score at time of last alert |
| `last_alerted_news2_components` | Per-component breakdown at last alert |
| `assessments[]` | Historical assessment log (assessed_at, clinical_status, being_addressed, addressed_evidence, next_check, alerted, should_alert, alert_reason, cited_notes) |

---

### `snapshot_schedule/{CPMRN}_{encounter}.json`

Scheduling and watermark state per patient. Written by scheduler and orchestrators every cycle.

| Field | Description |
|---|---|
| `workspace` | Ward/unit code (e.g. `1A`) |
| `active` | Boolean — is patient currently monitored? |
| `added_at` | ISO timestamp when patient was first enrolled |
| `last_collected_at` | ISO timestamp of last chart pull |
| `last_error` | Error string from last collection failure (null if clean) |
| `last_llm_run_at` | ISO timestamp of last full LLM run |
| `next_run_at` | ISO timestamp — cadence gate: skip if `now < next_run_at` |
| `deactivated_reason` | discharged / manual / null |
| `last_report_at` ⬛ | Watermark — newest report timestamp seen by report-interpret |
| `interpreted_doc_keys[]` ⬛ | Ledger of doc keys/group IDs already interpreted (bounded 200) |
| `last_recon_at` ⬛ | Watermark — newest med doc timestamp seen by med-recon |
| `reconciled_doc_keys[]` ⬛ | Ledger of doc keys already reconciled |

⬛ **Watermark fields** — these gate whether report-interpret and med-recon run this cycle.
Omitting `GCS_BUCKET` when seeding writes to the wrong bucket and leaves old watermarks in place.

---

### `proposed_order_actions/{CPMRN}_{encounter}.json`

Written by `med_recon.orchestrator` when a reconciliation card is produced.
Stores pending order actions (discontinue / route change) awaiting clinician confirmation.

| Field | Description |
|---|---|
| `_id` | Action set ID |
| `actions` | Map of `{key: {kind, order_no, order_label, status}}` |

---

## GCS — append-only audit logs

One file per event, **never overwritten**.

### `snapshots/{CPMRN}_{encounter}/{snapshot_at_iso}.json`

Full raw EMR chart payload at a point in time. Written by `snapshot_collector.collect` every
hourly cycle. Used by `delta_extractor` to diff against the previous snapshot.

| Field | Description |
|---|---|
| `snapshot_at` | ISO timestamp |
| `chart` | Full chart object |
| `chart.vitals[]` | Pre-filtered vital rows |
| `chart.documents[]` | Lab + imaging documents |
| `chart.orders.active / .pending / .completed` | Current orders |
| `chart.notes.finalNotes[]` | Clinical notes |
| `chart.io.days[]` | Intake/output records |

---

### `pipeline_traces/{ts}_{CPMRN}_{encounter}.json`

One file per LLM step per run. Written by `react_tracer.ReActTracer`. Rolled up hourly by
`study_cost_tracker` into BigQuery `pipeline_run_costs`.

| Field | Description |
|---|---|
| `step` | Step name: `pass1_screener`, `summary_update`, `problem_tracker`, `report_interpret`, … |
| `started_at` | ISO timestamp |
| `duration_ms` | Wall-clock milliseconds |
| `total_rounds` | Number of tool-call rounds |
| `total_tokens` | `{in, out, thinking}` |
| `final_output` | Step-specific result object |
| `rounds[]` | Per-round detail: round, started_at, thinking, tool_calls, tool_results, text, tokens, duration_ms |

---

## GCS — binary/media assets

### `report_images/report_images/{CPMRN}_{enc}/{batch_id}/{report_id}/…`

Binary JPEG/PNG image frames hosted for display in GChat report-interpret cards.
Written by `med_recon.image_host` when a report-interpret card is produced.
URL stored in BQ `report_interpret_audit.image_urls`.

### `med_recon_images/{CPMRN}_{enc}/{batch_id}/…`

Binary JPEG/PNG medication chart pages hosted for display in GChat med-recon cards.

### `note_indexes/{CPMRN}_{encounter}/`

FAISS binary index + hash file for semantic note search.
Written by `mongo_cache.save_index` when note content changes.
Used by `fn_detector` and `status_classifier` for vector similarity search over clinical notes.

---

## GCS — configuration (not written per run)

### `app_settings/{_id}.json`

| `_id` | Key fields |
|---|---|
| `alert_recipients` | `emails[]`, `enabled` |
| `gchat_webhook` | `url`, `enabled` |
| `lab_alert_rules` | `rules[]`, `enabled` |
| `lab_staleness_overrides` | `overrides{}`, `enabled` |
| `med_recon_config` | `enabled`, `categories[]`, `name_patterns[]`, `key_field` |
| `monitored_workspaces` | `workspaces[]` |

### `monitoring_protocols/{protocol_id}.json`

Six active protocols. Each defines `applies_when[]` (substring match against `problem_name`),
`scenarios[]`, and gate verdicts per scenario.

| Protocol | Scenarios | Purpose |
|---|---|---|
| `permissive-hypertension` | 7 | Suppress BP alerts in managed hypertension |
| `established-low-gcs` | 4 | Suppress GCS alerts for chronic low GCS |
| `permissive-respiratory` | 2 | Suppress hypoxia/tachypnea in post-op or chronic baseline |
| `haemoglobin-alert-criteria` | 2 | Alert only on ≥1 g/dL drop in 24h or below floor |
| `lactate-alert-criteria` | 2 | Alert lactate >4 always; 2–4 needs MAP <65 context |
| `acknowledged-myocardial-injury` | — | Suppress alerts for known/expected myocardial injury |

Gate verdicts: `permissive_active` (suppress) · `permissive_breached` (alert) · `permissive_ended`
(permanent close) · `no_permissive_context` (normal rules apply)

---

## BigQuery — alerts and study tables

Dataset: `cds_pipeline`. All tables are append-only.

### `study_alerts`

One row per alert fired. Written by `problem_tracker` → `bq_store.insert_alert`.

| Column | Type | Description |
|---|---|---|
| `alert_id` | STRING | Primary key |
| `problem_name` | STRING | Problem that triggered the alert |
| `alert_title` | STRING | Short alert headline |
| `alert_reason` | STRING | Full reasoning text |
| `note_vs_objective` | STRING | Whether alert is driven by notes or objective data |
| `alerted_at` | TIMESTAMP | When the alert was fired |
| `match_status` | STRING | pending / matched / tp_confirmed / fp_confirmed |
| `created_at` | TIMESTAMP | Row insertion time |

`match_status` is updated by `study_matcher` (→ matched) and manual adjudication (→ tp/fp_confirmed).

---

### `study_alert_feedback`

One row per clinician star-rating on an alert GChat card.
Written by `bq_store.insert_alert_feedback`.

| Column | Description |
|---|---|
| `alert_id` | FK to study_alerts |
| `rating` | INT — star rating (1–5) |
| `feedback_text` | Optional free text |
| `user_email`, `user_display` | Clinician identity |
| `space_name`, `message_name` | GChat message identifiers |

---

### `study_sbar_import`

Radar SBAR records imported for matching against `study_alerts`.
Written hourly by `study_sbar_syncer` → `bq_store.upsert_sbar` (MERGE upsert by `sbar_id`).

| Column | Description |
|---|---|
| `sbar_id` | Primary key from Radar |
| `hospital_name`, `unit_name` | Location |
| `urgency` | Urgency level |
| `issues` | Clinical issues text |
| `create_date_time` | When SBAR was created in Radar |
| `is_reviewed`, `reviewer_name`, `action` | Review state |
| `window_expires_at` | Matching window closes after 6 hours |
| `match_status`, `matched_alert_id` | Matching outcome |
| `synced_at` | Last sync time |

---

### `study_task_import`

Radar task records. Same structure/purpose as SBAR import but for task-type events.
Written by `study_task_syncer` → `bq_store.upsert_task`.

---

### `study_adjudications`

Manual TP/FP adjudication decisions by clinical reviewers.

| Column | Description |
|---|---|
| `alert_id` | FK to study_alerts |
| `adjudication` | tp_confirmed / fp_confirmed |
| `adjudicator_email` | Reviewer identity |
| `notes` | Free text |
| `created_at` | Decision timestamp |

---

## BigQuery — medication reconciliation

### `med_recon_audit`

One row per med-reconciliation run (when new medication chart documents are found).
Written by `med_recon.orchestrator` → `bq_store.insert_med_recon`.

| Column | Description |
|---|---|
| `recon_id` | Primary key |
| `snapshot_at`, `recon_at` | Cycle start / analysis completion timestamps |
| `document_file_keys` | JSON array — GCS keys of reconciled documents |
| `document_reported_ats` | JSON array — document timestamps |
| `document_categories` | JSON array — document categories |
| `summary_narrative` | Patient narrative used as context |
| `raw_transcription` | JSON — raw OCR/extraction output |
| `extracted_meds` | JSON — medications found in chart images |
| `active_orders_snapshot` | JSON — active orders at time of recon |
| `recon_items` | JSON — final reconciliation findings |
| `proposed_actions` | JSON — proposed order actions |
| `action_set_id` | ID linking to proposed_order_actions GCS doc |
| `image_urls` | JSON — hosted chart image URLs |
| `discrepancy_count` | Number of discrepancies found |
| `card_sent` | BOOL — was GChat card sent? |
| `recipients` | JSON — recipient list |
| `dedup_of` | recon_id if this was suppressed as a duplicate |
| `step_costs` | JSON — `{input_tokens, output_tokens, cost_usd}` |

---

### `med_recon_actions`

One row per order action applied when a clinician submits a med-recon card.
Written by `bq_store.insert_med_recon_action`.

| Column | Description |
|---|---|
| `recon_id`, `action_set_id` | FK to med_recon_audit |
| `action_kind` | discontinue / route_change |
| `action_key` | Internal order key |
| `order_label` | Human-readable order name |
| `result_ok` | BOOL — did the API call succeed? |
| `result_status`, `result_error` | HTTP status / error detail |
| `applied_by_email`, `applied_by_display` | Clinician who submitted |
| `applied_at` | TIMESTAMP |

---

## BigQuery — diagnostic report interpretation

### `report_interpret_audit`

One row per interpreted diagnostic report document.
Written by `report_interpret.audit` → `bq_store.insert_report_interpret`.

| Column | Description |
|---|---|
| `report_id` | Primary key |
| `batch_id` | Groups reports interpreted in the same cycle |
| `snapshot_at`, `interpreted_at` | Cycle start / interpretation completion |
| `document_file_key` | Primary GCS key of the source document |
| `document_category`, `document_name` | Category and name from Radar |
| `reported_at` | Document timestamp from EMR |
| `selection_reason` | Why this document was selected this cycle |
| `download_ok`, `interpret_ok` | BOOL — pipeline step success flags |
| `report_type` | echo / xray / ct / other |
| `confidence` | low / medium / high |
| `findings` | JSON array of `{label, body_system, severity, is_new_diagnosis, supporting_evidence}` |
| `raw_description` | Raw LLM description output |
| `interpretation` | LLM interpretation narrative |
| `image_urls` | JSON — hosted image URLs for card display |
| `card_sent` | BOOL |
| `step_costs` | JSON — `{input_tokens, output_tokens, cost_usd}` |

Multi-frame exams (e.g. ECHO with multiple image frames) are grouped by `(name, reportedAt-minute)`
by `doc_selector` and arrive as a single row here.

---

### `report_interpret_runs`

One row **per patient per cycle** on every path — including no_new_docs, disabled, error.
Written by `report_interpret.audit` → `bq_store.insert_report_run`.

| Column | Description |
|---|---|
| `run_id` | Primary key |
| `batch_id` | Non-empty only when reports were processed |
| `cycle_started_at`, `evaluated_at` | Cycle timestamps |
| `outcome` | no_new_docs / disabled / processed / send_failed / skipped_cycle_limit / error |
| `n_reports_found` | Total candidate documents found |
| `n_reports_interpreted` | Documents that went through the LLM |
| `findings_injected` | Total findings injected into delta for problem_tracker |
| `problems_touched` | ⚠ Always 0 — stub, not yet populated |
| `watermark_before`, `watermark_after` | Watermark timestamps before/after this cycle |
| `cost_usd` | FLOAT — LLM cost for this cycle |
| `error_detail` | Error string on failure paths |

---

### `report_interpret_feedback`

One row per clinician rating on a report-interpret GChat card.

| Column | Description |
|---|---|
| `report_id`, `batch_id` | FK to report_interpret_audit |
| `report_type` | echo / xray / ct / other |
| `rating` | INT — star rating |
| `feedback_text` | Optional free text |
| `user_email`, `user_display` | Clinician identity |

---

## BigQuery — pipeline cost accounting

### `pipeline_run_costs`

One row per hourly run, inserted after all patients have been processed.
Written by `study_cost_tracker` → `bq_store.insert_run_cost`. Aggregates all `pipeline_traces/`
GCS files from the cycle.

| Column | Description |
|---|---|
| `run_started_at` | Cycle start timestamp |
| `computed_at` | When the rollup was computed |
| `patient_count` | Patients processed this cycle |
| `trace_count` | Number of pipeline_trace files aggregated |
| `model` | Model name |
| `totals` | JSON — `{input_tokens, output_tokens, thinking_tokens, cost_usd}` |
| `by_step` | JSON — cost broken down by step name (pass1_screener, summary_update, problem_tracker, report_interpret, …) |
| `pricing` | JSON — per-token pricing used |
| `patient_tiers` | JSON — per-patient cost breakdown |
