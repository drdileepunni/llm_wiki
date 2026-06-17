"""
Execute medication order changes in Radar EMR — edit, discontinue, create.

Two auth surfaces are involved:

  * **Reads** (fetch current orders, needed to build edit/discontinue payloads)
    use the service-account ID-token flow against the Radar read API
    (`radar_auth.get_id_token` + `RADAR_READ_URL`), same as the rest of the pipeline.

  * **Writes** (the actual order PATCHes) use the refresh-token → bearer flow
    against `cloudphysicianworld.com` (`token_service.get_bearer_token`,
    env `REFRESH_TOKEN` + `RADAR_POST_URL`). Orders are attributed to the
    refresh-token's account.

Write endpoints (direct Radar API, as used by the web client):
  * edit / discontinue:  PATCH {RADAR_POST_URL}/api/patients/{CPMRN}/{enc}/orders/edit
  * create:              PATCH {RADAR_POST_URL}/api/patients/{CPMRN}/{enc}/orders/new

For edit/discontinue we start from the *existing* order object (so the EMR keeps
its `_id`, `orderNo`, schedule, etc.), mutate the few fields that change, and
PATCH it back. `presetName`/`preset` are not required and are omitted.

`apply_order_actions()` is the single entry point the chat webhook calls on Submit.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30


# ── auth helpers ────────────────────────────────────────────────────────────
def _bearer() -> str:
    """Refresh-token → bearer for write calls. Reuses repo-root token_service."""
    # token_service.py lives at the repo root; ensure it's importable whether we
    # run from the repo root locally or from /app in the Cloud Run image.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from token_service import get_bearer_token

    token = get_bearer_token()
    if not token:
        raise RuntimeError(
            "Could not obtain bearer token. Check REFRESH_TOKEN and RADAR_POST_URL."
        )
    return token


def _post_url() -> str:
    url = os.getenv("RADAR_POST_URL") or os.getenv("RADAR_POST_URL_STAGING")
    if not url:
        raise RuntimeError("RADAR_POST_URL not set")
    return url.rstrip("/")


def _write_headers() -> dict:
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


# ── reading current orders ──────────────────────────────────────────────────
def _read_active_medications(cpmrn: str, encounter: int) -> list[dict]:
    """Return the patient's active medication orders via the read API."""
    from tools.radar_sync.radar_auth import get_id_token

    url = os.environ.get("RADAR_READ_URL", "")
    if not url:
        raise RuntimeError("RADAR_READ_URL not set")

    payload = {
        "function": "get_patient_json",
        "filter_using": {"CPMRN": cpmrn, "encounters": encounter},
        "return_fields": {"CPMRN": 1, "encounters": 1, "orders": 1},
    }
    resp = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {get_id_token(url)}", "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    patients = data if isinstance(data, list) else [data]
    if not patients:
        return []
    orders = (patients[0] or {}).get("orders", {}) or {}
    return (orders.get("active", {}) or {}).get("medications", []) or []


def _find_order(meds: list[dict], order_no: str) -> dict | None:
    """Locate an active order by its orderNo (preferred) or _id."""
    for m in meds:
        if m.get("orderNo") == order_no or m.get("_id") == order_no:
            return m
    return None


def _order_label(order: dict) -> str:
    """Human-readable name+dose for results, e.g. 'Pantoprazole 40 mg'."""
    name = order.get("name", "order")
    qty, unit = order.get("quantity"), order.get("unit")
    if qty is not None and unit:
        return f"{name} {qty} {unit}"
    return name


# ── write primitives ────────────────────────────────────────────────────────
def _patch(cpmrn: str, encounter: int, leaf: str, body: dict) -> dict:
    """PATCH an order body to /orders/{leaf} and return a normalized result."""
    url = f"{_post_url()}/api/patients/{cpmrn}/{encounter}/orders/{leaf}"
    resp = requests.patch(url, headers=_write_headers(), json=body, timeout=_TIMEOUT)
    ok = resp.status_code in (200, 201)
    logger.info("order PATCH %s → HTTP %s | body_keys=%s | response=%s",
                leaf, resp.status_code, list(body.keys()), resp.text[:400])
    if not ok:
        logger.error("order PATCH %s failed (HTTP %s): %s", leaf, resp.status_code, resp.text[:300])
    return {"ok": ok, "status": resp.status_code, "error": None if ok else resp.text[:300]}


def edit_order(cpmrn: str, encounter: int, order_no: str, changes: dict) -> dict:
    """
    Edit an existing active order. `changes` is a shallow dict of fields to
    override on the existing order object (e.g. {"quantity": 60} or
    {"frequency": {"fType": "every", "hours": 6, ...}}).
    """
    meds = _read_active_medications(cpmrn, encounter)
    order = _find_order(meds, order_no)
    if not order:
        return {"kind": "edit", "name": order_no, "ok": False, "status": None,
                "error": f"active order {order_no!r} not found"}

    body = dict(order)
    body.update(changes)
    body["bedsideOrder"] = True
    result = _patch(cpmrn, encounter, "edit", body)
    result.update({"kind": "edit", "name": _order_label(order)})
    return result


def discontinue_order(
    cpmrn: str, encounter: int, order_no: str,
    reasons: list | None = None, statement: str = "",
) -> dict:
    """Discontinue an active order: PATCH /orders/edit with a `discontinue` block."""
    meds = _read_active_medications(cpmrn, encounter)
    order = _find_order(meds, order_no)
    if not order:
        return {"kind": "discontinue", "name": order_no, "ok": False, "status": None,
                "error": f"active order {order_no!r} not found"}

    body = dict(order)
    body["discontinue"] = {"reasons": reasons or ["Other"], "statement": statement}
    body["toBeDiscarded"] = True
    body["bedsideOrder"] = True
    result = _patch(cpmrn, encounter, "edit", body)
    result.update({"kind": "discontinue", "name": _order_label(order)})
    return result


def create_order(cpmrn: str, encounter: int, order: dict) -> dict:
    """
    Create a new medication order: PATCH /orders/new.

    `order` should carry at least `name`; sensible defaults are filled for the
    fields the EMR expects. `_id`/`presetName`/`preset` are intentionally omitted.
    """
    body = {
        "name": order["name"],
        "brandName": order.get("brandName"),
        "quantity": order.get("quantity"),
        "unit": order.get("unit"),
        "route": order.get("route"),
        "form": order.get("form"),
        "frequency": order.get("frequency"),
        "scheduleSelector": order.get("scheduleSelector", ""),
        "skipSchedule": order.get("skipSchedule", []),
        "startNow": order.get("startNow", True),
        "instructions": order.get("instructions"),
        "additionalInformation": order.get("additionalInformation"),
        "combination": order.get("combination", []),
        "sos": order.get("sos"),
        "sosReason": order.get("sosReason"),
        "pta": order.get("pta", False),
        "urgency": order.get("urgency"),
        "type": "medications",
        "category": "active",
        "state": order.get("state", "red"),
        "createdBy": order.get("createdBy", "Aina bot"),
        "bedsideOrder": True,
    }
    # drop keys the caller didn't set and that the EMR treats as optional
    body = {k: v for k, v in body.items() if v is not None or k in ("instructions", "combination", "skipSchedule")}
    result = _patch(cpmrn, encounter, "new", body)
    result.update({"kind": "new", "name": _order_label(order)})
    return result


def apply_order_actions(cpmrn: str, encounter: int, actions: list[dict]) -> list[dict]:
    """
    Execute a list of order actions sequentially. Each action is a dict:

      {"kind": "edit",        "order_no": "...", "changes": {...}}
      {"kind": "discontinue", "order_no": "...", "reasons": [...], "statement": "..."}
      {"kind": "new",         "order": {...}}

    Returns a list of per-action result dicts: {kind, name, ok, status, error}.
    Never raises on a single action's failure — failures are captured per item.
    """
    results: list[dict] = []
    for action in actions:
        kind = action.get("kind")
        try:
            if kind == "edit":
                res = edit_order(cpmrn, encounter, action["order_no"], action.get("changes", {}))
            elif kind == "discontinue":
                res = discontinue_order(
                    cpmrn, encounter, action["order_no"],
                    action.get("reasons"), action.get("statement", ""),
                )
            elif kind == "new":
                res = create_order(cpmrn, encounter, action["order"])
            else:
                res = {"kind": kind, "name": "", "ok": False, "status": None,
                       "error": f"unknown action kind {kind!r}"}
        except Exception as exc:  # noqa: BLE001 — one bad action must not abort the rest
            logger.exception("apply_order_actions: %s action failed", kind)
            res = {"kind": kind, "name": action.get("order_no") or "", "ok": False,
                   "status": None, "error": str(exc)}
        results.append(res)
    return results
