"""
Writes pipeline configuration back to GCS for the dashboard.
Uses storage.Client() directly for clean full-document replacement.
Auth: Application Default Credentials (same as reading).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_BUCKET_NAME = None


def _bucket():
    from google.cloud import storage
    global _BUCKET_NAME
    _BUCKET_NAME = os.environ.get("GCS_BUCKET", "patientview-cds-pipeline-ops")
    return storage.Client().bucket(_BUCKET_NAME)


def _write_blob(path: str, doc: Any):
    blob = _bucket().blob(path)
    blob.upload_from_string(
        json.dumps(doc, indent=2, ensure_ascii=False),
        content_type="application/json",
    )
    log.info("config_writer: wrote gs://%s/%s", _BUCKET_NAME, path)


def save_app_setting(setting_id: str, doc: dict) -> None:
    """Write app_settings/{setting_id}.json to GCS."""
    doc["_id"] = setting_id  # ensure _id is always present
    _write_blob(f"app_settings/{setting_id}.json", doc)


def save_monitoring_protocol(protocol: dict) -> None:
    """Write monitoring_protocols/{protocol_id}.json to GCS."""
    pid = protocol.get("protocol_id")
    if not pid:
        raise ValueError("protocol must have a protocol_id")
    _write_blob(f"monitoring_protocols/{pid}.json", protocol)


def delete_monitoring_protocol(protocol_id: str) -> None:
    bucket = _bucket()
    blob = bucket.blob(f"monitoring_protocols/{protocol_id}.json")
    if blob.exists():
        blob.delete()
        log.info("config_writer: deleted monitoring_protocols/%s.json", protocol_id)
