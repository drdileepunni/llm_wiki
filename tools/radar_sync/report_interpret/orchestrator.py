"""
Diagnostic-report-interpretation orchestrator — the per-patient step.

Phase 1 wiring (post-pipeline): the scheduler calls `run_report_interpret`, which
chains the three building blocks:
  - select_for_cycle      — cheap watermark selection of new diagnostic reports
  - analyze_new_reports   — download + multimodal interpretation (LLM cost step)
  - finalize_report_cycle — host images, build+send the batched card, audit, watermark

Phase 2 wiring (same-cycle integration): the scheduler will call select_for_cycle +
analyze_new_reports INSIDE _run_live_pipeline (injecting findings into the delta), and
finalize_report_cycle afterwards. The building blocks are kept separable for that.

Only this module touches db / GCS / sender / BQ for the feature. Never raises — failures
are logged and returned as a status dict so the scheduler keeps going.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCHED = "snapshot_schedule"


# ── cost helpers (mirror med_recon orchestrator) ───────────────────────────────

def _step_cost(usage, step: str = "report_interpret") -> dict:
    from tools.radar_sync.study_cost_tracker import _cost
    i = getattr(usage, "input_tokens", 0)
    o = getattr(usage, "output_tokens", 0)
    t = getattr(usage, "thinking_tokens", 0)
    return {"input_tokens": i, "output_tokens": o, "thinking_tokens": t,
            "cost_usd": round(_cost(step, i, o, t), 6)}


def _trace(db, cpmrn, encounter, step, usage, final_output=None):
    """Emit a pipeline_traces row so report-interpret steps appear in pipeline_run_costs.by_step."""
    try:
        from tools.radar_sync.react_tracer import ReActTracer
        tr = ReActTracer(cpmrn, encounter, step=step, db=db)
        tr.start_round(0)
        tr.log_tokens(
            getattr(usage, "input_tokens", 0),
            getattr(usage, "output_tokens", 0),
            getattr(usage, "thinking_tokens", 0),
        )
        tr.end_round()
        tr.save(final_output=final_output)
    except Exception:
        logger.exception("report_interpret: trace emit failed for step %s", step)


def _narrative(cpmrn: str, encounter: int) -> str:
    try:
        from tools.radar_sync.patient_context import get_context
        ctx = get_context(cpmrn, encounter) or {}
        structured = ctx.get("structured_summary") or {}
        return (structured.get("narrative") or ctx.get("running_summary") or "").strip()
    except Exception:
        logger.exception("report_interpret: failed to read patient context for %s", cpmrn)
        return ""


def _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys, doc_ids=None):
    """Advance last_report_at and extend the interpreted-doc ledger (bounded 200).

    Stores both individual file_keys AND composite doc_ids (name|ts-minute) so the
    select_for_cycle dedup filter works correctly after grouping was introduced.
    """
    updated = set(seen_keys or [])
    if file_keys:
        updated.update(file_keys)
    if doc_ids:
        updated.update(doc_ids)
    ledger = list(updated)[-200:]
    try:
        db[_SCHED].update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"last_report_at": snapshot_at, "interpreted_doc_keys": ledger}},
            upsert=True,
        )
    except Exception:
        logger.exception("report_interpret: failed to advance watermark for %s enc=%d", cpmrn, encounter)


# ── building blocks ────────────────────────────────────────────────────────────

def select_for_cycle(cpmrn: str, encounter: int, chart: dict, db) -> dict:
    """
    Cheap (no-LLM) watermark selection. Returns:
      {"status": "disabled"} | {"status": "no_new_docs"|"ok",
       "new_reports": [...], "cfg": cfg, "last_report_at": dt|None, "seen_keys": set}
    """
    from tools.radar_sync.report_interpret import doc_selector

    cfg = doc_selector.load_config(db)
    if not cfg.get("enabled", True):
        return {"status": "disabled", "cfg": cfg}

    sched = db[_SCHED].find_one({"CPMRN": cpmrn, "encounter": encounter}) or {}
    last_report_at = sched.get("last_report_at")
    seen_keys = set(sched.get("interpreted_doc_keys") or [])

    new_reports = doc_selector.select_new_reports(chart, last_report_at, cfg)
    new_reports = [d for d in new_reports if d["doc_id"] not in seen_keys]

    return {
        "status": "ok" if new_reports else "no_new_docs",
        "new_reports": new_reports,
        "cfg": cfg,
        "last_report_at": last_report_at,
        "seen_keys": seen_keys,
    }


_XRAY_PATTERNS = ("xr ", " xr", "xr_", "x-ray", "xray", "radiograph", "chest pa", "chest ap",
                   "chest view", "chest x")


def _is_xray(name: str) -> bool:
    n = (name or "").lower()
    return any(p in n for p in _XRAY_PATTERNS)


def analyze_new_reports(cpmrn: str, encounter: int, new_reports: list[dict],
                        narrative: str, db) -> dict | None:
    """
    Download + interpret each selected report. Returns the analysis object (carrying the
    in-memory image bytes for hosting) or None when there is nothing to do. Never raises.

    X-rays skip the LLM entirely — image is downloaded and passed through but no
    interpretation, findings, or description are generated.
    """
    if not new_reports:
        return None
    try:
        from tools.radar_sync.med_recon import image_downloader
        from tools.radar_sync.report_interpret import interpreter
        from backend.services.llm_client import LLMUsage

        batch_id = uuid.uuid4().hex
        all_file_keys_flat = [
            fk for r in new_reports
            for fk in (r.get("all_file_keys") or [r["file_key"]])
        ]
        downloaded = image_downloader.download_all(cpmrn, all_file_keys_flat)
        by_key = {d["file_key"]: d for d in downloaded}

        reports: list[dict] = []
        total = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cost_usd": 0.0}
        for r in new_reports:
            group_keys = r.get("all_file_keys") or [r["file_key"]]
            images = [by_key[k] for k in group_keys if k in by_key and by_key[k].get("bytes")]
            report_id = uuid.uuid4().hex

            if _is_xray(r["name"]):
                # X-ray: image only, no LLM call
                res = {"report_type": "xray", "description": "", "interpretation": "",
                       "findings": [], "confidence": "n/a"}
                usage = LLMUsage(0, 0)
                logger.info("report_interpret: skipping LLM for X-ray %s (%s enc=%d)",
                            r["name"], cpmrn, encounter)
            else:
                out = interpreter.interpret_report(images, r["name"], narrative, cpmrn)
                res   = out["result"]
                usage = out["usage"]
                sc = _step_cost(usage)
                for k in ("input_tokens", "output_tokens", "thinking_tokens"):
                    total[k] += sc[k]
                total["cost_usd"] = round(total["cost_usd"] + sc["cost_usd"], 6)

            _trace(db, cpmrn, encounter, "report_interpret", usage,
                   final_output={"batch_id": batch_id, "report_id": report_id,
                                 "report_type": res.get("report_type"),
                                 "n_findings": len(res.get("findings") or []),
                                 "n_images": len(images)})
            reports.append({
                "report_id":       report_id,
                "report_type":     res.get("report_type", "other"),
                "report_name":     r["name"],
                "reported_at":     r.get("reported_at"),
                "category":        r.get("category", ""),
                "file_key":        r["file_key"],
                "all_file_keys":   group_keys,
                "doc_id":          r.get("doc_id"),
                "selection_reason": r.get("selection_reason", ""),
                "description":     res.get("description", ""),
                "interpretation":  res.get("interpretation", ""),
                "findings":        res.get("findings", []) or [],
                "confidence":      res.get("confidence", "n/a"),
                "download_ok":     bool(images),
                "image":           images[0] if images else None,
            })

        return {"batch_id": batch_id, "reports": reports, "file_keys": all_file_keys_flat,
                "step_costs": total}
    except Exception:
        logger.exception("report_interpret: analyze failed for %s enc=%d", cpmrn, encounter)
        return None


def finalize_report_cycle(cpmrn: str, encounter: int, analysis: dict, snapshot_at: datetime,
                          db, *, send: bool = True, seen_keys=None) -> dict:
    """
    Host images, build + send the batched card, write audit + run rows, advance watermark.
    `send=False` (cycle cap reached) still hosts/audits/advances but suppresses the card.
    Never raises.
    """
    from tools.radar_sync.report_interpret import audit, report_card
    from tools.radar_sync.med_recon import image_host
    from tools.radar_sync.chat_card_sender import get_alert_recipients, send_cards_to_recipients

    batch_id = analysis["batch_id"]
    reports = analysis["reports"]
    file_keys = analysis.get("file_keys") or [r["file_key"] for r in reports]
    recon_at = datetime.now(timezone.utc)
    model = os.getenv("MODEL", "")

    # 1. host images per report
    for r in reports:
        img = r.get("image")
        urls = []
        if img and img.get("bytes"):
            try:
                urls = image_host.host_chart_images(
                    cpmrn, encounter, f"{batch_id}/{r['report_id']}", [img], prefix="report_images",
                )
            except Exception:
                logger.exception("report_interpret: image host failed for %s", r["report_id"])
        r["image_urls"] = urls

    # 2. build + send as few cards as possible — reports are packed into cards under
    #    GChat's ~10-section limit, so N reports typically becomes fewer than N messages.
    card_sent, recipients = False, []
    if send:
        recipients = get_alert_recipients(db)
        if recipients:
            card_batches = report_card.build_report_interpret_cards(
                cpmrn=cpmrn, encounter=encounter, batch_id=batch_id, reports=reports,
                patient_narrative=analysis.get("narrative", ""),
            )
            any_sent = False
            for cards in card_batches:
                if send_cards_to_recipients(cards, recipients):
                    any_sent = True
                else:
                    logger.warning(
                        "report_interpret: card send failed for batch %s (%s enc=%d)",
                        batch_id, cpmrn, encounter,
                    )
            card_sent = any_sent
        else:
            logger.warning("report_interpret: no alert recipients configured — card not sent (%s)", cpmrn)

    outcome = "processed" if (card_sent or not send) else "send_failed"
    if not send:
        outcome = "skipped_cycle_limit"

    # 3. per-report audit rows
    findings_injected = 0
    for r in reports:
        findings_injected += len(r.get("findings") or [])
        audit.write_report_audit({
            "report_id": r["report_id"], "batch_id": batch_id,
            "CPMRN": cpmrn, "encounter": encounter,
            "snapshot_at": snapshot_at, "interpreted_at": recon_at,
            "document_file_key": r["file_key"], "document_category": r.get("category", ""),
            "document_name": r.get("report_name", ""), "reported_at": r.get("reported_at"),
            "selection_reason": r.get("selection_reason", ""),
            "download_ok": bool(r.get("download_ok")), "interpret_ok": bool(r.get("interpretation")),
            "summary_narrative": analysis.get("narrative", ""),
            "raw_description": r.get("description", ""),
            "interpretation": r.get("interpretation", ""),
            "report_type": r.get("report_type", ""), "confidence": r.get("confidence", ""),
            "findings": r.get("findings", []),
            "image_urls": r.get("image_urls", []),
            "model": model,
            "card_sent": card_sent, "recipients": recipients,
            "step_costs": analysis.get("step_costs", {}),
        })

    # 4. advance watermark — include composite doc_ids so the grouping-based dedup filter
    #    correctly skips these reports on the next cycle even if the watermark is reset.
    doc_ids = [r.get("doc_id") for r in reports if r.get("doc_id")]
    _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys, doc_ids=doc_ids or None)

    # 5. run-outcome row
    audit.write_report_run({
        "run_id": uuid.uuid4().hex, "batch_id": batch_id,
        "CPMRN": cpmrn, "encounter": encounter,
        "cycle_started_at": snapshot_at, "evaluated_at": recon_at,
        "outcome": outcome,
        "n_reports_found": len(file_keys), "n_reports_interpreted": len(reports),
        "findings_injected": findings_injected, "problems_touched": 0,  # Phase 2 fills this
        "card_sent": card_sent, "recipients": recipients,
        "watermark_before": None, "watermark_after": snapshot_at,
        "cost_usd": analysis.get("step_costs", {}).get("cost_usd", 0.0),
        "error_detail": "",
    })

    # strip image bytes from the in-memory object before returning
    for r in reports:
        r.pop("image", None)

    return {"batch_id": batch_id, "n_reports": len(reports), "card_sent": card_sent,
            "recipients": recipients, "outcome": outcome,
            "cost_usd": analysis.get("step_costs", {}).get("cost_usd", 0.0)}


# ── Phase-1 entry point ─────────────────────────────────────────────────────────

def run_report_interpret(cpmrn: str, encounter: int, chart: dict, snapshot_at: datetime,
                         db, *, send: bool = True) -> dict:
    """
    Phase-1 per-patient entry: select → analyze → finalize. Post-pipeline (does NOT feed
    the problem list yet — that is Phase 2). Never raises.

    `send=False` runs the full analysis + audit + watermark but suppresses the card
    (used when the per-cycle card cap is reached).
    """
    try:
        sel = select_for_cycle(cpmrn, encounter, chart, db)
        status = sel.get("status")

        if status == "disabled":
            _run_skip_row(cpmrn, encounter, snapshot_at, "disabled", db)
            return {"skipped": "disabled"}

        if status == "no_new_docs":
            # Seed the watermark on a true first run so we never bulk-scan history.
            if sel.get("last_report_at") is None:
                _advance_watermark(db, cpmrn, encounter, snapshot_at, sel.get("seen_keys"), None)
            _run_skip_row(cpmrn, encounter, snapshot_at, "no_new_docs", db)
            return {"skipped": "no_new_docs"}

        narrative = _narrative(cpmrn, encounter)
        analysis = analyze_new_reports(cpmrn, encounter, sel["new_reports"], narrative, db)
        if not analysis:
            _run_skip_row(cpmrn, encounter, snapshot_at, "error", db)
            return {"error": "analyze_failed"}
        analysis["narrative"] = narrative

        return finalize_report_cycle(
            cpmrn, encounter, analysis, snapshot_at, db,
            send=send, seen_keys=sel.get("seen_keys"),
        )
    except Exception:
        logger.exception("report_interpret: run failed for %s enc=%d", cpmrn, encounter)
        _run_skip_row(cpmrn, encounter, snapshot_at, "error", db, detail="exception")
        return {"error": "exception"}


def _run_skip_row(cpmrn, encounter, snapshot_at, outcome, db, detail=""):
    """Write a runs row for a non-processing path (disabled / no_new_docs / error)."""
    from tools.radar_sync.report_interpret import audit
    audit.write_report_run({
        "run_id": uuid.uuid4().hex, "batch_id": "",
        "CPMRN": cpmrn, "encounter": encounter,
        "cycle_started_at": snapshot_at, "evaluated_at": datetime.now(timezone.utc),
        "outcome": outcome,
        "n_reports_found": 0, "n_reports_interpreted": 0,
        "findings_injected": 0, "problems_touched": 0,
        "card_sent": False, "recipients": [],
        "watermark_before": None, "watermark_after": None,
        "cost_usd": 0.0, "error_detail": detail,
    })
