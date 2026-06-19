"""
Reads pipeline configuration from GCS for the dashboard.

Collections:
  - app_settings/{_id}.json  — lab_alert_rules, alert_recipients, gchat_webhook,
                                monitored_workspaces, symptom_alert_rules (if seeded)
  - monitoring_protocols/*.json — permissive-hypertension etc.
                                   Falls back to seed-script constants if not in GCS.

All reads are read-only (no writes).
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)


def _get_db():
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[1]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("GCS_BUCKET", "patientview-cds-pipeline-ops")
    from backend.services.gcs_store import get_gcs_db
    return get_gcs_db()


# ── monitoring protocols ───────────────────────────────────────────────────────

def get_monitoring_protocols() -> list[dict]:
    """
    Return monitoring protocols from GCS monitoring_protocols/.
    If not yet seeded to GCS, falls back to the constants in the seed script.
    """
    try:
        db = _get_db()
        from google.cloud import storage
        bucket_name = os.environ.get("GCS_BUCKET", "patientview-cds-pipeline-ops")
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blobs  = list(bucket.list_blobs(prefix="monitoring_protocols/"))
        if blobs:
            return db["monitoring_protocols"].find({})
    except Exception:
        log.exception("config_reader: could not read monitoring_protocols from GCS")

    # Fallback: read from the seed script constants (not yet seeded to GCS)
    log.info("config_reader: monitoring_protocols not in GCS — loading from seed script")
    return _seed_protocols()


def _seed_protocols() -> list[dict]:
    """Load PROTOCOLS directly from the seed script without executing side effects."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[1]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from tools.radar_sync.seed_monitoring_protocols import PROTOCOLS
        return PROTOCOLS
    except Exception:
        log.exception("config_reader: could not import PROTOCOLS from seed script")
        return []


# ── lab alert rules ────────────────────────────────────────────────────────────

def get_lab_alert_rules() -> dict[str, Any]:
    try:
        db = _get_db()
        doc = db["app_settings"].find_one({"_id": "lab_alert_rules"})
        if doc:
            return doc
    except Exception:
        log.exception("config_reader: could not read lab_alert_rules")
    return {"_id": "lab_alert_rules", "enabled": False, "rules": []}


# ── symptom alert rules ───────────────────────────────────────────────────────

def get_symptom_alert_rules() -> dict[str, Any]:
    """
    Return symptom alert rules. Falls back to seed-script constants if not in GCS.
    """
    try:
        db = _get_db()
        doc = db["app_settings"].find_one({"_id": "symptom_alert_rules"})
        if doc:
            return doc
    except Exception:
        log.exception("config_reader: could not read symptom_alert_rules")

    log.info("config_reader: symptom_alert_rules not in GCS — loading from seed script")
    return _seed_symptom_rules()


def _seed_symptom_rules() -> dict[str, Any]:
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[1]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        from tools.radar_sync.seed_symptom_alert_rules import INITIAL_RULES
        return {"_id": "symptom_alert_rules", "enabled": True, "rules": INITIAL_RULES, "_source": "seed_script"}
    except Exception:
        log.exception("config_reader: could not import INITIAL_RULES from symptom seed script")
        return {"_id": "symptom_alert_rules", "enabled": False, "rules": []}


# ── lab staleness overrides ────────────────────────────────────────────────────

def get_lab_staleness_overrides() -> dict[str, Any]:
    """
    Return the lab staleness overrides doc. Falls back to seed-script defaults if not in GCS.
    """
    try:
        db = _get_db()
        doc = db["app_settings"].find_one({"_id": "lab_staleness_overrides"})
        if doc:
            return doc
    except Exception:
        log.exception("config_reader: could not read lab_staleness_overrides")

    log.info("config_reader: lab_staleness_overrides not in GCS — loading from seed script")
    try:
        import sys
        from pathlib import Path
        _root = Path(__file__).resolve().parents[1]
        for p in [str(_root / "app"), str(_root)]:
            if p not in sys.path:
                sys.path.insert(0, p)
        from tools.radar_sync.seed_lab_staleness_overrides import INITIAL_OVERRIDES
        return {"_id": "lab_staleness_overrides", "enabled": True, "overrides": INITIAL_OVERRIDES, "_source": "seed_script"}
    except Exception:
        log.exception("config_reader: could not import INITIAL_OVERRIDES from staleness seed script")
    return {"_id": "lab_staleness_overrides", "enabled": True, "overrides": {}}


# ── operational settings ───────────────────────────────────────────────────────

def get_operational_settings() -> dict[str, Any]:
    """Return alert_recipients, gchat_webhook, monitored_workspaces, study_pipeline_enabled,
    med_recon_enabled from GCS."""
    result: dict[str, Any] = {}
    setting_ids = ["alert_recipients", "gchat_webhook", "monitored_workspaces"]
    try:
        db = _get_db()
        for sid in setting_ids:
            doc = db["app_settings"].find_one({"_id": sid})
            if doc:
                result[sid] = _redact(sid, doc)
        # Study pipeline flag — stored separately, default False
        flag_doc = db["app_settings"].find_one({"_id": "study_pipeline_enabled"})
        result["study_pipeline_enabled"] = bool(flag_doc and flag_doc.get("enabled", False))
        # Med-recon flag — lives inside med_recon_config, default True (enabled)
        mr_doc = db["app_settings"].find_one({"_id": "med_recon_config"})
        result["med_recon_enabled"] = bool(mr_doc.get("enabled", True)) if mr_doc else True
    except Exception:
        log.exception("config_reader: could not read operational settings")
    return result


def _redact(setting_id: str, doc: dict) -> dict:
    """Redact sensitive values (webhook URL token)."""
    if setting_id == "gchat_webhook":
        url = doc.get("url", "")
        if url:
            # Keep only the space part, redact the key/token query params
            base = url.split("?")[0]
            doc = dict(doc)
            doc["url"] = base + "?[token redacted]"
    return doc


# ── all config (single call for the /api/config/* routes) ────────────────────

def get_all_config() -> dict[str, Any]:
    return {
        "monitoring_protocols": get_monitoring_protocols(),
        "lab_alert_rules":      get_lab_alert_rules(),
        "symptom_alert_rules":  get_symptom_alert_rules(),
        "operational":          get_operational_settings(),
    }
