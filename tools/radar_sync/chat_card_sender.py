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

from tools.radar_sync.alert_cards import build_alert_card, build_batched_alert_card

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


def _build_cards_v2(
    cpmrn: str,
    encounter: int,
    alerts: list[tuple[dict, str]],
    structured_summary: dict,
    service_url: str,
    cds_url: str,
    cb_token: str,
) -> list:
    """
    Build cardsV2 payload.
    Multiple alerts → one combined card (shared header + per-problem sections).
    Single alert   → one standard per-problem card.
    """
    gchat_webhook_url = f"{service_url}/webhook"
    callback_url      = f"{cds_url}/alert-feedback"
    order_callback_url = f"{cds_url}/order-action"

    if len(alerts) > 1:
        return build_batched_alert_card(
            cpmrn=cpmrn,
            encounter=encounter,
            alerts=alerts,
            structured_summary=structured_summary,
            gchat_webhook_url=gchat_webhook_url,
            callback_url=callback_url,
            cb_token=cb_token,
            order_callback_url=order_callback_url,
        )

    assessment, alert_id = alerts[0]
    return build_alert_card(
        cpmrn=cpmrn,
        encounter=encounter,
        assessment=assessment,
        structured_summary=structured_summary,
        alert_id=alert_id,
        gchat_webhook_url=gchat_webhook_url,
        callback_url=callback_url,
        cb_token=cb_token,
        order_callback_url=order_callback_url,
    )


def send_cards_to_recipients(cards_v2: list, recipients: list[str]) -> bool:
    """
    POST a pre-built cardsV2 payload to each recipient via the chat-microservice.
    Generic helper (reused by med-recon). Returns True if at least one send succeeded.
    """
    service_url = (os.getenv("GCHAT_SERVICE_URL") or _DEFAULT_GCHAT_SERVICE_URL).rstrip("/")
    api_key = os.getenv("GCHAT_API_KEY", "")
    if not api_key:
        logger.error("chat_card_sender: GCHAT_API_KEY not set — cannot send cards")
        return False

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
                logger.info("chat_card_sender: card sent to %s", email)
            else:
                logger.error("chat_card_sender: send to %s failed (HTTP %d): %s",
                             email, resp.status_code, resp.text[:300])
        except Exception:
            logger.exception("chat_card_sender: send to %s failed", email)
    return sent_any


def send_batch_alert_cards(
    cpmrn: str,
    encounter: int,
    alerts: list[tuple[dict, str]],
    structured_summary: dict,
    recipients: list[str],
) -> bool:
    """
    Send all alerting assessments for one patient as a single batched message.
    Each problem gets its own card (with its own rating form) but they are
    delivered in one message, eliminating per-problem notification noise.
    Returns True if at least one recipient received it.
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

    cards_v2 = _build_cards_v2(cpmrn, encounter, alerts, structured_summary,
                                service_url, cds_url, cb_token)

    alert_ids = [aid for _, aid in alerts]
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
                    "chat_card_sender: batch alert (%d problems) sent to %s for %s enc=%d (alert_ids=%s)",
                    len(alerts), email, cpmrn, encounter, alert_ids,
                )
            else:
                logger.error(
                    "chat_card_sender: batch send to %s failed (HTTP %d): %s",
                    email, resp.status_code, resp.text[:300],
                )
        except Exception:
            logger.exception("chat_card_sender: batch send to %s failed", email)

    return sent_any


def send_alert_cards(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    structured_summary: dict,
    alert_id: str,
    recipients: list[str],
) -> bool:
    """Single-problem convenience wrapper around send_batch_alert_cards."""
    return send_batch_alert_cards(
        cpmrn, encounter, [(assessment, alert_id)], structured_summary, recipients,
    )
