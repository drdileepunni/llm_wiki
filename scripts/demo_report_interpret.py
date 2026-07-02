"""
Demo / dry-run for the diagnostic-report interpretation module.

Modes:

  python -m scripts.demo_report_interpret select [CPMRN] [ENCOUNTER]
      No-secret dry run. Reads the latest GCS snapshot, forces the watermark back,
      and prints the diagnostic reports that select_new_reports() would pick — no
      Radar download, no LLM, no card. Proves the selector against real data.

  python -m scripts.demo_report_interpret card_offline
      No-secret card-JSON proof. Builds the batched card(s) from synthetic report
      data and prints the cardsV2 JSON (findings chips + transcription/image
      accordions; no interpretation text, no rating). No send.

  python -m scripts.demo_report_interpret analyze [CPMRN] [ENCOUNTER]
      Full dry run (needs GOOGLE_API_KEY + Radar REFRESH_TOKEN/RADAR_POST_URL).
      select → download → interpret; prints reports, findings, costs. No send.
      Run via the run-demo wrapper so secrets are present.

  python -m scripts.demo_report_interpret send [CPMRN] [ENCOUNTER]
      Full path incl. hosting + sending the card to the recipient (needs all secrets).
      Run via the run-demo wrapper.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta

RECIPIENT = "dileep.unni@cloudphysician.net"
_DEFAULT_CPMRN = "INTSHYD456101"
_DEFAULT_ENCOUNTER = 1


def _db():
    sys.path.insert(0, "app")
    sys.path.insert(0, ".")
    from backend.services.emr.db import get_db
    return get_db()


def _latest_snapshot(db, cpmrn, encounter):
    return db.snapshots.find_one({"CPMRN": cpmrn, "encounter": encounter},
                                 sort=[("snapshot_at", -1)])


def run_select(cpmrn, encounter):
    """No-secret: prove selection against a real snapshot with a forced-back watermark."""
    from tools.radar_sync.report_interpret import doc_selector

    db = _db()
    snap = _latest_snapshot(db, cpmrn, encounter)
    if not snap:
        print(f"No snapshot for {cpmrn}/{encounter}")
        return
    chart = snap.get("chart") or {}
    docs = chart.get("documents") or []
    scans = [d for d in docs if (d.get("category") or "").lower() == "scans"]
    print(f"Snapshot {snap.get('snapshot_at')} — {len(docs)} documents, {len(scans)} in 'scans':")
    for d in scans:
        print(f"  - {d.get('name')!r} reportedAt={d.get('reportedAt')} key={'yes' if d.get('key') else 'NO'}")

    cfg = doc_selector.load_config(db)
    print(f"\nConfig: categories={cfg['categories']} patterns={cfg['type_patterns'][:6]}…")

    # Force the watermark back 30 days so everything recent is 'new'
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    selected = doc_selector.select_new_reports(chart, cutoff, cfg)
    print(f"\nselect_new_reports(cutoff={cutoff.date()}) → {len(selected)} report(s):")
    for r in selected:
        print(f"  • {r['name']!r}  [{r['category']}]  {r['selection_reason']}  reported={r['reported_at']}")


def run_card_offline():
    """No-secret: build the batched card from synthetic data and print the JSON."""
    from tools.radar_sync.report_interpret import report_card

    reports = [
        {
            "report_id": "rid-echo-1", "report_type": "echo", "report_name": "ECHO Report",
            "reported_at": "2026-06-18T05:30:00Z",
            "interpretation": "Severe LV systolic dysfunction (LVEF ~30%) with global hypokinesia — "
                              "consistent with new congestive heart failure in this post-op patient "
                              "presenting with respiratory distress.",
            "description": "TTE: LVEF 30%, global hypokinesia, mild MR, no pericardial effusion. "
                           "RV function preserved.",
            "findings": [
                {"label": "Congestive heart failure", "is_new_diagnosis": True, "body_system": "cardiac",
                 "severity": "notable", "supporting_evidence": "LVEF 30%, global hypokinesia"},
            ],
            "image_urls": ["https://example.com/echo.jpg"],
        },
        {
            "report_id": "rid-cxr-1", "report_type": "xray", "report_name": "XR Chest PA View",
            "reported_at": "2026-06-18T05:45:00Z",
            "interpretation": "Bilateral interstitial opacities and upper-lobe diversion — pulmonary "
                              "oedema, supporting the echo finding of cardiac failure.",
            "description": "Cardiomegaly, bilateral perihilar haziness, Kerley B lines, small effusions.",
            "findings": [
                {"label": "Pulmonary oedema", "is_new_diagnosis": False, "body_system": "respiratory",
                 "severity": "notable", "supporting_evidence": "Kerley B lines, perihilar haze"},
            ],
            "image_urls": ["https://example.com/cxr.jpg"],
        },
    ]
    card_batches = report_card.build_report_interpret_cards(
        cpmrn="INDEMO0001", encounter=1, batch_id="batch-demo",
        reports=reports,
    )
    print(json.dumps(card_batches, indent=2, default=str))
    # quick structural assertions
    assert card_batches, "expected at least one card"
    sections = card_batches[0][0]["card"]["sections"]
    headers = [s.get("header", "") for s in sections]
    assert not any(h.startswith("Rate report:") for h in headers), "rating section should be gone"
    assert not any(h == "Interpretation" for h in headers), "interpretation section should be gone"
    assert any(s.get("collapsible") for s in sections), "missing collapsible accordion"
    print(f"\n✅ card structure OK: {len(card_batches)} card(s), "
          f"{len(sections)} section(s) in first card")


def run_analyze(cpmrn, encounter, send=False):
    """Full dry run (needs secrets): select → download → interpret [→ host+send]."""
    from tools.radar_sync.report_interpret import orchestrator

    db = _db()
    snap = _latest_snapshot(db, cpmrn, encounter)
    if not snap:
        print(f"No snapshot for {cpmrn}/{encounter}")
        return
    chart = snap.get("chart") or {}

    sel = orchestrator.select_for_cycle(cpmrn, encounter, chart, db)
    print(f"select_for_cycle → status={sel['status']} "
          f"({len(sel.get('new_reports', []))} reports; watermark={sel.get('last_report_at')})")
    if sel["status"] != "ok":
        # Force selection by passing a 30-day-back cutoff directly
        from tools.radar_sync.report_interpret import doc_selector
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        sel["new_reports"] = [d for d in doc_selector.select_new_reports(chart, cutoff, sel["cfg"])
                              if d["doc_id"] not in sel.get("seen_keys", set())]
        print(f"  (forced watermark back 30d → {len(sel['new_reports'])} reports)")
    if not sel["new_reports"]:
        print("  nothing to interpret")
        return

    narrative = orchestrator._narrative(cpmrn, encounter)
    analysis = orchestrator.analyze_new_reports(cpmrn, encounter, sel["new_reports"], narrative, db)
    if not analysis:
        print("  analyze returned None")
        return
    print(f"\nbatch_id={analysis['batch_id']}  cost=${analysis['step_costs']['cost_usd']}")
    for r in analysis["reports"]:
        print(f"\n── {r['report_name']} ({r['report_type']}, conf={r['confidence']}, "
              f"download_ok={r['download_ok']}) ──")
        print(f"  description: {r['description'][:200]}")
        print(f"  interpretation: {r['interpretation'][:300]}")
        for f in r["findings"]:
            print(f"  finding: [{f.get('severity')}] {f.get('label')} "
                  f"(new_dx={f.get('is_new_diagnosis')}) — {f.get('supporting_evidence')}")

    if send:
        analysis["narrative"] = narrative
        snap_ts = snap.get("snapshot_at")
        if isinstance(snap_ts, str):
            snap_ts = datetime.fromisoformat(snap_ts.replace("Z", "+00:00"))
        result = orchestrator.finalize_report_cycle(
            cpmrn, encounter, analysis, snap_ts, db, send=True, seen_keys=sel.get("seen_keys"),
        )
        print(f"\nfinalize → {result}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "select"
    cpmrn = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_CPMRN
    enc = int(sys.argv[3]) if len(sys.argv) > 3 else _DEFAULT_ENCOUNTER
    if mode == "select":
        run_select(cpmrn, enc)
    elif mode == "card_offline":
        run_card_offline()
    elif mode == "analyze":
        run_analyze(cpmrn, enc, send=False)
    elif mode == "send":
        run_analyze(cpmrn, enc, send=True)
    else:
        print("usage: python -m scripts.demo_report_interpret [select|card_offline|analyze|send] [CPMRN] [ENC]")
        sys.exit(1)
