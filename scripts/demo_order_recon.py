"""
Demo: medication-reconciliation order actions on the TEST patient INKLERN261338/1.

Three parts:

  python -m scripts.demo_order_recon execute
      Direct engine proof — applies one edit, one discontinue, one new order
      directly (no card), then re-reads to confirm.

  python -m scripts.demo_order_recon card
      Builds a reconciliation card from the patient's CURRENT orders, persists the
      proposed action set to GCS, and sends the card to the recipient so the UX is
      visible in Google Chat. (The Submit button only executes once /order-action
      is deployed — see plan.)

  python -m scripts.demo_order_recon card_image
      Same as 'card' but includes a synthetic treatment-chart image in the collapsible
      accordion so the full UX (including the image section) can be reviewed.

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


def _make_sample_chart_image() -> bytes:
    """Create a synthetic treatment-chart image (no PHI) for demo purposes."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    W, H = 800, 600
    img = Image.new("RGB", (W, H), color=(255, 255, 250))
    draw = ImageDraw.Draw(img)

    # Outer border
    draw.rectangle([10, 10, W - 10, H - 10], outline=(0, 0, 0), width=2)

    # Header box
    draw.rectangle([10, 10, W - 10, 60], fill=(220, 235, 255), outline=(0, 0, 0), width=2)
    draw.text((W // 2, 35), "TREATMENT CHART  [DEMO — SYNTHETIC DATA]",
              fill=(20, 20, 120), anchor="mm")

    # Column headers
    cols = ["Medication", "Dose", "Route", "Frequency", "Signature"]
    col_x = [20, 240, 380, 470, 620]
    draw.rectangle([10, 60, W - 10, 90], fill=(240, 240, 240), outline=(180, 180, 180))
    for i, (label, x) in enumerate(zip(cols, col_x)):
        draw.text((x, 75), label, fill=(60, 60, 60), anchor="lm")

    # Sample rows
    rows = [
        ("Furosemide",          "40 mg",  "IV",  "q12h",  "✓"),
        ("Pantoprazole",        "40 mg",  "IV",  "q24h",  "✓"),
        ("Metoprolol",          "25 mg",  "PO",  "q24h",  "✓"),   # changed from q12h
        ("Cefoperazone/Sulb.",  "1.5 g",  "IV",  "BD",    "✓"),
        ("Carvedilol",          "3.125 mg","PO", "BD",    "✓"),   # missing from EMR
        ("Iprat/Levosalbut.",   "2.5 mL", "NEB", "QID",   "✓"),
        ("Norepinephrine",      "PRN",    "IV",  "infusion","✓"),
        ("Insulin (basal)",     "10 U",   "SC",  "q24h",  "✓"),
    ]
    for row_i, row in enumerate(rows):
        y_top = 90 + row_i * 40
        bg = (255, 255, 255) if row_i % 2 == 0 else (248, 248, 255)
        draw.rectangle([10, y_top, W - 10, y_top + 40], fill=bg, outline=(200, 200, 200))
        for val, x in zip(row, col_x):
            draw.text((x, y_top + 20), val, fill=(30, 30, 30), anchor="lm")

    # Footer note
    draw.text((W // 2, H - 25),
              "Attending sign-off: ██████  |  Date: [redacted]  |  Ward: ICU-B",
              fill=(120, 120, 120), anchor="mm")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _get_radar_chart_image_url(cpmrn: str, encounter: int = 1) -> str | None:
    """
    Find the first document in the patient's chart and return Radar's short-lived
    signed URL for it (a pre-signed GCS link that Google Chat can fetch directly).
    Uses pull_chart (the Firebase function path) to get the chart, then the
    download endpoint to get the signed URL without following it.
    """
    import requests as req
    from tools.radar_sync.chart_puller import pull_chart
    from tools.radar_sync.order_actions import _bearer, _post_url

    try:
        chart = pull_chart(cpmrn, encounter)
    except Exception as exc:
        print(f"  pull_chart failed: {exc}")
        return None

    docs = chart.get("documents") or []
    # Prefer treatment charts / progress notes; fall back to any doc with a key
    candidates = [
        d for d in docs
        if d.get("category") == "documents" and d.get("key")
           and any(p in (d.get("name") or d.get("label") or "").lower()
                   for p in ("treatment chart", "progress note", "chart"))
    ]
    if not candidates:
        candidates = [d for d in docs if d.get("key")]
    if not candidates:
        print(f"  no documents found for {cpmrn}")
        return None

    file_key = candidates[0]["key"]
    doc_name = candidates[0].get("name") or candidates[0].get("label") or "document"
    print(f"  using: {doc_name!r}  key={file_key[:50]}…")

    # POST to download endpoint — extract the signed URL without following it
    headers = {
        "authorization": f"Bearer {_bearer()}",
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://cloudphysicianworld.com",
        "referer": "https://cloudphysicianworld.com/",
    }
    dl_resp = req.post(
        f"{_post_url()}/api/patients/{cpmrn}/download/",
        headers=headers, json={"key": file_key}, timeout=30,
    )
    if not dl_resp.ok:
        print(f"  download request failed: HTTP {dl_resp.status_code}")
        return None
    ct = dl_resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        signed = dl_resp.json().get("data") or dl_resp.json().get("url")
        if signed:
            print("  ✓ got signed URL from Radar")
            return signed
    print("  download response was raw bytes — image accordion skipped")
    return None


def run_card_with_image():
    """Same as run_card() but adds a synthetic chart image to the accordion."""
    from tools.radar_sync.order_actions import _read_active_medications
    from tools.radar_sync.order_action_store import save_action_set
    from tools.radar_sync.order_recon_card import build_order_recon_card

    meds = _read_active_medications(CPMRN, ENCOUNTER)
    metoprolol = _by_name(meds, "Metoprolol")
    pip = _by_name(meds, "Piperacillin")
    paracetamol = _by_name(meds, "Paracetamol")

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

    recon_id = uuid.uuid4().hex
    action_set_id = uuid.uuid4().hex
    save_action_set(action_set_id, CPMRN, ENCOUNTER, actions)
    print(f"Saved action set {action_set_id} ({len(actions)} actions)")

    # Fetch a real chart image URL directly from Radar (short-lived signed URL).
    # No re-hosting needed — Google Chat can fetch the signed URL directly.
    print(f"Fetching a chart image URL from {CPMRN} via Radar...")
    image_url = _get_radar_chart_image_url(CPMRN, ENCOUNTER)
    if not image_url:
        # Test patient may have no documents — try a known patient with charts
        print("  falling back to INTSHYD456101 for demo image...")
        image_url = _get_radar_chart_image_url("INTSHYD456101", 1)
    if not image_url:
        print("  no image URL available — card will send without the image accordion")
    chart_image_urls = [image_url] if image_url else []

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
        chart_image_urls=chart_image_urls,
        recon_id=recon_id,
    )

    resp = requests.post(
        f"{service_url}/send-cards",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json={"user_email": RECIPIENT, "cardsV2": cards},
        timeout=15,
    )
    print(f"send-cards → HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code == 200:
        img_note = "1 image" if chart_image_urls else "no image (URL unavailable)"
        print(f"✅ Card sent to {RECIPIENT} "
              f"({len(edits)} edits, {len(discontinues)} discontinue, {len(news)} new, {img_note})")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "card"
    if mode == "execute":
        run_execute()
    elif mode == "card":
        run_card()
    elif mode == "card_image":
        run_card_with_image()
    else:
        print("usage: python -m scripts.demo_order_recon [execute|card|card_image]")
        sys.exit(1)
