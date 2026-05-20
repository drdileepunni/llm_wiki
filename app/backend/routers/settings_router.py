"""
App-level settings endpoints.

GET  /api/settings/gchat-webhook          — fetch current webhook config
PUT  /api/settings/gchat-webhook          — save URL + enabled flag
POST /api/settings/gchat-webhook/test     — send a test ping
"""
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SETTING_ID = "gchat_webhook"


def _col():
    from backend.services.emr.db import get_db
    return get_db()["app_settings"]


# ── Models ─────────────────────────────────────────────────────────────────

class WebhookConfig(BaseModel):
    url: str
    enabled: bool = True


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/gchat-webhook")
def get_gchat_webhook():
    doc = _col().find_one({"_id": _SETTING_ID})
    if not doc:
        return {"url": "", "enabled": False}
    return {"url": doc.get("url", ""), "enabled": doc.get("enabled", False)}


@router.put("/gchat-webhook")
def put_gchat_webhook(cfg: WebhookConfig):
    _col().update_one(
        {"_id": _SETTING_ID},
        {"$set": {"url": cfg.url, "enabled": cfg.enabled}},
        upsert=True,
    )
    logger.info("settings: gchat webhook updated — enabled=%s", cfg.enabled)
    return {"ok": True}


@router.post("/gchat-webhook/test")
def test_gchat_webhook():
    doc = _col().find_one({"_id": _SETTING_ID})
    url = doc.get("url", "") if doc else ""
    if not url:
        raise HTTPException(status_code=400, detail="No webhook URL configured")

    from tools.radar_sync.gchat_notifier import send_test_message
    ok = send_test_message(url)
    if not ok:
        raise HTTPException(status_code=502, detail="Webhook delivery failed — check the URL")
    return {"ok": True}
