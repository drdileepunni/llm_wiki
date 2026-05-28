"""
Direct BigQuery client for prod-tech-project1-bv479-zo027 (READ-ONLY).

Authenticates using a service account key stored in the PRODTECH_BQ_SA_KEY
environment variable (JSON string, loaded from Secret Manager on Cloud Run).

This client is exclusively for reading from prod-tech BigQuery tables
(latest_sbar_fact, latest_task_fact, etc.).  It has no relation to the
patientview-9uxml BQ writes in bq_store.py, which use Workload Identity.

Usage
-----
    from backend.services.bq_client import get_bq_client

    rows = get_bq_client().execute_select(
        "SELECT * FROM `prod-tech-project1-bv479-zo027.patient.latest_sbar_fact` LIMIT 10"
    )
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from google.cloud import bigquery
from google.oauth2 import service_account

log = logging.getLogger(__name__)

_BQ_PROJECT = "prod-tech-project1-bv479-zo027"


def _parse_dt(value: Any) -> datetime | None:
    """Parse a value that may be a datetime, date, or ISO string. Returns UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class ProdTechBigQueryClient:
    """Read-only BigQuery client for prod-tech-project1-bv479-zo027."""

    def __init__(self, sa_key_json: str, project_id: str = _BQ_PROJECT):
        self.project_id = project_id
        key_info = json.loads(sa_key_json)
        creds = service_account.Credentials.from_service_account_info(
            key_info,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        self._client = bigquery.Client(project=project_id, credentials=creds)

    def execute_select(self, query: str, project: str | None = None) -> list[dict[str, Any]]:
        """Run a SELECT query and return a list of row dicts."""
        job = self._client.query(query)
        rows = list(job.result())
        return [dict(row) for row in rows]


_client_cache: ProdTechBigQueryClient | None = None


def get_bq_client() -> ProdTechBigQueryClient:
    global _client_cache
    if _client_cache is None:
        sa_key_json = os.environ.get("PRODTECH_BQ_SA_KEY")
        if not sa_key_json:
            raise RuntimeError(
                "PRODTECH_BQ_SA_KEY env var not set. "
                "On Cloud Run it is mounted from Secret Manager. "
                "For local dev, set it to the JSON contents of the prod-tech SA key file."
            )
        _client_cache = ProdTechBigQueryClient(sa_key_json)
        log.info("ProdTechBigQueryClient initialised (direct BQ, no proxy)")
    return _client_cache


# Re-export the datetime helper so syncers can use it without an extra import
parse_bq_dt = _parse_dt
