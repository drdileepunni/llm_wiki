"""
Demo: send an insulin alert card for a given CPMRN/encounter/glucose value.

Usage:
    python -m scripts.demo_insulin_alert

Sends a card to RECIPIENT in Google Chat showing the 💉 Insulin guidance
section with a placeable Regular Insulin order checkbox. Clicking "Place
selected insulin order" in the card will call /order-action and place the
order into Radar.

Required env (same as demo_order_recon):
    REFRESH_TOKEN, RADAR_POST_URL, RADAR_READ_URL, RADAR_READ_SERVICE_ACCOUNT,
    GCS_BUCKET, GCHAT_SERVICE_URL, GCHAT_API_KEY, CDS_PUBLIC_URL, ALERT_FEEDBACK_TOKEN
"""
from __future__ import annotations

import os
import sys
import uuid

CPMRN    = "INKLERN261338"
ENCOUNTER = 1
GLUCOSE  = 400.0
RECIPIENT = "dileep.unni@cloudphysician.net"


def run():
    from tools.radar_sync.insulin_advice import compute, gather_inputs, _build_order_payload, _human_label
    from tools.radar_sync.order_action_store import save_action_set
    from tools.radar_sync.alert_cards import build_alert_card
    from tools.radar_sync.chat_card_sender import send_cards_to_recipients

    # ── 1. Gather real EMR inputs (active insulin orders, diet, vasopressors) ──
    #      Override GRBS with the test glucose value so we can control it.
    engine_input, sourced = gather_inputs(CPMRN, ENCOUNTER)
    engine_input["GRBS"] = [GLUCOSE]  # override with test glucose
    from datetime import datetime, timezone, timedelta
    _IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(timezone.utc).astimezone(_IST).strftime("%-d %b, %-I:%M %p IST")
    sourced["grbs"] = {
        "label": f"Glucose {int(GLUCOSE)} mg/dL (test value, prior insulin/route from EMR)",
        "assumed": False, "found": True,
        "values": [GLUCOSE],
        "readings": [{"value": GLUCOSE, "ts_ist": f"{now_ist} (test)"}],
    }

    print("── EMR inputs ──────────────────────────────────────────")
    for k, v in sourced.items():
        flag = "⚠ assumed" if v.get("assumed") else "✓"
        print(f"  {flag}  {v.get('label', k)}")
    print("────────────────────────────────────────────────────────")

    # ── 2. Compute recommendation ──────────────────────────────────────────────
    reco = compute(engine_input)
    if "error" in reco:
        print(f"❌ Engine error: {reco['error']}")
        sys.exit(1)

    print(f"✅ Engine recommendation: {reco}")

    dose = reco["Suggested_insulin_dose"]
    unit = reco.get("unit", "IU")
    label = _human_label(reco, GLUCOSE)

    # ── 3. Build SC order payload + persist action set ─────────────────────────
    order_payload = _build_order_payload(reco, GLUCOSE)
    action_set_id = uuid.uuid4().hex
    action_set = {
        "insulin:0": {
            "kind": "new",
            "order": order_payload,
            "label": label,
        }
    }
    save_action_set(action_set_id, CPMRN, ENCOUNTER, action_set)
    print(f"✅ Action set {action_set_id} saved to GCS (bucket: {os.getenv('GCS_BUCKET')})")

    # ── 4. Build assessment dict ───────────────────────────────────────────────
    assessment = {
        "problem_name": "Hyperglycemia",
        "clinical_status": "worsening",
        "alert_title": f"Glucose {int(GLUCOSE)} mg/dL — insulin order required",
        "alert_reason": (
            f"Blood glucose is {int(GLUCOSE)} mg/dL. "
            f"CDS insulin protocol (Basal Bolus Level {reco.get('level', '?')}) recommends "
            f"{dose} {unit} SC. No active insulin order detected."
        ),
        "next_check": {
            "type": "lab",
            "lab_name": "glucose",
            "label": f"Glucose (check in {reco.get('next_grbs_after', '?')}h)",
        },
        "_insulin_order": {
            "reco": reco,
            "sourced": sourced,
            "current_grbs": GLUCOSE,
            "label": label,
            "action_set_id": action_set_id,
            "advisory_only": False,
        },
    }

    structured_summary = {
        "problems": [
            {"name": "Hyperglycemia", "status": "worsening", "current_state": f"Glucose {int(GLUCOSE)} mg/dL"}
        ]
    }

    # ── 5. Build card ──────────────────────────────────────────────────────────
    service_url = os.environ.get("GCHAT_SERVICE_URL", "").rstrip("/")
    cds_url     = os.environ.get("CDS_PUBLIC_URL", "").rstrip("/")
    cb_token    = os.environ.get("ALERT_FEEDBACK_TOKEN", "")

    cards_v2 = build_alert_card(
        cpmrn=CPMRN,
        encounter=ENCOUNTER,
        assessment=assessment,
        structured_summary=structured_summary,
        alert_id=uuid.uuid4().hex,
        gchat_webhook_url=f"{service_url}/webhook",
        callback_url=f"{cds_url}/alert-feedback",
        cb_token=cb_token,
        order_callback_url=f"{cds_url}/order-action",
    )

    # ── 6. Send card ───────────────────────────────────────────────────────────
    ok = send_cards_to_recipients(cards_v2, [RECIPIENT])
    if ok:
        print(f"✅ Card sent to {RECIPIENT}")
        print(f"   Tick the checkbox and submit to place the order in Radar.")
    else:
        print(f"❌ Failed to send card — check GCHAT_API_KEY and GCHAT_SERVICE_URL")
        sys.exit(1)


if __name__ == "__main__":
    run()
