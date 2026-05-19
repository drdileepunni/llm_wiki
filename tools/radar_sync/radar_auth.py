"""Service account ID-token auth for Radar EMR API."""
from __future__ import annotations

import json
import os
from pathlib import Path


def get_id_token(target_audience: str) -> str:
    """Return a Google Cloud ID token for the given audience URL."""
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    sa_creds = _load_service_account()
    creds = service_account.IDTokenCredentials.from_service_account_info(
        sa_creds, target_audience=target_audience
    )
    creds.refresh(Request())
    return creds.token


def _load_service_account() -> dict:
    # 1. JSON string from env
    raw = os.environ.get("RADAR_READ_SERVICE_ACCOUNT") or os.environ.get("RADAR_SERVICE_ACCOUNT")
    if raw:
        try:
            sa = json.loads(raw)
            if isinstance(sa, dict) and sa.get("type") == "service_account":
                return sa
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. File path from env or default
    sa_path = Path(os.environ.get("RADAR_SERVICE_ACCOUNT_PATH") or "")
    if not sa_path or not sa_path.exists():
        # walk up to repo root and look for radar_service_account.json
        sa_path = Path(__file__).resolve().parents[2] / "radar_service_account.json"

    if not sa_path.exists():
        raise FileNotFoundError(
            "Radar service account not found. Set RADAR_READ_SERVICE_ACCOUNT (JSON string) "
            "or RADAR_SERVICE_ACCOUNT_PATH (file path), or place radar_service_account.json "
            "in the repo root."
        )
    with open(sa_path) as f:
        return json.load(f)
