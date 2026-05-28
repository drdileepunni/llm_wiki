"""
BigQuery client via Cloud Run proxy.

Instead of calling BigQuery directly (which requires google-cloud-bigquery and
explicit ADC / BQ IAM roles), this routes all queries through the shared Cloud
Run BigQuery service using an identity token obtained from ADC (Workload Identity
in Cloud Run/GCE, or `gcloud auth application-default login` locally).

The calling service only needs roles/run.invoker on the proxy service; BigQuery
IAM is managed centrally on the proxy's service account.

Usage
-----
    from backend.services.bq_client import get_bq_client

    rows = get_bq_client().execute_select("SELECT ... FROM `project.dataset.table`")
    # rows is a list[dict] — field access is identical to google.cloud.bigquery Row objects
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests
from google.auth import default
from google.auth.transport.requests import Request
from google.oauth2 import id_token

log = logging.getLogger(__name__)

_BQ_PROJECT = "prod-tech-project1-bv479-zo027"


def _parse_dt(value: Any) -> datetime | None:
    """
    Parse a value that may be a datetime (from direct BQ client) or a string
    (from the Cloud Run JSON proxy). Always returns a UTC-aware datetime or None.
    """
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


class ProdBigQueryClient:
    def __init__(self, service_url: str, project_id: str = _BQ_PROJECT):
        self.service_url = service_url.rstrip("/")
        self.project_id  = project_id

    def _get_identity_token(self) -> str:
        # Prefer an explicit SA key file (local dev with invoker key)
        sa_path = os.environ.get("BQ_INVOKER_SA_KEY")
        if sa_path:
            from google.oauth2 import service_account as _sa
            creds = _sa.IDTokenCredentials.from_service_account_file(
                sa_path, target_audience=self.service_url
            )
            creds.refresh(Request())
            return creds.token
        # Cloud Run / GCE: Workload Identity via ADC
        credentials, _ = default()
        return id_token.fetch_id_token(Request(), self.service_url)

    def _make_request(self, method: str, endpoint: str, **kwargs) -> dict:
        token   = self._get_identity_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url     = f"{self.service_url}{endpoint}"

        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, params=kwargs.get("params"), timeout=30)
        elif method.upper() == "POST":
            resp = requests.post(url, headers=headers, json=kwargs.get("json"), timeout=120)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"BQ proxy error: {data['error']} — {data.get('details', '')}")
        return data

    def execute_select(self, query: str, project: str | None = None) -> list[dict[str, Any]]:
        """Run a SELECT query and return a list of dicts."""
        data = self._make_request("POST", "/query", json={
            "query":   query,
            "project": project or self.project_id,
        })
        return data.get("data", [])


def get_bq_client() -> ProdBigQueryClient:
    from backend.config import BQ_SERVICE_URL
    return ProdBigQueryClient(service_url=BQ_SERVICE_URL)


# Re-export the datetime helper so syncers can use it without an extra import
parse_bq_dt = _parse_dt
