"""
Download document media (treatment-chart images) from Radar.

Flow (proven via the download_radar_media skill):
  POST {RADAR_POST_URL}/api/patients/{CPMRN}/download/  {"key": file_key}
    → JSON {"data": signed_url}  → GET signed_url → bytes
  (some endpoints return the bytes directly; both paths handled.)

Bearer auth reuses order_actions._bearer (refresh-token → bearer via token_service).
"""
from __future__ import annotations

import logging
import mimetypes

import requests

from tools.radar_sync.order_actions import _bearer, _post_url

logger = logging.getLogger(__name__)

_TIMEOUT = 30
_EXT_MIME = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}


def _download_headers() -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {_bearer()}",
        "content-type": "application/json",
        "origin": "https://cloudphysicianworld.com",
        "referer": "https://cloudphysicianworld.com/",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
    }


def _guess_mime(file_key: str, header_ct: str | None) -> str:
    if header_ct and "/" in header_ct and "json" not in header_ct:
        return header_ct.split(";")[0].strip()
    guess, _ = mimetypes.guess_type(file_key)
    if guess:
        return guess
    ext = file_key.rsplit(".", 1)[-1].lower() if "." in file_key else ""
    return _EXT_MIME.get(ext, "image/jpeg")


def download_document_image(cpmrn: str, file_key: str) -> tuple[bytes, str]:
    """Return (image_bytes, mime_type) for a document file key. Raises on failure."""
    url = f"{_post_url()}/api/patients/{cpmrn}/download/"
    resp = requests.post(url, headers=_download_headers(), json={"key": file_key}, timeout=_TIMEOUT)
    resp.raise_for_status()

    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        data = resp.json()
        signed = data.get("data") or data.get("url")
        if not signed:
            raise RuntimeError(f"download: no signed URL in response for {file_key!r}")
        file_resp = requests.get(signed, timeout=_TIMEOUT)
        file_resp.raise_for_status()
        return file_resp.content, _guess_mime(file_key, file_resp.headers.get("Content-Type"))
    return resp.content, _guess_mime(file_key, ct)


def download_all(cpmrn: str, file_keys: list[str]) -> list[dict]:
    """[{file_key, bytes, mime_type, error}] — never raises per item."""
    out: list[dict] = []
    for k in file_keys:
        try:
            content, mime = download_document_image(cpmrn, k)
            out.append({"file_key": k, "bytes": content, "mime_type": mime, "error": None})
        except Exception as exc:  # noqa: BLE001 — one bad download must not abort the rest
            logger.exception("download_all: failed for %s", k)
            out.append({"file_key": k, "bytes": None, "mime_type": None, "error": str(exc)})
    return out
