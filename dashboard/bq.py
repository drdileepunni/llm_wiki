"""
BigQuery helper for the local CDS pipeline dashboard.

Uses Application Default Credentials (gcloud ADC) — no SA key needed locally.
Dataset: patientview-9uxml.cds_study in asia-south1.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from google.cloud import bigquery

log = logging.getLogger(__name__)

_PROJECT  = "patientview-9uxml"
_DATASET  = "cds_study"
_LOCATION = "asia-south1"

_client: bigquery.Client | None = None


def _get_client() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=_PROJECT)
        log.info("dashboard.bq: client initialised (ADC, project=%s)", _PROJECT)
    return _client


def query(sql: str, params: list | None = None) -> list[dict[str, Any]]:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    try:
        rows = _get_client().query(sql, job_config=cfg, location=_LOCATION).result()
        return [dict(r) for r in rows]
    except Exception:
        log.exception("dashboard.bq: query failed\n%s", sql)
        return []


def fqn(table: str) -> str:
    return f"`{_PROJECT}.{_DATASET}.{table}`"


def parse_json_col(value: str | None) -> dict:
    """Safely parse a JSON-string BQ column into a dict."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}
