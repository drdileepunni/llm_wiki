"""
Send CDS alert cards to clinicians through the generic chat-microservice.

Recipients come from the app_settings doc `alert_recipients`:
    {"_id": "alert_recipients", "enabled": true, "emails": ["a@x.com", ...]}

Env:
    GCHAT_SERVICE_URL    base URL of chat-microservice (no trailing slash)
    GCHAT_API_KEY        API key for its protected endpoints
    CDS_PUBLIC_URL       public base URL of this service (for click callbacks)
    ALERT_FEEDBACK_TOKEN shared secret echoed back on feedback callbacks
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

from tools.radar_sync.alert_cards import build_alert_card

logger = logging.getLogger(__name__)

_DEFAULT_GCHAT_SERVICE_URL = "https://gchat-microservice-971880579407.asia-south1.run.app"


def get_alert_recipients(db: Any) -> list[str]:
    """Return enabled recipient emails from app_settings, or [] if disabled/unset."""
    try:
        cfg = db["app_settings"].find_one({"_id": "alert_recipients"})
        if cfg and cfg.get("enabled") and cfg.get("emails"):
            return list(cfg["emails"])
    except Exception:
        logger.exception("chat_card_sender: failed to read alert_recipients config")
    return []


def send_alert_cards(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
    alert_id: str,
    recipients: list[str],
) -> bool:
    """
    Build the alert card once and send it to each recipient via the
    chat-microservice /send-cards endpoint. Returns True if at least one
    recipient received it.
    """
    service_url = (os.getenv("GCHAT_SERVICE_URL") or _DEFAULT_GCHAT_SERVICE_URL).rstrip("/")
    api_key = os.getenv("GCHAT_API_KEY", "")
    cds_url = (os.getenv("CDS_PUBLIC_URL") or "").rstrip("/")
    cb_token = os.getenv("ALERT_FEEDBACK_TOKEN", "")

    if not api_key:
        logger.error("chat_card_sender: GCHAT_API_KEY not set — cannot send alert cards")
        return False
    if not cds_url:
        logger.error("chat_card_sender: CDS_PUBLIC_URL not set — cannot send alert cards")
        return False

    cards_v2 = build_alert_card(
        cpmrn=cpmrn,
        encounter=encounter,
        assessment=assessment,
        structured_summary=structured_summary,
        alert_id=alert_id,
        gchat_webhook_url=f"{service_url}/webhook",
        callback_url=f"{cds_url}/alert-feedback",
        cb_token=cb_token,
    )

    sent_any = False
    for email in recipients:
        try:
            resp = requests.post(
                f"{service_url}/send-cards",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"user_email": email, "cardsV2": cards_v2},
                timeout=15,
            )
            if resp.status_code == 200:
                sent_any = True
                logger.info(
                    "chat_card_sender: alert card sent to %s for %s enc=%d (alert_id=%s)",
                    email, cpmrn, encounter, alert_id,
                )
            else:
                logger.error(
                    "chat_card_sender: send to %s failed (HTTP %d): %s",
                    email, resp.status_code, resp.text[:300],
                )
        except Exception:
            logger.exception("chat_card_sender: send to %s failed", email)

    return sent_any
