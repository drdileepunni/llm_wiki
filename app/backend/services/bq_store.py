"""
BigQuery-backed store for CDS study data.

All study output collections (study_alerts, study_sbar_import, study_task_import,
study_suppressed_events, study_metrics_snapshots, pipeline_run_costs) are stored
in the `cds_study` dataset in BigQuery.

Tables are created automatically on first use via CREATE TABLE IF NOT EXISTS DDL.

Auth: Workload Identity / Application Default Credentials — no key file needed
      when running on Cloud Run with the correct runtime service account.
      Locally: set GOOGLE_APPLICATION_CREDENTIALS or use
                gcloud auth application-default login.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from google.cloud import bigquery

log = logging.getLogger(__name__)

_PROJECT = "patientview-9uxml"
_DATASET = "cds_study"
_LOCATION = "asia-south1"


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_to_iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    return str(v)


def _to_json_col(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except Exception:
        return str(v)


def _new_id() -> str:
    return str(uuid.uuid4())


# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL: dict[str, str] = {}
_ALTER_DDL: dict[str, str] = {}  # ALTER TABLE migrations run once after table is ensured

_DDL["study_alerts"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_alerts` (
  alert_id               STRING    NOT NULL,
  CPMRN                  STRING,
  encounter              INT64,
  problem_name           STRING,
  alert_title            STRING,
  alert_reason           STRING,
  note_vs_objective      STRING,
  alerted_at             TIMESTAMP,
  match_status           STRING,
  matched_sbar_id        STRING,
  matched_task_id        STRING,
  llm_match_confidence   FLOAT64,
  llm_match_reasoning    STRING,
  fp_candidate_at        TIMESTAMP,
  adjudicated_at         TIMESTAMP,
  alert_source           STRING,
  protocol_ids           STRING,
  created_at             TIMESTAMP
)
OPTIONS (description = "CDS system alerts — llm_reasoned (problem_tracker) and care_gap_shortcut (rule-based overdue-check nudge) alerts")
"""

_ALTER_DDL["study_alerts"] = f"""
ALTER TABLE `{_PROJECT}.{_DATASET}.study_alerts`
ADD COLUMN IF NOT EXISTS alert_source STRING,
ADD COLUMN IF NOT EXISTS protocol_ids STRING
"""

_DDL["study_sbar_import"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_sbar_import` (
  sbar_id               STRING    NOT NULL,
  CPMRN                 STRING,
  encounter             INT64,
  hospital_name         STRING,
  unit_name             STRING,
  urgency               STRING,
  issues                STRING,
  module                STRING,
  create_date_time      TIMESTAMP,
  is_reviewed           BOOL,
  reviewer_name         STRING,
  action                STRING,
  window_expires_at     TIMESTAMP,
  match_status          STRING,
  matched_alert_id      STRING,
  llm_match_confidence  FLOAT64,
  llm_match_reasoning   STRING,
  matched_at            TIMESTAMP,
  confirmed_fn_at       TIMESTAMP,
  excluded_at           TIMESTAMP,
  exclusion_reason      STRING,
  dedup_reason          STRING,
  fn_verdict            STRING,
  fn_excusal_reason     STRING,
  fn_reviewer_notes     STRING,
  fn_adjudicated_at     TIMESTAMP,
  synced_at             TIMESTAMP
)
OPTIONS (description = "SBARs synced from BigQuery patient tables")
"""

_DDL["study_task_import"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_task_import` (
  task_id               STRING    NOT NULL,
  CPMRN                 STRING,
  encounter             INT64,
  hospital_name         STRING,
  unit_name             STRING,
  title                 STRING,
  issues                STRING,
  priority              STRING,
  status                STRING,
  task_visible_at       TIMESTAMP,
  window_expires_at     TIMESTAMP,
  match_status          STRING,
  matched_alert_id      STRING,
  llm_match_confidence  FLOAT64,
  llm_match_reasoning   STRING,
  matched_at            TIMESTAMP,
  confirmed_fn_at       TIMESTAMP,
  excluded_at           TIMESTAMP,
  exclusion_reason      STRING,
  dedup_reason          STRING,
  deduplicated_at       TIMESTAMP,
  fn_verdict            STRING,
  fn_excusal_reason     STRING,
  fn_reviewer_notes     STRING,
  fn_adjudicated_at     TIMESTAMP,
  synced_at             TIMESTAMP
)
OPTIONS (description = "Review Abnormal Vitals tasks synced from BigQuery")
"""

_DDL["study_suppressed_events"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_suppressed_events` (
  CPMRN               STRING,
  encounter           INT64,
  problem_name        STRING,
  suppressed_at       TIMESTAMP,
  suppression_reason  STRING,
  created_at          TIMESTAMP
)
OPTIONS (description = "Alert events suppressed by cooldown logic")
"""

_DDL["study_metrics_snapshots"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_metrics_snapshots` (
  computed_at              TIMESTAMP,
  tp                       INT64,
  fp                       INT64,
  fn                       INT64,
  fn_sbar                  INT64,
  fn_task                  INT64,
  tn                       INT64,
  total_patient_hours      INT64,
  sensitivity              FLOAT64,
  specificity              FLOAT64,
  ppv                      FLOAT64,
  npv                      FLOAT64,
  f1                       FLOAT64,
  sensitivity_ci           STRING,
  specificity_ci           STRING,
  ppv_ci                   STRING,
  lead_time_median_minutes FLOAT64,
  lead_time_iqr_minutes    FLOAT64,
  lead_time_n              INT64,
  suppressed_total         INT64,
  suppressed_with_sbar     INT64,
  pending_adjudication     INT64,
  adj_total                INT64,
  adj_appropriate          INT64,
  adj_inappropriate        INT64,
  expl_clear               INT64,
  expl_partial             INT64,
  expl_unclear             INT64,
  alerts_total             INT64,
  sbars_total              INT64,
  tasks_total              INT64,
  excluded_downtime        INT64,
  task_excluded_downtime   INT64,
  task_deduplicated        INT64
)
OPTIONS (description = "Hourly study metrics snapshots")
"""

_DDL["pipeline_run_costs"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.pipeline_run_costs` (
  run_started_at   TIMESTAMP,
  computed_at      TIMESTAMP,
  patient_count    INT64,
  trace_count      INT64,
  model            STRING,
  totals           STRING,
  by_step          STRING,
  pricing          STRING,
  patient_tiers    STRING
)
OPTIONS (description = "Per-run LLM token costs")
"""

_ALTER_DDL["pipeline_run_costs"] = f"""
ALTER TABLE `{_PROJECT}.{_DATASET}.pipeline_run_costs`
ADD COLUMN IF NOT EXISTS patient_tiers STRING
"""

_DDL["study_adjudications"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_adjudications` (
  alert_id              STRING,
  verdict               STRING,
  explainability_rating INT64,
  reviewer_notes        STRING,
  reviewed_at           TIMESTAMP
)
OPTIONS (description = "Manual adjudications from review UI")
"""

_DDL["study_alert_feedback"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.study_alert_feedback` (
  alert_id       STRING,
  CPMRN          STRING,
  encounter      INT64,
  problem_name   STRING,
  rating         INT64,
  feedback_text  STRING,
  user_email     STRING,
  user_display   STRING,
  space_name     STRING,
  message_name   STRING,
  created_at     TIMESTAMP
)
OPTIONS (description = "Clinician 1-5 appropriateness ratings from alert chat cards")
"""

_DDL["med_recon_audit"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.med_recon_audit` (
  recon_id               STRING    NOT NULL,
  CPMRN                  STRING,
  encounter              INT64,
  snapshot_at            TIMESTAMP,
  recon_at               TIMESTAMP,
  document_file_keys     STRING,
  document_reported_ats  STRING,
  document_categories    STRING,
  summary_narrative      STRING,
  raw_transcription      STRING,
  extracted_meds         STRING,
  active_orders_snapshot STRING,
  recon_items            STRING,
  proposed_actions       STRING,
  action_set_id          STRING,
  image_urls             STRING,
  model                  STRING,
  discrepancy_count      INT64,
  card_sent              BOOL,
  recipients             STRING,
  dedup_of               STRING,
  step_costs             STRING,
  created_at             TIMESTAMP
)
OPTIONS (description = "Medication reconciliation audit — full per-run trace")
"""

_DDL["med_recon_actions"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.med_recon_actions` (
  recon_id           STRING,
  action_set_id      STRING,
  CPMRN              STRING,
  encounter          INT64,
  selected_keys      STRING,
  action_kind        STRING,
  action_key         STRING,
  order_label        STRING,
  result_ok          BOOL,
  result_status      INT64,
  result_error       STRING,
  applied_by_email   STRING,
  applied_by_display STRING,
  space_name         STRING,
  message_name       STRING,
  applied_at         TIMESTAMP
)
OPTIONS (description = "Applied med-recon order actions — who/what/when/result")
"""

_DDL["report_interpret_audit"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.report_interpret_audit` (
  report_id          STRING    NOT NULL,
  batch_id           STRING,
  CPMRN              STRING,
  encounter          INT64,
  snapshot_at        TIMESTAMP,
  interpreted_at     TIMESTAMP,
  document_file_key  STRING,
  document_category  STRING,
  document_name      STRING,
  reported_at        STRING,
  selection_reason   STRING,
  download_ok        BOOL,
  interpret_ok       BOOL,
  summary_narrative  STRING,
  raw_description    STRING,
  interpretation     STRING,
  report_type        STRING,
  confidence         STRING,
  findings           STRING,
  image_urls         STRING,
  model              STRING,
  card_sent          BOOL,
  recipients         STRING,
  step_costs         STRING,
  created_at         TIMESTAMP
)
OPTIONS (description = "Diagnostic-report interpretation audit — one row per interpreted report")
"""

_DDL["report_interpret_runs"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.report_interpret_runs` (
  run_id                STRING    NOT NULL,
  batch_id              STRING,
  CPMRN                 STRING,
  encounter             INT64,
  cycle_started_at      TIMESTAMP,
  evaluated_at          TIMESTAMP,
  outcome               STRING,
  n_reports_found       INT64,
  n_reports_interpreted INT64,
  findings_injected     INT64,
  problems_touched      INT64,
  card_sent             BOOL,
  recipients            STRING,
  watermark_before      TIMESTAMP,
  watermark_after       TIMESTAMP,
  cost_usd              FLOAT64,
  error_detail          STRING,
  created_at            TIMESTAMP
)
OPTIONS (description = "Report-interpret per-patient-per-cycle run outcome — written on every path")
"""

_DDL["report_interpret_feedback"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.report_interpret_feedback` (
  report_id      STRING,
  batch_id       STRING,
  CPMRN          STRING,
  encounter      INT64,
  report_type    STRING,
  rating         INT64,
  feedback_text  STRING,
  user_email     STRING,
  user_display   STRING,
  space_name     STRING,
  message_name   STRING,
  created_at     TIMESTAMP
)
OPTIONS (description = "Clinician 1-5 usefulness ratings from report-interpret chat cards")
"""

_DDL["pipeline_patient_runs"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.pipeline_patient_runs` (
  run_started_at   TIMESTAMP,
  CPMRN            STRING,
  encounter        INT64,
  pipeline_outcome STRING,
  pass1_tag        STRING,
  pass1_needs_full BOOL,
  pass2_outcome    STRING,
  problem_details  STRING,
  delta_vitals     INT64,
  delta_labs       INT64,
  delta_notes      INT64,
  delta_reports    INT64,
  trigger_reason   STRING,
  problems_scoped  STRING,
  gate_trace       STRING,
  created_at       TIMESTAMP
)
OPTIONS (description = "Per-patient-per-run audit trace for the dashboard run-audit table")
"""

_ALTER_DDL["pipeline_patient_runs"] = f"""
ALTER TABLE `{_PROJECT}.{_DATASET}.pipeline_patient_runs`
ADD COLUMN IF NOT EXISTS delta_vitals    INT64,
ADD COLUMN IF NOT EXISTS delta_labs      INT64,
ADD COLUMN IF NOT EXISTS delta_notes     INT64,
ADD COLUMN IF NOT EXISTS delta_reports   INT64,
ADD COLUMN IF NOT EXISTS trigger_reason  STRING,
ADD COLUMN IF NOT EXISTS problems_scoped STRING,
ADD COLUMN IF NOT EXISTS gate_trace      STRING
"""

_DDL["fn_adjudications"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.fn_adjudications` (
  record_id        STRING,
  source           STRING,
  verdict          STRING,
  new_status       STRING,
  excusal_reason   STRING,
  reviewer_notes   STRING,
  adjudicated_at   TIMESTAMP,
  CPMRN            STRING,
  encounter        INT64
)
OPTIONS (description = "FN adjudications from review UI")
"""

_DDL["patient_next_checks"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.patient_next_checks` (
  run_started_at    TIMESTAMP,
  CPMRN             STRING,
  encounter         INT64,
  problem_name      STRING,
  nc_type           STRING,
  nc_key            STRING,
  nc_label          STRING,
  due_after         TIMESTAMP,
  clinical_status   STRING,
  tracker_reasoning STRING,
  protocol_ids      STRING,
  created_at        TIMESTAMP
)
OPTIONS (description = "Per-problem next_check state captured at each pipeline run — NULL due_after means cleared/resolved")
"""

_ALTER_DDL["patient_next_checks"] = f"""
ALTER TABLE `{_PROJECT}.{_DATASET}.patient_next_checks`
ADD COLUMN IF NOT EXISTS protocol_ids STRING
"""

_DDL["documentation_audit_queue"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.documentation_audit_queue` (
  queue_id      STRING    NOT NULL,
  CPMRN         STRING,
  encounter     INT64,
  problem_name  STRING,
  protocol_id   STRING,
  detected_at   TIMESTAMP,
  audit_due_at  TIMESTAMP,
  enqueued_at   TIMESTAMP
)
OPTIONS (description = "Audit work queue — one row per (patient, problem) that needs a documentation audit. Deduped by queue_id at sweep time.")
"""

_DDL["documentation_audit"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.documentation_audit` (
  audit_id           STRING    NOT NULL,
  CPMRN              STRING,
  encounter          INT64,
  problem_name       STRING,
  protocol_id        STRING,
  detected_at        TIMESTAMP,
  audited_at         TIMESTAMP,
  window_hours       INT64,
  verdict            STRING,
  note_count         INT64,
  required_items     STRING,
  documented_items   STRING,
  missing_items      STRING,
  created_at         TIMESTAMP
)
OPTIONS (description = "Documentation audit results — fired 12h after first problem detection per protocol audit spec")
"""

_DDL["problem_resolution_events"] = f"""
CREATE TABLE IF NOT EXISTS `{_PROJECT}.{_DATASET}.problem_resolution_events` (
  event_id                       STRING NOT NULL,
  CPMRN                          STRING,
  encounter                      INT64,
  problem_name                   STRING,
  protocol_ids                   STRING,
  first_detected_at              TIMESTAMP,
  resolved_at                    TIMESTAMP,
  duration_monitored_hours       FLOAT64,
  resolution_status              STRING,
  resolution_reasoning           STRING,
  last_status_before_resolution  STRING,
  was_ever_alerted               BOOL,
  alert_attempts_total           INT64,
  created_at                     TIMESTAMP
)
OPTIONS (description = "One row per problem-resolution transition — captures how a monitored problem closed (self-resolved vs alerted) for alert-burden analysis")
"""


# ── BQStudyStore ──────────────────────────────────────────────────────────────

class BQStudyStore:
    """
    Provides typed read/write methods for all study collections in BigQuery.

    Usage:
        store = get_bq_store()
        store.insert_alert({...})
        rows = store.find_alerts({"match_status": "pending", ...})
        store.update_alert_status(alert_id, "matched", matched_sbar_id="...")
    """

    def __init__(self, project: str = _PROJECT, dataset: str = _DATASET, location: str = "asia-south1"):
        self._project  = project
        self._dataset  = dataset
        self._location = location
        self._client   = bigquery.Client(project=project)
        self._tables_ensured: set[str] = set()
        self._ensure_dataset()

    def _fqn(self, table: str) -> str:
        return f"`{self._project}.{self._dataset}.{table}`"

    def _ensure_dataset(self):
        """Create the dataset in asia-south1 if it doesn't exist."""
        from google.cloud.bigquery import Dataset, DatasetReference
        ref = DatasetReference(self._project, self._dataset)
        try:
            self._client.get_dataset(ref)
        except Exception:
            ds = Dataset(ref)
            ds.location = self._location
            self._client.create_dataset(ds, exists_ok=True)
            log.info("bq_store: created dataset %s.%s in %s", self._project, self._dataset, self._location)

    def _ensure_table(self, table: str):
        if table in self._tables_ensured:
            return
        ddl = _DDL.get(table)
        if not ddl:
            log.warning("bq_store: no DDL for table %s — skipping ensure", table)
            return
        try:
            job_cfg = bigquery.QueryJobConfig(default_dataset=f"{self._project}.{self._dataset}")
            self._client.query(ddl, job_config=job_cfg, location=self._location).result()
            log.info("bq_store: ensured table %s.%s", self._dataset, table)
            self._tables_ensured.add(table)
        except Exception:
            log.exception("bq_store: failed to ensure table %s", table)
        alter_ddl = _ALTER_DDL.get(table)
        if alter_ddl:
            try:
                self._client.query(alter_ddl, location=self._location).result()
                log.info("bq_store: applied alter migration for %s.%s", self._dataset, table)
            except Exception:
                log.exception("bq_store: alter migration failed for %s (may already exist)", table)

    def _query(self, sql: str, params: list | None = None) -> list[dict]:
        """Execute a SELECT and return rows as list of dicts."""
        job_config = bigquery.QueryJobConfig(query_parameters=params or [])
        try:
            rows = self._client.query(sql, job_config=job_config, location=self._location).result()
            return [dict(row) for row in rows]
        except Exception:
            log.exception("bq_store: query failed\n%s", sql)
            return []

    def _execute(self, sql: str, params: list | None = None) -> int:
        """Execute a DML (INSERT/UPDATE/MERGE) and return affected row count."""
        job_config = bigquery.QueryJobConfig(query_parameters=params or [])
        try:
            job = self._client.query(sql, job_config=job_config, location=self._location)
            job.result()
            return job.num_dml_affected_rows or 0
        except Exception:
            log.exception("bq_store: DML failed\n%s", sql)
            return 0

    # ── study_alerts ──────────────────────────────────────────────────────────

    def insert_alert(self, doc: dict) -> str:
        """
        Insert a new alert row. Returns the generated alert_id.

        alert_source distinguishes LLM-reasoned alerts from problem_tracker
        ("llm_reasoned", the default) from the rule-based overdue-check nudge
        fired by scheduler._send_missing_care_gap_alerts ("care_gap_shortcut").
        """
        self._ensure_table("study_alerts")
        alert_id = doc.get("alert_id") or _new_id()
        protocol_ids = doc.get("protocol_ids") or []
        if isinstance(protocol_ids, list):
            protocol_ids = ",".join(protocol_ids)
        sql = f"""
        INSERT INTO {self._fqn("study_alerts")}
          (alert_id, CPMRN, encounter, problem_name, alert_title, alert_reason,
           note_vs_objective, alerted_at, match_status, alert_source, protocol_ids, created_at)
        VALUES
          (@alert_id, @CPMRN, @encounter, @problem_name, @alert_title, @alert_reason,
           @note_vs_objective, @alerted_at, @match_status, @alert_source, @protocol_ids, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("alert_id",     "STRING",    alert_id),
            bigquery.ScalarQueryParameter("CPMRN",        "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",    "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("problem_name", "STRING",    doc.get("problem_name", "")),
            bigquery.ScalarQueryParameter("alert_title",  "STRING",    doc.get("alert_title", "")),
            bigquery.ScalarQueryParameter("alert_reason",      "STRING",    doc.get("alert_reason", "")),
            bigquery.ScalarQueryParameter("note_vs_objective", "STRING",    doc.get("note_vs_objective", "")),
            bigquery.ScalarQueryParameter("alerted_at",        "TIMESTAMP", _dt_to_iso(doc.get("alerted_at"))),
            bigquery.ScalarQueryParameter("match_status", "STRING",    doc.get("match_status", "pending")),
            bigquery.ScalarQueryParameter("alert_source", "STRING",    doc.get("alert_source") or "llm_reasoned"),
            bigquery.ScalarQueryParameter("protocol_ids", "STRING",    protocol_ids),
            bigquery.ScalarQueryParameter("created_at",   "TIMESTAMP", _now_iso()),
        ])
        return alert_id

    def insert_alert_feedback(self, doc: dict):
        """Insert a clinician rating row from an alert chat card."""
        self._ensure_table("study_alert_feedback")
        sql = f"""
        INSERT INTO {self._fqn("study_alert_feedback")}
          (alert_id, CPMRN, encounter, problem_name, rating, feedback_text,
           user_email, user_display, space_name, message_name, created_at)
        VALUES
          (@alert_id, @CPMRN, @encounter, @problem_name, @rating, @feedback_text,
           @user_email, @user_display, @space_name, @message_name, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("alert_id",      "STRING",    doc.get("alert_id", "")),
            bigquery.ScalarQueryParameter("CPMRN",         "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",     "INT64",     doc.get("encounter")),
            bigquery.ScalarQueryParameter("problem_name",  "STRING",    doc.get("problem_name", "")),
            bigquery.ScalarQueryParameter("rating",        "INT64",     doc.get("rating")),
            bigquery.ScalarQueryParameter("feedback_text", "STRING",    doc.get("feedback_text")),
            bigquery.ScalarQueryParameter("user_email",    "STRING",    doc.get("user_email", "")),
            bigquery.ScalarQueryParameter("user_display",  "STRING",    doc.get("user_display", "")),
            bigquery.ScalarQueryParameter("space_name",    "STRING",    doc.get("space_name", "")),
            bigquery.ScalarQueryParameter("message_name",  "STRING",    doc.get("message_name", "")),
            bigquery.ScalarQueryParameter("created_at",    "TIMESTAMP", _now_iso()),
        ])

    def update_alert(self, alert_id: str, fields: dict):
        """Update arbitrary fields on an alert by alert_id."""
        self._ensure_table("study_alerts")
        if not fields:
            return
        set_clauses = []
        params = [bigquery.ScalarQueryParameter("alert_id", "STRING", alert_id)]
        for k, v in fields.items():
            param_name = f"p_{k}"
            if isinstance(v, datetime):
                params.append(bigquery.ScalarQueryParameter(param_name, "TIMESTAMP", _dt_to_iso(v)))
            elif isinstance(v, float):
                params.append(bigquery.ScalarQueryParameter(param_name, "FLOAT64", v))
            elif isinstance(v, int):
                params.append(bigquery.ScalarQueryParameter(param_name, "INT64", v))
            else:
                params.append(bigquery.ScalarQueryParameter(param_name, "STRING", str(v) if v is not None else None))
            set_clauses.append(f"{k} = @{param_name}")
        sql = f"""
        UPDATE {self._fqn("study_alerts")}
        SET {', '.join(set_clauses)}
        WHERE alert_id = @alert_id
        """
        self._execute(sql, params)

    def bulk_update_alerts(self, filter_sql: str, set_sql: str, params: list):
        """Generic UPDATE with caller-provided WHERE and SET fragments."""
        self._ensure_table("study_alerts")
        sql = f"""
        UPDATE {self._fqn("study_alerts")}
        SET {set_sql}
        WHERE {filter_sql}
        """
        return self._execute(sql, params)

    def find_alerts(self, filter: dict) -> list[dict]:
        """SELECT alerts matching filter dict (CPMRN, encounter, match_status, alerted_at range)."""
        self._ensure_table("study_alerts")
        clauses, params = _build_where("study_alerts", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('study_alerts')} WHERE {where}"
        return self._query(sql, params)

    def count_alerts(self, filter: dict) -> int:
        self._ensure_table("study_alerts")
        clauses, params = _build_where("study_alerts", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT COUNT(*) AS n FROM {self._fqn('study_alerts')} WHERE {where}"
        rows = self._query(sql, params)
        return rows[0]["n"] if rows else 0

    # ── study_sbar_import ─────────────────────────────────────────────────────

    def upsert_sbar(self, doc: dict) -> bool:
        """MERGE-upsert a SBAR row. Returns True if this was a new row."""
        self._ensure_table("study_sbar_import")
        sql = f"""
        MERGE {self._fqn("study_sbar_import")} T
        USING (SELECT @sbar_id AS sbar_id) S
        ON T.sbar_id = S.sbar_id
        WHEN NOT MATCHED THEN INSERT (
          sbar_id, CPMRN, encounter, hospital_name, unit_name, urgency, issues, module,
          create_date_time, is_reviewed, reviewer_name, action, window_expires_at,
          match_status, matched_alert_id, synced_at
        ) VALUES (
          @sbar_id, @CPMRN, @encounter, @hospital_name, @unit_name, @urgency, @issues, @module,
          @create_date_time, @is_reviewed, @reviewer_name, @action, @window_expires_at,
          @match_status, NULL, @synced_at
        )
        """
        n = self._execute(sql, [
            bigquery.ScalarQueryParameter("sbar_id",          "STRING",    doc["sbar_id"]),
            bigquery.ScalarQueryParameter("CPMRN",            "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",        "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("hospital_name",    "STRING",    doc.get("hospital_name", "")),
            bigquery.ScalarQueryParameter("unit_name",        "STRING",    doc.get("unit_name", "")),
            bigquery.ScalarQueryParameter("urgency",          "STRING",    doc.get("urgency", "")),
            bigquery.ScalarQueryParameter("issues",           "STRING",    doc.get("issues", "")),
            bigquery.ScalarQueryParameter("module",           "STRING",    doc.get("module", "")),
            bigquery.ScalarQueryParameter("create_date_time", "TIMESTAMP", _dt_to_iso(doc.get("create_date_time"))),
            bigquery.ScalarQueryParameter("is_reviewed",      "BOOL",      bool(doc.get("is_reviewed", False))),
            bigquery.ScalarQueryParameter("reviewer_name",    "STRING",    doc.get("reviewer_name", "")),
            bigquery.ScalarQueryParameter("action",           "STRING",    doc.get("action", "")),
            bigquery.ScalarQueryParameter("window_expires_at","TIMESTAMP", _dt_to_iso(doc.get("window_expires_at"))),
            bigquery.ScalarQueryParameter("match_status",     "STRING",    doc.get("match_status", "pending")),
            bigquery.ScalarQueryParameter("synced_at",        "TIMESTAMP", _now_iso()),
        ])
        return n > 0

    def update_sbar(self, sbar_id: str, fields: dict):
        """Update fields on a SBAR row by sbar_id."""
        self._ensure_table("study_sbar_import")
        _update_by_id(self, "study_sbar_import", "sbar_id", sbar_id, fields)

    def find_sbars(self, filter: dict) -> list[dict]:
        self._ensure_table("study_sbar_import")
        clauses, params = _build_where("study_sbar_import", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('study_sbar_import')} WHERE {where}"
        return self._query(sql, params)

    def count_sbars(self, filter: dict) -> int:
        self._ensure_table("study_sbar_import")
        clauses, params = _build_where("study_sbar_import", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT COUNT(*) AS n FROM {self._fqn('study_sbar_import')} WHERE {where}"
        rows = self._query(sql, params)
        return rows[0]["n"] if rows else 0

    def batch_upsert_sbars(self, docs: list[dict]) -> tuple[int, int]:
        """Insert new SBAR rows in a single round-trip. Returns (inserted, skipped)."""
        if not docs:
            return 0, 0
        self._ensure_table("study_sbar_import")
        sbar_ids = [d["sbar_id"] for d in docs if d.get("sbar_id")]
        if not sbar_ids:
            return 0, len(docs)

        # One SELECT to find which ids already exist
        id_params = [bigquery.ScalarQueryParameter(f"eid_{i}", "STRING", sid)
                     for i, sid in enumerate(sbar_ids)]
        id_placeholders = ", ".join(f"@eid_{i}" for i in range(len(sbar_ids)))
        existing = {
            r["sbar_id"]
            for r in self._query(
                f"SELECT sbar_id FROM {self._fqn('study_sbar_import')} WHERE sbar_id IN ({id_placeholders})",
                id_params,
            )
        }

        new_docs = [d for d in docs if d.get("sbar_id") and d["sbar_id"] not in existing]
        skipped = len(docs) - len(new_docs)
        if not new_docs:
            return 0, skipped

        # Chunk into batches of 200 rows to stay within BQ parameter limits
        inserted = 0
        synced_at = _now_iso()
        for chunk_start in range(0, len(new_docs), 200):
            chunk = new_docs[chunk_start: chunk_start + 200]
            params: list = []
            selects: list[str] = []
            for i, doc in enumerate(chunk):
                p = f"s{chunk_start}_{i}_"
                params += [
                    bigquery.ScalarQueryParameter(f"{p}sbar_id",          "STRING",    doc["sbar_id"]),
                    bigquery.ScalarQueryParameter(f"{p}CPMRN",            "STRING",    doc.get("CPMRN", "")),
                    bigquery.ScalarQueryParameter(f"{p}encounter",        "INT64",     doc.get("encounter", 1)),
                    bigquery.ScalarQueryParameter(f"{p}hospital_name",    "STRING",    doc.get("hospital_name", "")),
                    bigquery.ScalarQueryParameter(f"{p}unit_name",        "STRING",    doc.get("unit_name", "")),
                    bigquery.ScalarQueryParameter(f"{p}urgency",          "STRING",    doc.get("urgency", "")),
                    bigquery.ScalarQueryParameter(f"{p}issues",           "STRING",    doc.get("issues", "")),
                    bigquery.ScalarQueryParameter(f"{p}module",           "STRING",    doc.get("module", "")),
                    bigquery.ScalarQueryParameter(f"{p}create_dt",        "TIMESTAMP", _dt_to_iso(doc.get("create_date_time"))),
                    bigquery.ScalarQueryParameter(f"{p}is_reviewed",      "BOOL",      bool(doc.get("is_reviewed", False))),
                    bigquery.ScalarQueryParameter(f"{p}reviewer_name",    "STRING",    doc.get("reviewer_name", "")),
                    bigquery.ScalarQueryParameter(f"{p}action",           "STRING",    doc.get("action", "")),
                    bigquery.ScalarQueryParameter(f"{p}window_expires_at","TIMESTAMP", _dt_to_iso(doc.get("window_expires_at"))),
                    bigquery.ScalarQueryParameter(f"{p}match_status",     "STRING",    doc.get("match_status", "pending")),
                ]
                selects.append(
                    f"SELECT @{p}sbar_id AS sbar_id, @{p}CPMRN AS CPMRN,"
                    f" @{p}encounter AS encounter, @{p}hospital_name AS hospital_name,"
                    f" @{p}unit_name AS unit_name, @{p}urgency AS urgency,"
                    f" @{p}issues AS issues, @{p}module AS module,"
                    f" @{p}create_dt AS create_date_time, @{p}is_reviewed AS is_reviewed,"
                    f" @{p}reviewer_name AS reviewer_name, @{p}action AS action,"
                    f" @{p}window_expires_at AS window_expires_at,"
                    f" @{p}match_status AS match_status, NULL AS matched_alert_id,"
                    f" TIMESTAMP('{synced_at}') AS synced_at"
                )
            sql = (
                f"INSERT INTO {self._fqn('study_sbar_import')}"
                f" (sbar_id, CPMRN, encounter, hospital_name, unit_name, urgency, issues, module,"
                f" create_date_time, is_reviewed, reviewer_name, action, window_expires_at,"
                f" match_status, matched_alert_id, synced_at)"
                f" {' UNION ALL '.join(selects)}"
            )
            inserted += self._execute(sql, params)

        return inserted, skipped

    def find_alerts_for_sbars(
        self,
        sbars: list[dict],
        window_before_h: int = 6,
        window_after_h: int = 2,
    ) -> dict[str, list[dict]]:
        """
        One BQ round-trip: return {sbar_id: [matching alert rows]} for all pending SBARs.
        Only returns SBARs that have at least one candidate alert.
        """
        if not sbars:
            return {}
        self._ensure_table("study_alerts")

        # Build inline SBAR window table as UNION ALL SELECTs
        params: list = []
        window_rows: list[str] = []
        for i, sbar in enumerate(sbars):
            create_dt = sbar.get("create_date_time")
            if isinstance(create_dt, datetime):
                pass
            else:
                continue
            if create_dt.tzinfo is None:
                create_dt = create_dt.replace(tzinfo=timezone.utc)
            ws = (create_dt - timedelta(hours=window_before_h)).isoformat()
            we = (create_dt + timedelta(hours=window_after_h)).isoformat()
            params += [
                bigquery.ScalarQueryParameter(f"sw_sid_{i}",  "STRING",    sbar["sbar_id"]),
                bigquery.ScalarQueryParameter(f"sw_cpm_{i}",  "STRING",    sbar.get("CPMRN", "")),
                bigquery.ScalarQueryParameter(f"sw_enc_{i}",  "INT64",     sbar.get("encounter", 1)),
                bigquery.ScalarQueryParameter(f"sw_ws_{i}",   "TIMESTAMP", ws),
                bigquery.ScalarQueryParameter(f"sw_we_{i}",   "TIMESTAMP", we),
            ]
            window_rows.append(
                f"SELECT @sw_sid_{i} AS sbar_id, @sw_cpm_{i} AS CPMRN,"
                f" @sw_enc_{i} AS encounter, @sw_ws_{i} AS window_start, @sw_we_{i} AS window_end"
            )

        if not window_rows:
            return {}

        sql = f"""
        WITH sbar_windows AS ({" UNION ALL ".join(window_rows)})
        SELECT a.*, sw.sbar_id AS _sbar_id
        FROM {self._fqn("study_alerts")} a
        JOIN sbar_windows sw
          ON  a.CPMRN     = sw.CPMRN
          AND a.encounter = sw.encounter
          AND a.alerted_at >= sw.window_start
          AND a.alerted_at <= sw.window_end
          AND a.match_status IN ('pending', 'fp_candidate')
        """
        rows = self._query(sql, params)

        result: dict[str, list[dict]] = {}
        for row in rows:
            sbar_id = row.pop("_sbar_id", None)
            if sbar_id:
                result.setdefault(sbar_id, []).append(row)
        return result

    # ── study_task_import ─────────────────────────────────────────────────────

    def upsert_task(self, doc: dict) -> bool:
        self._ensure_table("study_task_import")
        sql = f"""
        MERGE {self._fqn("study_task_import")} T
        USING (SELECT @task_id AS task_id) S
        ON T.task_id = S.task_id
        WHEN NOT MATCHED THEN INSERT (
          task_id, CPMRN, encounter, hospital_name, unit_name, title, issues,
          priority, status, task_visible_at, window_expires_at, match_status,
          matched_alert_id, synced_at
        ) VALUES (
          @task_id, @CPMRN, @encounter, @hospital_name, @unit_name, @title, @issues,
          @priority, @status, @task_visible_at, @window_expires_at, @match_status,
          NULL, @synced_at
        )
        """
        n = self._execute(sql, [
            bigquery.ScalarQueryParameter("task_id",          "STRING",    doc["task_id"]),
            bigquery.ScalarQueryParameter("CPMRN",            "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",        "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("hospital_name",    "STRING",    doc.get("hospital_name", "")),
            bigquery.ScalarQueryParameter("unit_name",        "STRING",    doc.get("unit_name", "")),
            bigquery.ScalarQueryParameter("title",            "STRING",    doc.get("title", "")),
            bigquery.ScalarQueryParameter("issues",           "STRING",    doc.get("issues", "")),
            bigquery.ScalarQueryParameter("priority",         "STRING",    doc.get("priority", "")),
            bigquery.ScalarQueryParameter("status",           "STRING",    doc.get("status", "")),
            bigquery.ScalarQueryParameter("task_visible_at",  "TIMESTAMP", _dt_to_iso(doc.get("task_visible_at"))),
            bigquery.ScalarQueryParameter("window_expires_at","TIMESTAMP", _dt_to_iso(doc.get("window_expires_at"))),
            bigquery.ScalarQueryParameter("match_status",     "STRING",    doc.get("match_status", "pending")),
            bigquery.ScalarQueryParameter("synced_at",        "TIMESTAMP", _now_iso()),
        ])
        return n > 0

    def update_task(self, task_id: str, fields: dict):
        self._ensure_table("study_task_import")
        _update_by_id(self, "study_task_import", "task_id", task_id, fields)

    def find_tasks(self, filter: dict) -> list[dict]:
        self._ensure_table("study_task_import")
        clauses, params = _build_where("study_task_import", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('study_task_import')} WHERE {where}"
        return self._query(sql, params)

    def count_tasks(self, filter: dict) -> int:
        self._ensure_table("study_task_import")
        clauses, params = _build_where("study_task_import", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT COUNT(*) AS n FROM {self._fqn('study_task_import')} WHERE {where}"
        rows = self._query(sql, params)
        return rows[0]["n"] if rows else 0

    # ── append-only tables ────────────────────────────────────────────────────

    def insert_suppressed_event(self, doc: dict):
        self._ensure_table("study_suppressed_events")
        sql = f"""
        INSERT INTO {self._fqn("study_suppressed_events")}
          (CPMRN, encounter, problem_name, suppressed_at, suppression_reason, created_at)
        VALUES
          (@CPMRN, @encounter, @problem_name, @suppressed_at, @suppression_reason, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("CPMRN",              "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",          "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("problem_name",       "STRING",    doc.get("problem_name", "")),
            bigquery.ScalarQueryParameter("suppressed_at",      "TIMESTAMP", _dt_to_iso(doc.get("suppressed_at"))),
            bigquery.ScalarQueryParameter("suppression_reason", "STRING",    doc.get("suppression_reason", "")),
            bigquery.ScalarQueryParameter("created_at",         "TIMESTAMP", _now_iso()),
        ])

    def count_suppressed(self, filter: dict) -> int:
        self._ensure_table("study_suppressed_events")
        clauses, params = _build_where("study_suppressed_events", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT COUNT(*) AS n FROM {self._fqn('study_suppressed_events')} WHERE {where}"
        rows = self._query(sql, params)
        return rows[0]["n"] if rows else 0

    def find_suppressed(self, filter: dict) -> list[dict]:
        self._ensure_table("study_suppressed_events")
        clauses, params = _build_where("study_suppressed_events", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('study_suppressed_events')} WHERE {where}"
        return self._query(sql, params)

    def insert_metrics_snapshot(self, doc: dict):
        self._ensure_table("study_metrics_snapshots")
        # Build columns dynamically — only include fields that have DDL columns
        _METRIC_COLS = {
            "computed_at", "tp", "fp", "fn", "fn_sbar", "fn_task", "tn",
            "total_patient_hours", "sensitivity", "specificity", "ppv", "npv", "f1",
            "sensitivity_ci", "specificity_ci", "ppv_ci",
            "lead_time_median_minutes", "lead_time_iqr_minutes", "lead_time_n",
            "suppressed_total", "suppressed_with_sbar", "pending_adjudication",
            "adj_total", "adj_appropriate", "adj_inappropriate",
            "expl_clear", "expl_partial", "expl_unclear",
            "alerts_total", "sbars_total", "tasks_total",
            "excluded_downtime", "task_excluded_downtime", "task_deduplicated",
        }
        row = {k: v for k, v in doc.items() if k in _METRIC_COLS}
        # CI fields: stored as JSON string
        for ci_col in ("sensitivity_ci", "specificity_ci", "ppv_ci"):
            if ci_col in row and not isinstance(row[ci_col], str):
                row[ci_col] = json.dumps(row[ci_col])
        # computed_at
        if "computed_at" in row:
            row["computed_at"] = _dt_to_iso(row["computed_at"])

        cols = list(row.keys())
        vals = [f"@{c}" for c in cols]
        params = []
        for c in cols:
            v = row[c]
            if c == "computed_at" or c.endswith("_at"):
                params.append(bigquery.ScalarQueryParameter(c, "TIMESTAMP", v))
            elif c in ("sensitivity_ci", "specificity_ci", "ppv_ci"):
                params.append(bigquery.ScalarQueryParameter(c, "STRING", v))
            elif isinstance(v, float) or c in ("sensitivity", "specificity", "ppv", "npv", "f1",
                                                "lead_time_median_minutes", "lead_time_iqr_minutes"):
                params.append(bigquery.ScalarQueryParameter(c, "FLOAT64", float(v) if v is not None else None))
            elif isinstance(v, int) or isinstance(v, type(None)):
                params.append(bigquery.ScalarQueryParameter(c, "INT64", int(v) if v is not None else None))
            else:
                params.append(bigquery.ScalarQueryParameter(c, "STRING", str(v)))

        sql = f"""
        INSERT INTO {self._fqn("study_metrics_snapshots")} ({', '.join(cols)})
        VALUES ({', '.join(vals)})
        """
        self._execute(sql, params)

    def get_latest_metrics_snapshot(self) -> dict | None:
        self._ensure_table("study_metrics_snapshots")
        sql = f"""
        SELECT * FROM {self._fqn("study_metrics_snapshots")}
        ORDER BY computed_at DESC
        LIMIT 1
        """
        rows = self._query(sql)
        return rows[0] if rows else None

    def mark_fp_candidates(self, cutoff_dt: datetime, now: datetime | None = None) -> int:
        """Bulk-mark pending alerts older than cutoff_dt as fp_candidate. Returns row count."""
        self._ensure_table("study_alerts")
        now_iso = _dt_to_iso(now) if now else _now_iso()
        sql = f"""
        UPDATE {self._fqn("study_alerts")}
        SET match_status = 'fp_candidate', fp_candidate_at = @fp_now
        WHERE match_status = 'pending' AND alerted_at < @fp_cutoff
        """
        return self._execute(sql, [
            bigquery.ScalarQueryParameter("fp_cutoff", "TIMESTAMP", _dt_to_iso(cutoff_dt)),
            bigquery.ScalarQueryParameter("fp_now",    "TIMESTAMP", now_iso),
        ])

    def find_matched_alerts_with_sbar(self) -> list[dict]:
        """Return matched/tp_confirmed alerts that have a non-null matched_sbar_id."""
        self._ensure_table("study_alerts")
        sql = f"""
        SELECT alert_id, matched_sbar_id, alerted_at
        FROM {self._fqn("study_alerts")}
        WHERE match_status = 'tp_confirmed'
          AND matched_sbar_id IS NOT NULL
        """
        return self._query(sql)

    def find_adjudications(self) -> list[dict]:
        """Return all adjudication rows."""
        self._ensure_table("study_adjudications")
        sql = f"SELECT * FROM {self._fqn('study_adjudications')}"
        return self._query(sql)

    # ── med_recon ───────────────────────────────────────────────────────────────

    def insert_med_recon(self, doc: dict) -> str:
        """Insert one med-reconciliation audit row. Returns the recon_id."""
        self._ensure_table("med_recon_audit")
        recon_id = doc.get("recon_id") or _new_id()
        sql = f"""
        INSERT INTO {self._fqn("med_recon_audit")}
          (recon_id, CPMRN, encounter, snapshot_at, recon_at, document_file_keys,
           document_reported_ats, document_categories, summary_narrative, raw_transcription,
           extracted_meds, active_orders_snapshot, recon_items, proposed_actions, action_set_id,
           image_urls, model, discrepancy_count, card_sent, recipients, dedup_of, step_costs, created_at)
        VALUES
          (@recon_id, @CPMRN, @encounter, @snapshot_at, @recon_at, @document_file_keys,
           @document_reported_ats, @document_categories, @summary_narrative, @raw_transcription,
           @extracted_meds, @active_orders_snapshot, @recon_items, @proposed_actions, @action_set_id,
           @image_urls, @model, @discrepancy_count, @card_sent, @recipients, @dedup_of, @step_costs, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("recon_id",              "STRING",    recon_id),
            bigquery.ScalarQueryParameter("CPMRN",                 "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",             "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("snapshot_at",           "TIMESTAMP", _dt_to_iso(doc.get("snapshot_at"))),
            bigquery.ScalarQueryParameter("recon_at",              "TIMESTAMP", _dt_to_iso(doc.get("recon_at"))),
            bigquery.ScalarQueryParameter("document_file_keys",    "STRING",    _to_json_col(doc.get("document_file_keys"))),
            bigquery.ScalarQueryParameter("document_reported_ats", "STRING",    _to_json_col(doc.get("document_reported_ats"))),
            bigquery.ScalarQueryParameter("document_categories",   "STRING",    _to_json_col(doc.get("document_categories"))),
            bigquery.ScalarQueryParameter("summary_narrative",     "STRING",    doc.get("summary_narrative")),
            bigquery.ScalarQueryParameter("raw_transcription",     "STRING",    _to_json_col(doc.get("raw_transcription"))),
            bigquery.ScalarQueryParameter("extracted_meds",        "STRING",    _to_json_col(doc.get("extracted_meds"))),
            bigquery.ScalarQueryParameter("active_orders_snapshot","STRING",    _to_json_col(doc.get("active_orders_snapshot"))),
            bigquery.ScalarQueryParameter("recon_items",           "STRING",    _to_json_col(doc.get("recon_items"))),
            bigquery.ScalarQueryParameter("proposed_actions",      "STRING",    _to_json_col(doc.get("proposed_actions"))),
            bigquery.ScalarQueryParameter("action_set_id",         "STRING",    doc.get("action_set_id")),
            bigquery.ScalarQueryParameter("image_urls",            "STRING",    _to_json_col(doc.get("image_urls"))),
            bigquery.ScalarQueryParameter("model",                 "STRING",    doc.get("model", "")),
            bigquery.ScalarQueryParameter("discrepancy_count",     "INT64",     doc.get("discrepancy_count", 0)),
            bigquery.ScalarQueryParameter("card_sent",             "BOOL",      bool(doc.get("card_sent", False))),
            bigquery.ScalarQueryParameter("recipients",            "STRING",    _to_json_col(doc.get("recipients"))),
            bigquery.ScalarQueryParameter("dedup_of",              "STRING",    doc.get("dedup_of")),
            bigquery.ScalarQueryParameter("step_costs",            "STRING",    _to_json_col(doc.get("step_costs"))),
            bigquery.ScalarQueryParameter("created_at",            "TIMESTAMP", _now_iso()),
        ])
        return recon_id

    def insert_med_recon_action(self, doc: dict):
        """Insert one applied-action row (one per action on Submit)."""
        self._ensure_table("med_recon_actions")
        sql = f"""
        INSERT INTO {self._fqn("med_recon_actions")}
          (recon_id, action_set_id, CPMRN, encounter, selected_keys, action_kind, action_key,
           order_label, result_ok, result_status, result_error, applied_by_email,
           applied_by_display, space_name, message_name, applied_at)
        VALUES
          (@recon_id, @action_set_id, @CPMRN, @encounter, @selected_keys, @action_kind, @action_key,
           @order_label, @result_ok, @result_status, @result_error, @applied_by_email,
           @applied_by_display, @space_name, @message_name, @applied_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("recon_id",           "STRING",    doc.get("recon_id", "")),
            bigquery.ScalarQueryParameter("action_set_id",      "STRING",    doc.get("action_set_id", "")),
            bigquery.ScalarQueryParameter("CPMRN",              "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",          "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("selected_keys",      "STRING",    _to_json_col(doc.get("selected_keys"))),
            bigquery.ScalarQueryParameter("action_kind",        "STRING",    doc.get("action_kind", "")),
            bigquery.ScalarQueryParameter("action_key",         "STRING",    doc.get("action_key", "")),
            bigquery.ScalarQueryParameter("order_label",        "STRING",    doc.get("order_label", "")),
            bigquery.ScalarQueryParameter("result_ok",          "BOOL",      bool(doc.get("result_ok", False))),
            bigquery.ScalarQueryParameter("result_status",      "INT64",     doc.get("result_status")),
            bigquery.ScalarQueryParameter("result_error",       "STRING",    doc.get("result_error")),
            bigquery.ScalarQueryParameter("applied_by_email",   "STRING",    doc.get("applied_by_email", "")),
            bigquery.ScalarQueryParameter("applied_by_display", "STRING",    doc.get("applied_by_display", "")),
            bigquery.ScalarQueryParameter("space_name",         "STRING",    doc.get("space_name", "")),
            bigquery.ScalarQueryParameter("message_name",       "STRING",    doc.get("message_name", "")),
            bigquery.ScalarQueryParameter("applied_at",         "TIMESTAMP", _now_iso()),
        ])

    def find_med_recon(self, filter: dict) -> list[dict]:
        """SELECT med_recon_audit rows matching filter (recon_id, CPMRN, encounter, ...)."""
        self._ensure_table("med_recon_audit")
        clauses, params = _build_where("med_recon_audit", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('med_recon_audit')} WHERE {where}"
        return self._query(sql, params)

    def insert_run_cost(self, doc: dict):
        self._ensure_table("pipeline_run_costs")
        sql = f"""
        INSERT INTO {self._fqn("pipeline_run_costs")}
          (run_started_at, computed_at, patient_count, trace_count, model,
           totals, by_step, pricing, patient_tiers)
        VALUES
          (@run_started_at, @computed_at, @patient_count, @trace_count, @model,
           @totals, @by_step, @pricing, @patient_tiers)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("run_started_at",  "TIMESTAMP", _dt_to_iso(doc.get("run_started_at"))),
            bigquery.ScalarQueryParameter("computed_at",     "TIMESTAMP", _dt_to_iso(doc.get("computed_at"))),
            bigquery.ScalarQueryParameter("patient_count",   "INT64",     doc.get("patient_count", 0)),
            bigquery.ScalarQueryParameter("trace_count",     "INT64",     doc.get("trace_count", 0)),
            bigquery.ScalarQueryParameter("model",           "STRING",    doc.get("model", "")),
            bigquery.ScalarQueryParameter("totals",          "STRING",    _to_json_col(doc.get("totals"))),
            bigquery.ScalarQueryParameter("by_step",         "STRING",    _to_json_col(doc.get("by_step"))),
            bigquery.ScalarQueryParameter("pricing",         "STRING",    _to_json_col(doc.get("pricing"))),
            bigquery.ScalarQueryParameter("patient_tiers",   "STRING",    _to_json_col(doc.get("patient_tiers"))),
        ])

    # ── report_interpret ──────────────────────────────────────────────────────

    def insert_patient_run(self, doc: dict):
        """Insert one per-patient-per-run audit row."""
        self._ensure_table("pipeline_patient_runs")
        sql = f"""
        INSERT INTO {self._fqn("pipeline_patient_runs")}
          (run_started_at, CPMRN, encounter, pipeline_outcome, pass1_tag,
           pass1_needs_full, pass2_outcome, problem_details,
           delta_vitals, delta_labs, delta_notes, delta_reports,
           trigger_reason, problems_scoped, gate_trace, created_at)
        VALUES
          (@run_started_at, @CPMRN, @encounter, @pipeline_outcome, @pass1_tag,
           @pass1_needs_full, @pass2_outcome, @problem_details,
           @delta_vitals, @delta_labs, @delta_notes, @delta_reports,
           @trigger_reason, @problems_scoped, @gate_trace, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("run_started_at",   "TIMESTAMP", _dt_to_iso(doc.get("run_started_at"))),
            bigquery.ScalarQueryParameter("CPMRN",            "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",        "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("pipeline_outcome", "STRING",    doc.get("pipeline_outcome", "")),
            bigquery.ScalarQueryParameter("pass1_tag",        "STRING",    doc.get("pass1_tag", "")),
            bigquery.ScalarQueryParameter("pass1_needs_full", "BOOL",      bool(doc.get("pass1_needs_full", False))),
            bigquery.ScalarQueryParameter("pass2_outcome",    "STRING",    doc.get("pass2_outcome", "")),
            bigquery.ScalarQueryParameter("problem_details",  "STRING",    _to_json_col(doc.get("problem_details"))),
            bigquery.ScalarQueryParameter("delta_vitals",     "INT64",     int(doc.get("delta_vitals") or 0)),
            bigquery.ScalarQueryParameter("delta_labs",       "INT64",     int(doc.get("delta_labs") or 0)),
            bigquery.ScalarQueryParameter("delta_notes",      "INT64",     int(doc.get("delta_notes") or 0)),
            bigquery.ScalarQueryParameter("delta_reports",    "INT64",     int(doc.get("delta_reports") or 0)),
            bigquery.ScalarQueryParameter("trigger_reason",   "STRING",    doc.get("trigger_reason", "")),
            bigquery.ScalarQueryParameter("problems_scoped",  "STRING",    _to_json_col(doc.get("problems_scoped"))),
            bigquery.ScalarQueryParameter("gate_trace",       "STRING",    _to_json_col(doc.get("gate_trace"))),
            bigquery.ScalarQueryParameter("created_at",       "TIMESTAMP", _now_iso()),
        ])

    def insert_next_check_events(self, rows: list[dict]) -> int:
        """
        Write one row per problem per run to patient_next_checks.

        Each row captures the next_check state after this run. Pass due_after=None
        to emit a clearing row (problem resolved, or patient discharged).
        Returns count of rows successfully inserted.
        """
        if not rows:
            return 0
        self._ensure_table("patient_next_checks")
        sql = f"""
        INSERT INTO {self._fqn("patient_next_checks")}
          (run_started_at, CPMRN, encounter, problem_name,
           nc_type, nc_key, nc_label, due_after, clinical_status, tracker_reasoning, protocol_ids, created_at)
        VALUES
          (@run_started_at, @CPMRN, @encounter, @problem_name,
           @nc_type, @nc_key, @nc_label, @due_after, @clinical_status, @tracker_reasoning, @protocol_ids, @created_at)
        """
        written = 0
        for row in rows:
            try:
                self._execute(sql, [
                    bigquery.ScalarQueryParameter("run_started_at",    "TIMESTAMP", _dt_to_iso(row.get("run_started_at"))),
                    bigquery.ScalarQueryParameter("CPMRN",             "STRING",    row.get("CPMRN", "")),
                    bigquery.ScalarQueryParameter("encounter",         "INT64",     row.get("encounter", 1)),
                    bigquery.ScalarQueryParameter("problem_name",      "STRING",    row.get("problem_name", "")),
                    bigquery.ScalarQueryParameter("nc_type",           "STRING",    row.get("nc_type") or ""),
                    bigquery.ScalarQueryParameter("nc_key",            "STRING",    row.get("nc_key") or ""),
                    bigquery.ScalarQueryParameter("nc_label",          "STRING",    row.get("nc_label") or ""),
                    bigquery.ScalarQueryParameter("due_after",         "TIMESTAMP", _dt_to_iso(row.get("due_after"))),
                    bigquery.ScalarQueryParameter("clinical_status",   "STRING",    row.get("clinical_status") or ""),
                    bigquery.ScalarQueryParameter("tracker_reasoning", "STRING",    row.get("tracker_reasoning") or ""),
                    bigquery.ScalarQueryParameter("protocol_ids",      "STRING",    row.get("protocol_ids") or ""),
                    bigquery.ScalarQueryParameter("created_at",        "TIMESTAMP", _now_iso()),
                ])
                written += 1
            except Exception:
                log.exception(
                    "bq_store: insert_next_check_events row failed for %s enc=%d problem=%s",
                    row.get("CPMRN"), row.get("encounter"), row.get("problem_name"),
                )
        return written

    def insert_report_interpret(self, doc: dict) -> str:
        """Insert one report-interpretation audit row. Returns the report_id."""
        self._ensure_table("report_interpret_audit")
        report_id = doc.get("report_id") or _new_id()
        sql = f"""
        INSERT INTO {self._fqn("report_interpret_audit")}
          (report_id, batch_id, CPMRN, encounter, snapshot_at, interpreted_at,
           document_file_key, document_category, document_name, reported_at, selection_reason,
           download_ok, interpret_ok, summary_narrative, raw_description, interpretation,
           report_type, confidence, findings, image_urls, model, card_sent, recipients,
           step_costs, created_at)
        VALUES
          (@report_id, @batch_id, @CPMRN, @encounter, @snapshot_at, @interpreted_at,
           @document_file_key, @document_category, @document_name, @reported_at, @selection_reason,
           @download_ok, @interpret_ok, @summary_narrative, @raw_description, @interpretation,
           @report_type, @confidence, @findings, @image_urls, @model, @card_sent, @recipients,
           @step_costs, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("report_id",         "STRING",    report_id),
            bigquery.ScalarQueryParameter("batch_id",          "STRING",    doc.get("batch_id", "")),
            bigquery.ScalarQueryParameter("CPMRN",             "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",         "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("snapshot_at",       "TIMESTAMP", _dt_to_iso(doc.get("snapshot_at"))),
            bigquery.ScalarQueryParameter("interpreted_at",    "TIMESTAMP", _dt_to_iso(doc.get("interpreted_at"))),
            bigquery.ScalarQueryParameter("document_file_key", "STRING",    doc.get("document_file_key", "")),
            bigquery.ScalarQueryParameter("document_category", "STRING",    doc.get("document_category", "")),
            bigquery.ScalarQueryParameter("document_name",     "STRING",    doc.get("document_name", "")),
            bigquery.ScalarQueryParameter("reported_at",       "STRING",    _to_json_col(doc.get("reported_at")) if not isinstance(doc.get("reported_at"), str) else doc.get("reported_at")),
            bigquery.ScalarQueryParameter("selection_reason",  "STRING",    doc.get("selection_reason", "")),
            bigquery.ScalarQueryParameter("download_ok",       "BOOL",      bool(doc.get("download_ok", False))),
            bigquery.ScalarQueryParameter("interpret_ok",      "BOOL",      bool(doc.get("interpret_ok", False))),
            bigquery.ScalarQueryParameter("summary_narrative", "STRING",    doc.get("summary_narrative")),
            bigquery.ScalarQueryParameter("raw_description",   "STRING",    doc.get("raw_description")),
            bigquery.ScalarQueryParameter("interpretation",    "STRING",    doc.get("interpretation")),
            bigquery.ScalarQueryParameter("report_type",       "STRING",    doc.get("report_type", "")),
            bigquery.ScalarQueryParameter("confidence",        "STRING",    doc.get("confidence", "")),
            bigquery.ScalarQueryParameter("findings",          "STRING",    _to_json_col(doc.get("findings"))),
            bigquery.ScalarQueryParameter("image_urls",        "STRING",    _to_json_col(doc.get("image_urls"))),
            bigquery.ScalarQueryParameter("model",             "STRING",    doc.get("model", "")),
            bigquery.ScalarQueryParameter("card_sent",         "BOOL",      bool(doc.get("card_sent", False))),
            bigquery.ScalarQueryParameter("recipients",        "STRING",    _to_json_col(doc.get("recipients"))),
            bigquery.ScalarQueryParameter("step_costs",        "STRING",    _to_json_col(doc.get("step_costs"))),
            bigquery.ScalarQueryParameter("created_at",        "TIMESTAMP", _now_iso()),
        ])
        return report_id

    def insert_report_run(self, doc: dict):
        """Insert one report-interpret run-outcome row (one per patient per cycle)."""
        self._ensure_table("report_interpret_runs")
        sql = f"""
        INSERT INTO {self._fqn("report_interpret_runs")}
          (run_id, batch_id, CPMRN, encounter, cycle_started_at, evaluated_at, outcome,
           n_reports_found, n_reports_interpreted, findings_injected, problems_touched,
           card_sent, recipients, watermark_before, watermark_after, cost_usd, error_detail,
           created_at)
        VALUES
          (@run_id, @batch_id, @CPMRN, @encounter, @cycle_started_at, @evaluated_at, @outcome,
           @n_reports_found, @n_reports_interpreted, @findings_injected, @problems_touched,
           @card_sent, @recipients, @watermark_before, @watermark_after, @cost_usd, @error_detail,
           @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("run_id",                "STRING",    doc.get("run_id") or _new_id()),
            bigquery.ScalarQueryParameter("batch_id",              "STRING",    doc.get("batch_id", "")),
            bigquery.ScalarQueryParameter("CPMRN",                 "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",             "INT64",     doc.get("encounter", 1)),
            bigquery.ScalarQueryParameter("cycle_started_at",      "TIMESTAMP", _dt_to_iso(doc.get("cycle_started_at"))),
            bigquery.ScalarQueryParameter("evaluated_at",          "TIMESTAMP", _dt_to_iso(doc.get("evaluated_at"))),
            bigquery.ScalarQueryParameter("outcome",               "STRING",    doc.get("outcome", "")),
            bigquery.ScalarQueryParameter("n_reports_found",       "INT64",     doc.get("n_reports_found", 0)),
            bigquery.ScalarQueryParameter("n_reports_interpreted", "INT64",     doc.get("n_reports_interpreted", 0)),
            bigquery.ScalarQueryParameter("findings_injected",     "INT64",     doc.get("findings_injected", 0)),
            bigquery.ScalarQueryParameter("problems_touched",      "INT64",     doc.get("problems_touched", 0)),
            bigquery.ScalarQueryParameter("card_sent",             "BOOL",      bool(doc.get("card_sent", False))),
            bigquery.ScalarQueryParameter("recipients",            "STRING",    _to_json_col(doc.get("recipients"))),
            bigquery.ScalarQueryParameter("watermark_before",      "TIMESTAMP", _dt_to_iso(doc.get("watermark_before"))),
            bigquery.ScalarQueryParameter("watermark_after",       "TIMESTAMP", _dt_to_iso(doc.get("watermark_after"))),
            bigquery.ScalarQueryParameter("cost_usd",              "FLOAT64",   float(doc.get("cost_usd", 0.0) or 0.0)),
            bigquery.ScalarQueryParameter("error_detail",          "STRING",    doc.get("error_detail", "")),
            bigquery.ScalarQueryParameter("created_at",            "TIMESTAMP", _now_iso()),
        ])

    def insert_report_feedback(self, doc: dict):
        """Insert a clinician rating row from a report-interpret chat card."""
        self._ensure_table("report_interpret_feedback")
        sql = f"""
        INSERT INTO {self._fqn("report_interpret_feedback")}
          (report_id, batch_id, CPMRN, encounter, report_type, rating, feedback_text,
           user_email, user_display, space_name, message_name, created_at)
        VALUES
          (@report_id, @batch_id, @CPMRN, @encounter, @report_type, @rating, @feedback_text,
           @user_email, @user_display, @space_name, @message_name, @created_at)
        """
        self._execute(sql, [
            bigquery.ScalarQueryParameter("report_id",     "STRING",    doc.get("report_id", "")),
            bigquery.ScalarQueryParameter("batch_id",      "STRING",    doc.get("batch_id", "")),
            bigquery.ScalarQueryParameter("CPMRN",         "STRING",    doc.get("CPMRN", "")),
            bigquery.ScalarQueryParameter("encounter",     "INT64",     doc.get("encounter")),
            bigquery.ScalarQueryParameter("report_type",   "STRING",    doc.get("report_type", "")),
            bigquery.ScalarQueryParameter("rating",        "INT64",     doc.get("rating")),
            bigquery.ScalarQueryParameter("feedback_text", "STRING",    doc.get("feedback_text")),
            bigquery.ScalarQueryParameter("user_email",    "STRING",    doc.get("user_email", "")),
            bigquery.ScalarQueryParameter("user_display",  "STRING",    doc.get("user_display", "")),
            bigquery.ScalarQueryParameter("space_name",    "STRING",    doc.get("space_name", "")),
            bigquery.ScalarQueryParameter("message_name",  "STRING",    doc.get("message_name", "")),
            bigquery.ScalarQueryParameter("created_at",    "TIMESTAMP", _now_iso()),
        ])

    def find_report_interpret(self, filter: dict) -> list[dict]:
        """SELECT report_interpret_audit rows matching filter."""
        self._ensure_table("report_interpret_audit")
        clauses, params = _build_where("report_interpret_audit", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('report_interpret_audit')} WHERE {where}"
        return self._query(sql, params)

    def find_report_runs(self, filter: dict) -> list[dict]:
        """SELECT report_interpret_runs rows matching filter."""
        self._ensure_table("report_interpret_runs")
        clauses, params = _build_where("report_interpret_runs", filter)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = f"SELECT * FROM {self._fqn('report_interpret_runs')} WHERE {where}"
        return self._query(sql, params)

    def insert_documentation_audit(self, doc: dict) -> str:
        """Insert one documentation audit row. Returns the audit_id."""
        self._ensure_table("documentation_audit")
        audit_id = doc.get("audit_id") or _new_id()
        row = {
            "audit_id":         audit_id,
            "CPMRN":            doc.get("CPMRN"),
            "encounter":        int(doc.get("encounter") or 0),
            "problem_name":     doc.get("problem_name"),
            "protocol_id":      doc.get("protocol_id"),
            "detected_at":      _dt_to_iso(doc.get("detected_at")),
            "audited_at":       _dt_to_iso(doc.get("audited_at")),
            "window_hours":     int(doc.get("window_hours") or 0),
            "verdict":          doc.get("verdict"),
            "note_count":       int(doc.get("note_count") or 0),
            "required_items":   _to_json_col(doc.get("required_items")),
            "documented_items": _to_json_col(doc.get("documented_items")),
            "missing_items":    _to_json_col(doc.get("missing_items")),
            "created_at":       _now_iso(),
        }
        errors = self._client.insert_rows_json(
            f"{self._project}.{self._dataset}.documentation_audit", [row]
        )
        if errors:
            log.error("bq_store: insert_documentation_audit errors: %s", errors)
        return audit_id

    def enqueue_documentation_audit(self, doc: dict) -> None:
        """
        Insert one row into documentation_audit_queue.
        queue_id = CPMRN_encounter_problem_name — stable natural key used by the
        sweep to deduplicate multiple inserts for the same problem.
        """
        self._ensure_table("documentation_audit_queue")
        cpmrn        = doc.get("CPMRN") or ""
        encounter    = int(doc.get("encounter") or 0)
        problem_name = doc.get("problem_name") or ""
        queue_id     = f"{cpmrn}_{encounter}_{problem_name}"
        row = {
            "queue_id":     queue_id,
            "CPMRN":        cpmrn,
            "encounter":    encounter,
            "problem_name": problem_name,
            "protocol_id":  doc.get("protocol_id"),
            "detected_at":  _dt_to_iso(doc.get("detected_at")),
            "audit_due_at": _dt_to_iso(doc.get("audit_due_at")),
            "enqueued_at":  _now_iso(),
        }
        errors = self._client.insert_rows_json(
            f"{self._project}.{self._dataset}.documentation_audit_queue", [row]
        )
        if errors:
            log.error("bq_store: enqueue_documentation_audit errors: %s", errors)

    def insert_resolution_event(self, doc: dict) -> str:
        """
        Insert one row into problem_resolution_events, fired when a worsening/critical
        problem's next_check clears without ever being deleted from tracking — i.e. it
        resolved (self-resolved or after being alerted). event_id is timestamped so a
        problem that later re-worsens and re-resolves gets a fresh row, not a dedupe.
        """
        self._ensure_table("problem_resolution_events")
        cpmrn        = doc.get("CPMRN") or ""
        encounter    = int(doc.get("encounter") or 0)
        problem_name = doc.get("problem_name") or ""
        resolved_at  = doc.get("resolved_at")
        first_detected_at = doc.get("first_detected_at")
        duration_hours: float | None = None
        if isinstance(resolved_at, datetime) and isinstance(first_detected_at, datetime):
            _ra = resolved_at if resolved_at.tzinfo else resolved_at.replace(tzinfo=timezone.utc)
            _fd = first_detected_at if first_detected_at.tzinfo else first_detected_at.replace(tzinfo=timezone.utc)
            duration_hours = round((_ra - _fd).total_seconds() / 3600, 2)
        event_id = f"{cpmrn}_{encounter}_{problem_name}_{_dt_to_iso(resolved_at)}"
        row = {
            "event_id":                      event_id,
            "CPMRN":                         cpmrn,
            "encounter":                     encounter,
            "problem_name":                  problem_name,
            "protocol_ids":                  ",".join(doc.get("protocol_ids") or []),
            "first_detected_at":             _dt_to_iso(first_detected_at),
            "resolved_at":                   _dt_to_iso(resolved_at),
            "duration_monitored_hours":      duration_hours,
            "resolution_status":             doc.get("resolution_status") or "",
            "resolution_reasoning":          doc.get("resolution_reasoning") or "",
            "last_status_before_resolution": doc.get("last_status_before_resolution") or "",
            "was_ever_alerted":              bool(doc.get("was_ever_alerted")),
            "alert_attempts_total":          int(doc.get("alert_attempts_total") or 0),
            "created_at":                    _now_iso(),
        }
        errors = self._client.insert_rows_json(
            f"{self._project}.{self._dataset}.problem_resolution_events", [row]
        )
        if errors:
            log.error("bq_store: insert_resolution_event errors: %s", errors)
        return event_id

    def find_pending_audit_queue(self) -> list[dict]:
        """
        Return queue rows whose audit window has elapsed and that have not yet
        been audited. Deduplicates by queue_id (takes the earliest detected_at).
        Joins against documentation_audit to exclude already-completed audits.
        """
        self._ensure_table("documentation_audit_queue")
        self._ensure_table("documentation_audit")
        sql = f"""
        SELECT
          q.queue_id,
          q.CPMRN,
          q.encounter,
          q.problem_name,
          q.protocol_id,
          MIN(q.detected_at) AS detected_at,
          MIN(q.audit_due_at) AS audit_due_at
        FROM {self._fqn('documentation_audit_queue')} q
        LEFT JOIN (
          SELECT CPMRN, encounter, problem_name
          FROM {self._fqn('documentation_audit')}
        ) a ON a.CPMRN = q.CPMRN
           AND a.encounter = q.encounter
           AND a.problem_name = q.problem_name
        WHERE a.CPMRN IS NULL
          AND q.audit_due_at <= CURRENT_TIMESTAMP()
        GROUP BY q.queue_id, q.CPMRN, q.encounter, q.problem_name, q.protocol_id
        ORDER BY MIN(q.audit_due_at)
        LIMIT 500
        """
        return self._query(sql)

    def find_documentation_audits(self, hours_back: int = 72) -> list[dict]:
        """Return recent documentation audit rows ordered by audited_at desc."""
        self._ensure_table("documentation_audit")
        sql = f"""
        SELECT *
        FROM {self._fqn('documentation_audit')}
        WHERE audited_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {int(hours_back)} HOUR)
        ORDER BY audited_at DESC
        LIMIT 500
        """
        return self._query(sql)


# ── where-clause builder ──────────────────────────────────────────────────────

# Allowed filterable columns per table (prevent SQL injection via column names)
_FILTERABLE: dict[str, set[str]] = {
    "study_alerts":     {"alert_id", "CPMRN", "encounter", "match_status", "alerted_at", "problem_name"},
    "study_sbar_import":{"CPMRN", "encounter", "match_status", "create_date_time", "window_expires_at", "sbar_id"},
    "study_task_import":{"CPMRN", "encounter", "match_status", "task_visible_at", "window_expires_at", "task_id"},
    "study_suppressed_events": {"CPMRN", "encounter", "suppressed_at"},
    "med_recon_audit":  {"recon_id", "CPMRN", "encounter", "recon_at", "action_set_id"},
    "med_recon_actions":{"recon_id", "CPMRN", "encounter", "action_set_id"},
    "report_interpret_audit": {"report_id", "batch_id", "CPMRN", "encounter", "report_type"},
    "report_interpret_runs":  {"run_id", "batch_id", "CPMRN", "encounter", "outcome"},
}


def _build_where(table: str, filter: dict) -> tuple[list[str], list]:
    """Translate a MongoDB-style filter dict to BQ WHERE clauses + parameters."""
    allowed = _FILTERABLE.get(table, set())
    clauses = []
    params  = []
    idx     = 0

    for k, v in filter.items():
        if k not in allowed:
            continue
        pname = f"w_{idx}_{k}"
        idx  += 1

        if isinstance(v, dict):
            for op, operand in v.items():
                op_pname = f"{pname}_{op.lstrip('$')}"
                p_type, py_val = _infer_type(operand)
                if op == "$gte":
                    clauses.append(f"{k} >= @{op_pname}")
                elif op == "$lte":
                    clauses.append(f"{k} <= @{op_pname}")
                elif op == "$lt":
                    clauses.append(f"{k} < @{op_pname}")
                elif op == "$gt":
                    clauses.append(f"{k} > @{op_pname}")
                elif op == "$in":
                    # Use UNNEST for $in
                    arr_pname = f"{op_pname}_arr"
                    clauses.append(f"{k} IN UNNEST(@{arr_pname})")
                    params.append(bigquery.ArrayQueryParameter(arr_pname, "STRING", [str(x) for x in operand]))
                    continue
                elif op == "$ne":
                    clauses.append(f"({k} IS NULL OR {k} != @{op_pname})")
                params.append(bigquery.ScalarQueryParameter(op_pname, p_type, py_val))
        elif isinstance(v, list):
            clauses.append(f"{k} IN UNNEST(@{pname})")
            params.append(bigquery.ArrayQueryParameter(pname, "STRING", [str(x) for x in v]))
        else:
            p_type, py_val = _infer_type(v)
            clauses.append(f"{k} = @{pname}")
            params.append(bigquery.ScalarQueryParameter(pname, p_type, py_val))

    return clauses, params


def _infer_type(v) -> tuple[str, Any]:
    if isinstance(v, bool):
        return "BOOL", v
    if isinstance(v, int):
        return "INT64", v
    if isinstance(v, float):
        return "FLOAT64", v
    if isinstance(v, datetime):
        return "TIMESTAMP", _dt_to_iso(v)
    return "STRING", str(v) if v is not None else None


def _update_by_id(store: BQStudyStore, table: str, id_col: str, id_val: str, fields: dict):
    """Generic UPDATE by primary key."""
    if not fields:
        return
    set_parts = []
    params    = [bigquery.ScalarQueryParameter("_pk", "STRING", id_val)]
    for k, v in fields.items():
        pname = f"u_{k}"
        p_type, py_val = _infer_type(v)
        set_parts.append(f"{k} = @{pname}")
        params.append(bigquery.ScalarQueryParameter(pname, p_type, py_val))
    sql = f"""
    UPDATE {store._fqn(table)}
    SET {', '.join(set_parts)}
    WHERE {id_col} = @_pk
    """
    store._execute(sql, params)


# ── Factory ───────────────────────────────────────────────────────────────────

_store_instance: BQStudyStore | None = None


def get_bq_store() -> BQStudyStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = BQStudyStore(
            project=os.environ.get("BQ_PROJECT", _PROJECT),
            dataset=os.environ.get("BQ_DATASET", _DATASET),
        )
    return _store_instance
