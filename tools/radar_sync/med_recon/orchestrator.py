"""
Medication-reconciliation orchestrator — the per-patient step wired into the pipeline.

Flow (run_med_recon):
  1. select new treatment-chart/progress-note docs since last_recon_at  → skip if none
  2. download the images
  3. transcribe (summary-seeded, multimodal)               [cost step: med_recon_transcribe]
  4. read active EMR meds
  5. reconcile chart meds vs active orders                 [cost step: med_recon_reconcile]
  6. guardrail → keyed action set (Moderate posture)
  7. dedup (content hash + seen-ledger)                    → audit-only if identical/unactioned
  8. persist action set + host chart images
  9. build + send the reconciliation card
 10. write the full BQ audit row (incl. per-step costs)
 11. advance watermark (last_recon_at, seen keys, last hash)

Only this module touches db / GCS action store / sender / BQ. Never raises — failures are
logged and returned as a status dict so the scheduler keeps going.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCHED = "snapshot_schedule"


def _canonical_hash(action_set: dict) -> str:
    """Stable hash of the proposed action set (order-independent)."""
    norm = []
    for key in sorted(action_set):
        a = action_set[key]
        norm.append({
            "k": key, "kind": a.get("kind"), "order_no": a.get("order_no"),
            "changes": a.get("changes"), "order": a.get("order"),
        })
    return hashlib.sha256(json.dumps(norm, sort_keys=True, default=str).encode()).hexdigest()


def _split_for_card(action_set: dict) -> tuple[list, list, list]:
    edits, discontinues, news = [], [], []
    for key, a in action_set.items():
        item = {"key": key, "label": a.get("label", key)}
        if a["kind"] == "edit":
            edits.append(item)
        elif a["kind"] == "discontinue":
            discontinues.append(item)
        elif a["kind"] == "new":
            news.append(item)
    return edits, discontinues, news


def _step_cost(usage, step: str = "med_recon") -> dict:
    from tools.radar_sync.study_cost_tracker import _cost
    i = getattr(usage, "input_tokens", 0)
    o = getattr(usage, "output_tokens", 0)
    t = getattr(usage, "thinking_tokens", 0)
    return {"input_tokens": i, "output_tokens": o, "thinking_tokens": t,
            "cost_usd": round(_cost(step, i, o, t), 6)}


def _trace(db, cpmrn, encounter, step, usage, final_output=None):
    """Emit a pipeline_traces row so med-recon steps appear in pipeline_run_costs.by_step."""
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
        logger.exception("med_recon: trace emit failed for step %s", step)


def run_med_recon(cpmrn: str, encounter: int, chart: dict, snapshot_at: datetime, db) -> dict:
    try:
        return _run(cpmrn, encounter, chart, snapshot_at, db)
    except Exception:
        logger.exception("med_recon: run failed for %s enc=%d", cpmrn, encounter)
        return {"error": "exception"}


def _run(cpmrn: str, encounter: int, chart: dict, snapshot_at: datetime, db) -> dict:
    from tools.radar_sync.med_recon import doc_selector, image_downloader, transcriber, recon_engine
    from tools.radar_sync.med_recon import image_host, audit

    cfg = doc_selector.load_config(db)
    if not cfg.get("enabled", True):
        return {"skipped": "disabled"}

    sched = db[_SCHED].find_one({"CPMRN": cpmrn, "encounter": encounter}) or {}
    last_recon_at = sched.get("last_recon_at")
    seen_keys = set(sched.get("reconciled_doc_keys") or [])

    new_docs = doc_selector.select_new_treatment_docs(chart, last_recon_at, cfg)
    # Drop any doc already reconciled (seen-ledger guard against watermark resets)
    new_docs = [d for d in new_docs if d["doc_id"] not in seen_keys]

    if not new_docs:
        # Advance the watermark even with nothing new so we don't re-scan history;
        # only set it on a true first run (last_recon_at absent) to seed the watermark.
        if last_recon_at is None:
            _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, None)
        return {"skipped": "no_new_docs"}

    recon_id = uuid.uuid4().hex
    file_keys = [d["file_key"] for d in new_docs]
    recon_at = datetime.now(timezone.utc)

    # 2. download
    images = image_downloader.download_all(cpmrn, file_keys)

    # 3. transcribe (summary-seeded)
    narrative = _narrative(cpmrn, encounter)
    tr = transcriber.transcribe_treatment_charts(images, narrative, cpmrn)
    extracted_meds = tr["extracted_meds"]
    _trace(db, cpmrn, encounter, "med_recon_transcribe", tr["usage"],
           final_output={"recon_id": recon_id, "n_meds": len(extracted_meds)})

    # 4. active EMR meds
    from tools.radar_sync.order_actions import _read_active_medications
    active = _read_active_medications(cpmrn, encounter)

    # 5. reconcile
    rec = recon_engine.reconcile(extracted_meds, active, narrative, cpmrn, encounter)
    _trace(db, cpmrn, encounter, "med_recon_reconcile", rec["usage"],
           final_output={"recon_id": recon_id, "n_items": len(rec["items"])})

    step_costs = {
        "transcribe": _step_cost(tr["usage"]),
        "reconcile": _step_cost(rec["usage"]),
    }
    step_costs["total_cost_usd"] = round(
        step_costs["transcribe"]["cost_usd"] + step_costs["reconcile"]["cost_usd"], 6
    )

    # 6. guardrail → action set
    action_set = recon_engine.to_action_set(rec)

    audit_doc = {
        "recon_id": recon_id, "CPMRN": cpmrn, "encounter": encounter,
        "snapshot_at": snapshot_at, "recon_at": recon_at,
        "document_file_keys": file_keys,
        "document_reported_ats": [d["reported_at"] for d in new_docs],
        "document_categories": [d["category"] for d in new_docs],
        "summary_narrative": narrative,
        "raw_transcription": tr["raw_text"],
        "extracted_meds": extracted_meds,
        "active_orders_snapshot": active,
        "recon_items": rec["items"],
        "proposed_actions": action_set,
        "action_set_id": None,
        "image_urls": [],
        "model": os.getenv("MODEL", ""),
        "discrepancy_count": len(action_set),
        "card_sent": False,
        "recipients": [],
        "dedup_of": None,
        "step_costs": step_costs,
    }

    # No actionable discrepancies → audit only, advance watermark.
    if not action_set:
        audit.write_recon_audit(audit_doc)
        _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys)
        return {"recon_id": recon_id, "discrepancies": 0, "cost_usd": step_costs["total_cost_usd"]}

    # 7. dedup: identical proposal to a prior UNACTIONED card → skip send
    new_hash = _canonical_hash(action_set)
    prior_hash = sched.get("last_recon_hash")
    prior_action_set_id = sched.get("last_recon_action_set_id")
    if prior_hash == new_hash and not _was_actioned(prior_action_set_id):
        audit_doc["dedup_of"] = sched.get("last_recon_id")
        audit.write_recon_audit(audit_doc)
        _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys)
        return {"recon_id": recon_id, "discrepancies": len(action_set), "deduped": True,
                "cost_usd": step_costs["total_cost_usd"]}

    # 8. persist action set + host images
    from tools.radar_sync.order_action_store import save_action_set
    action_set_id = uuid.uuid4().hex
    save_action_set(action_set_id, cpmrn, encounter, action_set)
    image_urls = image_host.host_chart_images(cpmrn, encounter, recon_id, images)

    # 9. build + send card
    sent = _build_and_send(db, cpmrn, encounter, action_set_id, action_set, image_urls, recon_id)

    # 10. audit
    audit_doc.update({
        "action_set_id": action_set_id, "image_urls": image_urls,
        "card_sent": sent["ok"], "recipients": sent["recipients"],
    })
    audit.write_recon_audit(audit_doc)

    # 11. watermark + dedup state
    _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys, extra={
        "last_recon_hash": new_hash,
        "last_recon_id": recon_id,
        "last_recon_action_set_id": action_set_id,
    })

    return {"recon_id": recon_id, "discrepancies": len(action_set), "card_sent": sent["ok"],
            "recipients": sent["recipients"], "cost_usd": step_costs["total_cost_usd"]}


def _narrative(cpmrn: str, encounter: int) -> str:
    try:
        from tools.radar_sync.patient_context import get_context
        ctx = get_context(cpmrn, encounter) or {}
        structured = ctx.get("structured_summary") or {}
        return (structured.get("narrative") or ctx.get("running_summary") or "").strip()
    except Exception:
        logger.exception("med_recon: failed to read patient context for %s", cpmrn)
        return ""


def _build_and_send(db, cpmrn, encounter, action_set_id, action_set, image_urls, recon_id) -> dict:
    from tools.radar_sync.order_recon_card import build_order_recon_card
    from tools.radar_sync.chat_card_sender import get_alert_recipients, send_cards_to_recipients

    recipients = get_alert_recipients(db)
    if not recipients:
        logger.warning("med_recon: no alert recipients configured — card not sent (%s)", cpmrn)
        return {"ok": False, "recipients": []}

    service_url = (os.getenv("GCHAT_SERVICE_URL") or "").rstrip("/")
    cds_url = (os.getenv("CDS_PUBLIC_URL") or "").rstrip("/")
    cb_token = os.getenv("ALERT_FEEDBACK_TOKEN", "")
    edits, discontinues, news = _split_for_card(action_set)

    cards = build_order_recon_card(
        cpmrn=cpmrn, encounter=encounter, action_set_id=action_set_id,
        edits=edits, discontinues=discontinues, news=news,
        gchat_webhook_url=f"{service_url}/webhook",
        callback_url=f"{cds_url}/order-action",
        cb_token=cb_token, chart_image_urls=image_urls, recon_id=recon_id,
    )
    ok = send_cards_to_recipients(cards, recipients)
    return {"ok": ok, "recipients": recipients}


def _was_actioned(action_set_id: str | None) -> bool:
    """True if any med_recon_actions row exists for this action_set_id."""
    if not action_set_id:
        return False
    try:
        from google.cloud import bigquery
        from app.backend.services.bq_store import get_bq_store
        store = get_bq_store()
        store._ensure_table("med_recon_actions")
        sql = (f"SELECT COUNT(*) AS n FROM {store._fqn('med_recon_actions')} "
               f"WHERE action_set_id = @a")
        res = store._query(sql, [bigquery.ScalarQueryParameter("a", "STRING", action_set_id)])
        return bool(res and res[0].get("n", 0) > 0)
    except Exception:
        logger.exception("med_recon: _was_actioned check failed")
        return False  # fail open → allow re-send rather than silently suppress


def _advance_watermark(db, cpmrn, encounter, snapshot_at, seen_keys, file_keys, extra: dict | None = None):
    """Advance last_recon_at and extend the reconciled-doc ledger."""
    updated = set(seen_keys)
    if file_keys:
        updated.update(file_keys)
    # Keep the ledger bounded (most recent 200 keys).
    ledger = list(updated)[-200:]
    fields = {"last_recon_at": snapshot_at, "reconciled_doc_keys": ledger}
    if extra:
        fields.update(extra)
    try:
        db[_SCHED].update_one({"CPMRN": cpmrn, "encounter": encounter}, {"$set": fields}, upsert=True)
    except Exception:
        logger.exception("med_recon: failed to advance watermark for %s enc=%d", cpmrn, encounter)
