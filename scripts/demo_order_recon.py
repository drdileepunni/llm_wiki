"""
Demo: medication-reconciliation order actions on the TEST patient INKLERN261338/1.

Two parts:

  python -m scripts.demo_order_recon execute
      Direct engine proof — applies one edit, one discontinue, one new order
      directly (no card), then re-reads to confirm.

  python -m scripts.demo_order_recon card
      Builds a reconciliation card from the patient's CURRENT orders, persists the
      proposed action set to GCS, and sends the card to the recipient so the UX is
      visible in Google Chat. (The Submit button only executes once /order-action
      is deployed — see plan.)

Required env (the run-demo wrapper sets these from Secret Manager):
    REFRESH_TOKEN, RADAR_POST_URL, RADAR_READ_URL, RADAR_READ_SERVICE_ACCOUNT,
    GCS_BUCKET, GCHAT_SERVICE_URL, GCHAT_API_KEY, CDS_PUBLIC_URL, ALERT_FEEDBACK_TOKEN
"""
from __future__ import annotations

import os
import sys
import uuid

import requests

CPMRN = "INKLERN261338"
ENCOUNTER = 1
RECIPIENT = "dileep.unni@cloudphysician.net"


def _by_name(meds: list[dict], name_fragment: str) -> dict | None:
    for m in meds:
        if name_fragment.lower() in (m.get("name", "")).lower():
            return m
    return None


def run_execute():
    from tools.radar_sync.order_actions import apply_order_actions, _read_active_medications

    print(f"Current active meds on {CPMRN}/{ENCOUNTER}:")
    meds = _read_active_medications(CPMRN, ENCOUNTER)
    for m in meds:
        print(f"  orderNo={m.get('orderNo')} | {m.get('name')} {m.get('quantity')}{m.get('unit')} "
              f"{m.get('route')} q{m.get('frequency', {}).get('hours')}h")

    metoprolol = _by_name(meds, "Metoprolol")
    aspirin = _by_name(meds, "Aspirin")
    actions = []
    if metoprolol:
        actions.append({"kind": "edit", "order_no": metoprolol["orderNo"],
                        "changes": {"frequency": {**metoprolol.get("frequency", {}), "hours": 24}}})
    if aspirin:
        actions.append({"kind": "discontinue", "order_no": aspirin["orderNo"], "statement": "recon demo"})
    actions.append({"kind": "new", "order": {
        "name": "Pantoprazole", "quantity": 40, "unit": "mg", "route": "IV", "form": "Injection",
        "frequency": {"fType": "every", "days": None, "hours": 24, "mins": None, "timeOfDay": None},
        "scheduleSelector": "06:00",
    }})

    print("\nApplying actions...")
    for r in apply_order_actions(CPMRN, ENCOUNTER, actions):
        print(f"  [{r['kind']}] {r['name']}: ok={r['ok']} status={r['status']} err={r['error']}")

    print("\nActive meds after:")
    for m in _read_active_medications(CPMRN, ENCOUNTER):
        print(f"  {m.get('name')} {m.get('quantity')}{m.get('unit')} {m.get('route')} "
              f"q{m.get('frequency', {}).get('hours')}h")


def run_card():
    from tools.radar_sync.order_actions import _read_active_medications
    from tools.radar_sync.order_action_store import save_action_set
    from tools.radar_sync.order_recon_card import build_order_recon_card

    meds = _read_active_medications(CPMRN, ENCOUNTER)
    metoprolol = _by_name(meds, "Metoprolol")
    pip = _by_name(meds, "Piperacillin")
    paracetamol = _by_name(meds, "Paracetamol")

    # Build a representative reconciliation set: 2 edits, 1 discontinue, 2 new.
    actions: dict[str, dict] = {}
    edits, discontinues, news = [], [], []

    if metoprolol:
        k = f"edit:{metoprolol['orderNo']}"
        actions[k] = {"kind": "edit", "order_no": metoprolol["orderNo"],
                      "changes": {"frequency": {**metoprolol.get("frequency", {}), "hours": 24}}}
        edits.append({"key": k, "label": "Metoprolol 25 mg PO — change frequency q12h → q24h (per chart)"})
    if pip:
        k = f"edit:{pip['orderNo']}"
        actions[k] = {"kind": "edit", "order_no": pip["orderNo"], "changes": {"quantity": 4.5}}
        edits.append({"key": k, "label": "Piperacillin+Tazobactam — correct dose 4 g → 4.5 g (per chart)"})

    if paracetamol:
        k = f"discontinue:{paracetamol['orderNo']}"
        actions[k] = {"kind": "discontinue", "order_no": paracetamol["orderNo"], "statement": "not in chart"}
        discontinues.append({"key": k, "label": "Paracetamol 500 mg IV — discontinue (not on chart)"})

    new_orders = [
        {"name": "Furosemide", "quantity": 40, "unit": "mg", "route": "IV", "form": "Injection",
         "frequency": {"fType": "every", "days": None, "hours": 12, "mins": None, "timeOfDay": None},
         "scheduleSelector": "06:00-18:00", "_label": "Furosemide 40 mg IV q12h — add (on chart, missing from EMR)"},
        {"name": "Pantoprazole", "quantity": 40, "unit": "mg", "route": "IV", "form": "Injection",
         "frequency": {"fType": "every", "days": None, "hours": 24, "mins": None, "timeOfDay": None},
         "scheduleSelector": "06:00", "_label": "Pantoprazole 40 mg IV q24h — add (on chart, missing from EMR)"},
    ]
    for i, o in enumerate(new_orders):
        k = f"new:{i}"
        label = o.pop("_label")
        actions[k] = {"kind": "new", "order": o}
        news.append({"key": k, "label": label})

    action_set_id = uuid.uuid4().hex
    save_action_set(action_set_id, CPMRN, ENCOUNTER, actions)
    print(f"Saved action set {action_set_id} ({len(actions)} actions) to GCS bucket {os.getenv('GCS_BUCKET')}")

    service_url = os.environ["GCHAT_SERVICE_URL"].rstrip("/")
    cds_url = os.environ["CDS_PUBLIC_URL"].rstrip("/")
    cb_token = os.environ["ALERT_FEEDBACK_TOKEN"]
    api_key = os.environ["GCHAT_API_KEY"]

    cards = build_order_recon_card(
        cpmrn=CPMRN, encounter=ENCOUNTER, action_set_id=action_set_id,
        edits=edits, discontinues=discontinues, news=news,
        gchat_webhook_url=f"{service_url}/webhook",
        callback_url=f"{cds_url}/order-action",
        cb_token=cb_token,
    )

    resp = requests.post(
        f"{service_url}/send-cards",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={"user_email": RECIPIENT, "cardsV2": cards},
        timeout=15,
    )
    print(f"send-cards → HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code == 200:
        print(f"✅ Card sent to {RECIPIENT} ({len(edits)} edits, {len(discontinues)} discontinue, {len(news)} new)")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "card"
    if mode == "execute":
        run_execute()
    elif mode == "card":
        run_card()
    else:
        print("usage: python -m scripts.demo_order_recon [execute|card]")
        sys.exit(1)
