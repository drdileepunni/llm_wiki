"""APScheduler singleton — hourly snapshot + live pipeline for all scheduled patients.

Per-patient pipeline (runs after each successful snapshot):
  1. Chart sync        — snapshot_collector.collect() → db.snapshots
  2. Note indexing     — FAISS index rebuilt from latest chart
  3. Delta extraction  — new vitals/labs/notes vs last_snapshot_at
  4. Rolling summary   — Gemini structured update → db.patient_contexts
  5. CDS (conditional) — runs only when any problem is worsening / critical

Only one process may hold the scheduler at a time. An exclusive fcntl file lock
is acquired on startup and held for the lifetime of the process. When the process
exits (cleanly or not), the OS releases the lock automatically, allowing the next
process to take over. This prevents duplicate schedulers under uvicorn --reload.
"""
import fcntl
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_WORSENING_STATUSES = {"worsening", "critical"}

_scheduler: BackgroundScheduler | None = None
_last_run_at: datetime | None = None
_last_run_results: list = []
_lock_fh = None   # open file handle — keeps the lock alive

_LOCK_PATH = Path("/tmp/llm_wiki_scheduler.lock")


def _acquire_lock() -> bool:
    """Try to acquire an exclusive non-blocking file lock. Returns True on success."""
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_PATH, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except OSError:
        if _lock_fh:
            _lock_fh.close()
            _lock_fh = None
        return False


def _release_lock():
    global _lock_fh
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
        except Exception:
            pass
        _lock_fh = None


def _run_live_pipeline(cpmrn: str, encounter: int, chart: dict, snapshot_at: datetime) -> dict:
    """
    Steps 2-5 of the per-patient pipeline, run synchronously after a fresh snapshot.
    Returns a status dict that gets merged into the scheduler result entry.
    """
    status: dict = {}

    # Step 2: note indexing (FAISS) — skipped when notes haven't changed
    try:
        from tools.radar_sync.notes_module.admission_loader import load_admission
        from tools.radar_sync.notes_module.mongo_cache import (
            compute_notes_hash, get_stored_notes_hash, save_index,
        )
        admission_id = f"{cpmrn}_{encounter}"
        current_hash = compute_notes_hash(chart)
        stored_hash  = get_stored_notes_hash(admission_id)
        if current_hash == stored_hash:
            status["note_index"] = "skipped_unchanged"
            logger.info("pipeline: note index unchanged for %s enc=%d — skipping embed", cpmrn, encounter)
        else:
            store = load_admission(chart)
            save_index(store, admission_id, notes_hash=current_hash)
            status["note_index"] = "ok"
            logger.info("pipeline: note index rebuilt for %s enc=%d", cpmrn, encounter)
    except Exception:
        logger.exception("pipeline: note indexing failed for %s enc=%d", cpmrn, encounter)
        status["note_index"] = "error"

    # Step 3: delta extraction
    try:
        from tools.radar_sync.patient_context import get_context
        from tools.radar_sync.delta_extractor import extract_delta
        ctx = get_context(cpmrn, encounter)
        last_ts = ctx.get("last_snapshot_at")
        if isinstance(last_ts, datetime) and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        delta = extract_delta(chart, last_ts)
        status["delta"] = {
            "new_vitals": len(delta.get("new_vitals", [])),
            "new_labs":   len(delta.get("new_labs", [])),
            "new_notes":  len(delta.get("new_notes", [])),
        }
        logger.info("pipeline: delta extracted for %s — %s", cpmrn, status["delta"])
    except Exception:
        logger.exception("pipeline: delta extraction failed for %s enc=%d", cpmrn, encounter)
        return status

    # Step 4: rolling summary update (Gemini)
    try:
        import importlib
        import tools.radar_sync.summary_updater as _su
        importlib.reload(_su)
        existing = ctx.get("structured_summary") or ctx.get("running_summary", "")
        is_first = not bool(existing)
        new_structured = _su.update_summary(
            existing, delta, cpmrn,
            chart=chart if is_first else None,
        )
        status["summary"] = "ok"
        logger.info("pipeline: summary updated for %s enc=%d", cpmrn, encounter)
    except Exception:
        logger.exception("pipeline: summary update failed for %s enc=%d", cpmrn, encounter)
        status["summary"] = "error"
        return status

    # Step 4b: status classification — reasoning model verifies worsening/critical labels
    try:
        from tools.radar_sync.status_classifier import classify_statuses
        new_structured = classify_statuses(cpmrn, encounter, new_structured)
        status["classifier"] = "ok"
        logger.info("pipeline: status classification done for %s enc=%d", cpmrn, encounter)
    except Exception:
        logger.exception("pipeline: status classification failed for %s enc=%d", cpmrn, encounter)
        status["classifier"] = "error"
        # non-fatal — continue with draft statuses

    # Persist updated context
    try:
        from tools.radar_sync.patient_context import append_entry, update_summary as _ctx_update_summary
        narrative = (
            new_structured.get("narrative", "")
            if isinstance(new_structured, dict)
            else str(new_structured)
        )
        _ctx_update_summary(cpmrn, encounter, narrative, snapshot_at)

        # Store structured summary alongside narrative
        from backend.services.emr.db import get_db as _get_db
        _get_db()["patient_contexts"].update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"structured_summary": new_structured}},
        )
    except Exception:
        logger.exception("pipeline: context save failed for %s enc=%d", cpmrn, encounter)

    # Step 5: CDS — only if any problem is worsening or critical
    problems = new_structured.get("problems", []) if isinstance(new_structured, dict) else []
    triggering = [p["name"] for p in problems if p.get("status") in _WORSENING_STATUSES]

    if triggering:
        logger.info(
            "pipeline: CDS triggered for %s enc=%d — worsening/critical: %s",
            cpmrn, encounter, triggering,
        )
        try:
            from tools.radar_sync.cds_runner import run_cds
            io_summary = delta.get("io_last_24h", {})
            cds_result = run_cds(cpmrn, encounter, new_structured, chart, io_summary)

            # Append to hourly_entries so the frontend can surface it
            from tools.radar_sync.patient_context import append_entry
            append_entry(cpmrn, encounter, {
                "snapshot_at":    snapshot_at,
                "triggering_problems": triggering,
                "structured_summary": new_structured,
                "suggestions":    cds_result,
            })
            status["cds"] = {"triggered": True, "problems": triggering}
            logger.info("pipeline: CDS complete for %s enc=%d", cpmrn, encounter)

            # Google Chat alert — only if webhook is configured and enabled
            try:
                from backend.services.emr.db import get_db as _get_db
                from tools.radar_sync.gchat_notifier import send_gchat_alert
                cfg = _get_db()["app_settings"].find_one({"_id": "gchat_webhook"})
                if cfg and cfg.get("enabled") and cfg.get("url"):
                    sent = send_gchat_alert(
                        cpmrn, encounter, triggering,
                        new_structured, cds_result,
                        cfg["url"],
                    )
                    status["cds"]["gchat_alert"] = "sent" if sent else "failed"
            except Exception:
                logger.exception("pipeline: gchat alert failed for %s enc=%d", cpmrn, encounter)
                status["cds"]["gchat_alert"] = "error"
        except Exception:
            logger.exception("pipeline: CDS failed for %s enc=%d", cpmrn, encounter)
            status["cds"] = {"triggered": True, "problems": triggering, "error": True}
    else:
        logger.info(
            "pipeline: CDS skipped for %s enc=%d — no worsening/critical problems (statuses: %s)",
            cpmrn, encounter,
            [p.get("status") for p in problems],
        )
        status["cds"] = {"triggered": False}

    return status


def _collect_all():
    global _last_run_at, _last_run_results
    _last_run_at = datetime.now(timezone.utc)
    _last_run_results = []

    try:
        from backend.services.emr.db import get_db
        from tools.radar_sync.snapshot_collector import collect
        from tools.radar_sync.chart_puller import get_admitted_patients

        db = get_db()
        patients = list(db.snapshot_schedule.find({"active": True}))
        logger.info("scheduler: collecting %d scheduled patient(s)", len(patients))

        # Build admitted-patient sets per workspace (fetch once per unique workspace)
        admitted_by_workspace: dict[str, set] = {}
        for p in patients:
            ws = p.get("workspace")
            if ws and ws not in admitted_by_workspace:
                try:
                    admitted = get_admitted_patients(ws)
                    admitted_by_workspace[ws] = {
                        (a["CPMRN"], a["encounter"]) for a in admitted
                    }
                    logger.info("scheduler: workspace %s has %d admitted patient(s)", ws, len(admitted))
                except Exception:
                    logger.exception("scheduler: could not fetch admitted patients for workspace %s", ws)
                    admitted_by_workspace[ws] = None  # None = couldn't check; don't skip

        for p in patients:
            cpmrn     = p["CPMRN"]
            encounter = p.get("encounter", 1)
            workspace = p.get("workspace")
            try:
                # Admission check — skip and auto-deactivate discharged patients
                if workspace and admitted_by_workspace.get(workspace) is not None:
                    if (cpmrn, encounter) not in admitted_by_workspace[workspace]:
                        logger.info(
                            "scheduler: %s enc=%d not in admitted list for workspace %s — deactivating",
                            cpmrn, encounter, workspace,
                        )
                        db.snapshot_schedule.update_one(
                            {"CPMRN": cpmrn, "encounter": encounter},
                            {"$set": {"active": False, "deactivated_reason": "discharged"}},
                        )
                        _last_run_results.append({"cpmrn": cpmrn, "status": "skipped_discharged"})
                        continue

                # Idempotency guard — skip if a snapshot already exists within last 50 min
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=50)
                recent = db.snapshots.find_one(
                    {"CPMRN": cpmrn, "encounter": encounter, "snapshot_at": {"$gte": cutoff}}
                )
                if recent:
                    logger.info("scheduler: skipping %s enc=%d — snapshot exists within 50 min", cpmrn, encounter)
                    _last_run_results.append({"cpmrn": cpmrn, "status": "skipped_duplicate"})
                    continue

                result = collect(cpmrn=cpmrn, encounter=encounter)
                db.snapshot_schedule.update_one(
                    {"CPMRN": cpmrn, "encounter": encounter},
                    {"$set": {"last_collected_at": datetime.now(timezone.utc), "last_error": None}},
                )
                logger.info("scheduler: collected %s enc=%d — running live pipeline", cpmrn, encounter)

                # Fetch the stored snapshot chart for the pipeline
                snap_doc = db.snapshots.find_one(
                    {"CPMRN": cpmrn, "encounter": encounter},
                    sort=[("snapshot_at", -1)],
                )
                if snap_doc:
                    snap_ts = snap_doc["snapshot_at"]
                    if isinstance(snap_ts, datetime) and snap_ts.tzinfo is None:
                        snap_ts = snap_ts.replace(tzinfo=timezone.utc)
                    pipeline_status = _run_live_pipeline(
                        cpmrn, encounter, snap_doc["chart"], snap_ts
                    )
                else:
                    pipeline_status = {"error": "snapshot_not_found"}

                _last_run_results.append({
                    "cpmrn": cpmrn, "status": "ok",
                    **result,
                    "pipeline": pipeline_status,
                })
                logger.info("scheduler: pipeline done for %s enc=%d — %s", cpmrn, encounter, pipeline_status)
            except Exception as e:
                db.snapshot_schedule.update_one(
                    {"CPMRN": cpmrn, "encounter": encounter},
                    {"$set": {"last_error": str(e)}},
                )
                _last_run_results.append({"cpmrn": cpmrn, "status": "error", "error": str(e)})
                logger.exception("scheduler: collect failed for %s", cpmrn)
    except Exception:
        logger.exception("scheduler: _collect_all crashed")


def start_scheduler():
    global _scheduler

    # Retry acquiring the lock — the old uvicorn worker may still be alive for a
    # few seconds during a reload. We try up to 5 times with 1-second gaps.
    import threading
    def _try_start():
        for attempt in range(5):
            if _acquire_lock():
                _boot_scheduler()
                return
            hold = _LOCK_PATH.read_text().strip() if _LOCK_PATH.exists() else "?"
            logger.info("Scheduler lock held by PID %s — retrying in 1 s (attempt %d/5)", hold, attempt + 1)
            import time; time.sleep(1)
        logger.warning("Could not acquire scheduler lock after 5 attempts — no scheduler in this worker")

    threading.Thread(target=_try_start, daemon=True, name="scheduler-init").start()


def _boot_scheduler():
    global _scheduler

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _collect_all,
        trigger=CronTrigger(minute=0),
        id="hourly_snapshot",
        name="Hourly chart snapshot collection",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("APScheduler started (PID %d) — hourly snapshot collection active", os.getpid())


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
    _release_lock()


def get_scheduler_status() -> dict:
    if not _scheduler or not _scheduler.running:
        return {"running": False, "next_run": None, "last_run": None, "last_results": []}

    job = _scheduler.get_job("hourly_snapshot")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None

    return {
        "running":      True,
        "paused":       _scheduler.state == 2,  # STATE_PAUSED = 2
        "next_run":     next_run,
        "last_run":     _last_run_at.isoformat() if _last_run_at else None,
        "last_results": _last_run_results,
    }
