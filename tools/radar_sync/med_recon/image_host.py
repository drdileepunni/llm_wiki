"""
Host treatment-chart images for the Google Chat card.

Radar signed URLs are short-lived, but Google fetches a card's image lazily when the
clinician expands the accordion (possibly hours later). So we re-upload the bytes to the
ops GCS bucket and mint a long-lived (7-day) v4 signed URL.

Signing needs a private key. Cloud Run's runner SA and local ADC can't sign blobs without
IAM Token Creator, so we sign with the service-account key already available as
RADAR_READ_SERVICE_ACCOUNT (same key the read path uses) — independent of the storage
client used for the upload. If signing isn't possible, we return [] (card simply omits
the image section) rather than failing the reconciliation.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

logger = logging.getLogger(__name__)

_EXT = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
_SIGN_EXPIRY = timedelta(days=7)


def _sa_key_credentials():
    """Build service_account.Credentials from RADAR_READ_SERVICE_ACCOUNT (fallback signer)."""
    raw = os.getenv("RADAR_READ_SERVICE_ACCOUNT")
    if not raw:
        return None
    try:
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_info(json.loads(raw))
    except Exception:
        logger.exception("image_host: could not build SA-key credentials")
        return None


def _sign(blob) -> str:
    """
    Mint a v4 signed GET URL.

    Preferred: IAM SignBlob using the ambient credentials (the Cloud Run runner SA,
    which already has bucket access). Requires roles/iam.serviceAccountTokenCreator on
    the runner SA. Fallback: an explicit SA key (RADAR_READ_SERVICE_ACCOUNT) if that SA
    has object read on the bucket.
    """
    from google import auth
    from google.auth.transport import requests as grequests

    try:
        creds, _ = auth.default()
        creds.refresh(grequests.Request())
        email = getattr(creds, "service_account_email", None)
        token = getattr(creds, "token", None)
        if email and email != "default" and token:
            return blob.generate_signed_url(
                version="v4", expiration=_SIGN_EXPIRY, method="GET",
                service_account_email=email, access_token=token,
            )
    except Exception:
        logger.warning("image_host: IAM signing unavailable, trying SA key", exc_info=True)

    key_creds = _sa_key_credentials()
    if key_creds is not None:
        return blob.generate_signed_url(
            version="v4", expiration=_SIGN_EXPIRY, method="GET", credentials=key_creds,
        )
    raise RuntimeError("no signing method available")


def host_chart_images(cpmrn: str, encounter: int, recon_id: str, images: list[dict],
                      prefix: str = "med_recon_images") -> list[str]:
    """
    Upload valid images to gs://{ops}/{prefix}/{cpmrn}/{enc}/{recon_id}/{i}.{ext}
    and return signed URLs. Returns [] on any failure (card omits images, recon proceeds).

    `prefix` selects the GCS folder — defaults to med_recon_images; the report-interpret
    module passes "report_images" so the two features don't share a path.
    """
    valid = [im for im in images if im.get("bytes")]
    if not valid:
        return []

    try:
        from app.backend.services.gcs_store import get_bucket
        bucket = get_bucket()
    except Exception:
        logger.exception("image_host: could not get bucket")
        return []

    urls: list[str] = []
    for i, im in enumerate(valid):
        mime = im.get("mime_type") or "image/jpeg"
        ext = _EXT.get(mime, "jpg")
        blob_path = f"{prefix}/{cpmrn}/{encounter}/{recon_id}/{i}.{ext}"
        try:
            blob = bucket.blob(blob_path)
            blob.upload_from_string(im["bytes"], content_type=mime)
            urls.append(_sign(blob))
        except Exception:
            logger.exception("image_host: failed to host image %d (%s)", i, blob_path)
    return urls
