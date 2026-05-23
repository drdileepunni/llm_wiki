"""APScheduler singleton — hourly snapshot + live pipeline for all scheduled patients.

Per-patient pipeline (runs after each successful snapshot):
  1. Chart sync        — snapshot_collector.collect() → db.snapshots
  2. Note indexing     — FAISS index rebuilt from latest chart (skipped if notes unchanged)
  3. Delta extraction  — new vitals/labs/notes vs last_snapshot_at
  4. Rolling summary   — Gemini structured update → db.patient_contexts
  4b. Status classifier — reasoning model verifies worsening/critical labels
  5. Problem tracker   — persistent problem list, targeted alerts, next_check lifecycle

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
        from tools.radar_sync.patient_context import update_summary as _ctx_update_summary
        narrative = (
            new_structured.get("narrative", "")
            if isinstance(new_structured, dict)
            else str(new_structured)
        )
        _ctx_update_summary(cpmrn, encounter, narrative, snapshot_at)

        from backend.services.emr.db import get_db as _get_db
        _get_db()["patient_contexts"].update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"structured_summary": new_structured}},
        )
    except Exception:
        logger.exception("pipeline: context save failed for %s enc=%d", cpmrn, encounter)

    # Step 5: problem tracker — manages persistent problem list + targeted alerts
    try:
        from tools.radar_sync.problem_tracker import track_problems
        tracker_result = track_problems(cpmrn, encounter, new_structured, snapshot_at)
        status["problem_tracker"] = tracker_result
        logger.info("pipeline: problem tracker done for %s enc=%d — %s", cpmrn, encounter, tracker_result)
    except Exception:
        logger.exception("pipeline: problem tracker failed for %s enc=%d", cpmrn, encounter)
        status["problem_tracker"] = {"error": "exception"}

    # Step 6: false-negative detector — safety net for missed deteriorations
    try:
        from tools.radar_sync.fn_detector import run_fn_detector
        from backend.services.emr.db import get_db as _get_db2
        fn_result = run_fn_detector(cpmrn, encounter, new_structured, snapshot_at, _get_db2())
        status["fn_detector"] = fn_result
        logger.info("pipeline: fn_detector done for %s enc=%d — %s", cpmrn, encounter, fn_result)
    except Exception:
        logger.exception("pipeline: fn_detector failed for %s enc=%d", cpmrn, encounter)
        status["fn_detector"] = {"error": "exception"}

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

        # ── Step 0: auto-enroll new admissions ────────────────────────────────
        # Derive monitored workspaces from app_settings (primary) and from any
        # workspace already present in snapshot_schedule (backward compat).
        ws_config = db["app_settings"].find_one({"_id": "monitored_workspaces"})
        configured_ws: list[str] = (ws_config or {}).get("workspaces", [])
        scheduled_ws: list[str] = db.snapshot_schedule.distinct(
            "workspace", {"workspace": {"$exists": True, "$ne": None}}
        )
        all_workspaces: list[str] = list(dict.fromkeys(configured_ws + scheduled_ws))  # ordered, deduped

        # Fetch admitted patients for every monitored workspace.
        # admitted_by_workspace[ws] = set of (CPMRN, encounter) tuples, or None on error.
        admitted_by_workspace: dict[str, set | None] = {}
        for ws in all_workspaces:
            try:
                admitted = get_admitted_patients(ws)
                admitted_by_workspace[ws] = {(a["CPMRN"], a["encounter"]) for a in admitted}
                logger.info("scheduler: workspace %s — %d admitted patient(s)", ws, len(admitted))

                # Enroll any patient not already active in snapshot_schedule
                enrolled = 0
                for patient in admitted:
                    cpmrn    = patient["CPMRN"]
                    encounter = patient["encounter"]
                    existing = db.snapshot_schedule.find_one(
                        {"CPMRN": cpmrn, "encounter": encounter}
                    )
                    if existing is None:
                        db.snapshot_schedule.insert_one({
                            "CPMRN":             cpmrn,
                            "encounter":         encounter,
                            "workspace":         ws,
                            "active":            True,
                            "added_at":          datetime.now(timezone.utc),
                            "last_collected_at": None,
                            "last_error":        None,
                        })
                        enrolled += 1
                        logger.info(
                            "scheduler: auto-enrolled %s enc=%d from workspace %s",
                            cpmrn, encounter, ws,
                        )
                if enrolled:
                    logger.info("scheduler: workspace %s — enrolled %d new patient(s)", ws, enrolled)

            except Exception:
                logger.exception("scheduler: could not fetch admitted patients for workspace %s", ws)
                admitted_by_workspace[ws] = None  # None = couldn't check; don't skip or deactivate

        # Reload patient list — now includes freshly enrolled patients
        patients = list(db.snapshot_schedule.find({"active": True}))
        logger.info("scheduler: collecting %d active patient(s)", len(patients))

        for p in patients:
            cpmrn     = p["CPMRN"]
            encounter = p.get("encounter", 1)
            workspace = p.get("workspace")
            try:
                # ── Discharge check — deactivate patients no longer admitted ──
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
        # ── Study pipeline — runs after all patients are processed ───────────
        try:
            from tools.radar_sync.study_runner import run_study_jobs
            study_result = run_study_jobs(db)
            _last_run_results.append({"step": "study_jobs", **study_result})
            logger.info("scheduler: study jobs done — %s", study_result)
        except Exception:
            logger.exception("scheduler: study jobs failed")
            _last_run_results.append({"step": "study_jobs", "error": "exception"})

        # ── Cost tracker — aggregate LLM token costs for this run ────────────
        try:
            from tools.radar_sync.study_cost_tracker import compute_run_cost
            cost_doc = compute_run_cost(db, _last_run_at)
            totals = cost_doc.get("totals", {})
            _last_run_results.append({
                "step":          "cost_summary",
                "patient_count": cost_doc.get("patient_count", 0),
                "trace_count":   cost_doc.get("trace_count", 0),
                "total_cost_usd": totals.get("cost_usd", 0),
                "total_input_tokens":    totals.get("input_tokens", 0),
                "total_output_tokens":   totals.get("output_tokens", 0),
                "total_thinking_tokens": totals.get("thinking_tokens", 0),
            })
            logger.info("scheduler: cost summary — $%.4f USD", totals.get("cost_usd", 0))
        except Exception:
            logger.exception("scheduler: cost tracker failed")
            _last_run_results.append({"step": "cost_summary", "error": "exception"})

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
