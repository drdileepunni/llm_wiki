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
from typing import Any
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def _coerce_dt(v) -> "datetime | None":
    """Coerce None / ISO-string / datetime → tz-aware datetime (UTC). Returns None if unparseable."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


_scheduler: BackgroundScheduler | None = None
_last_run_at: datetime | None = None
_last_run_results: list = []

# Hard cap on med-recon cards sent per pipeline cycle (trial period — keeps costs bounded
# while we calibrate the feature across a 40-45 patient workspace).
_RECON_CARD_LIMIT = 5
# Hard cap on report-interpret cards sent per pipeline cycle. Bounds card volume only —
# when reached we still interpret + audit (send=False) so nothing is silently dropped.
_REPORT_CARD_LIMIT = 5
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


def _run_live_pipeline(
    cpmrn: str,
    encounter: int,
    chart: dict,
    snapshot_at: datetime,
    db=None,
) -> dict:
    """
    Steps 2-6 of the per-patient pipeline, run synchronously after a fresh snapshot.

    Cost-reduction gates (evaluated in order):
      1. Cadence gate  — skip LLM if next_run_at is not yet due; bypassed when new reports present
      2. Delta gate    — skip LLM if no new vitals/labs/notes/reports since last_llm_run_at
      3. Pass 1 screener — cheap call that decides if Pass 2 is needed
      4. Pass 2        — status_classifier + problem_tracker (only when Pass 1 flags)

    Step 3b/3c (report selection + interpretation) run before the summary update so that
    new diagnostic-report findings are injected into the delta and flow through the normal
    summary → problem tracker path in the same cycle (Phase 2 same-cycle integration).

    Stashes _report_analysis and _report_sel on the returned status dict for
    _run_report_interpret_step to consume in _collect_all.

    fn_detector always runs at the end (zero LLM cost, safety net).

    Returns a status dict that gets merged into the scheduler result entry.
    """
    from backend.services.emr.db import get_db as _get_db
    if db is None:
        db = _get_db()

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

    now = datetime.now(timezone.utc)

    # Read schedule doc early — needed for delta cutoff and both gates
    sched_doc = db.snapshot_schedule.find_one({"CPMRN": cpmrn, "encounter": encounter}) or {}

    # Step 3: delta extraction (always — free, no LLM)
    # Cutoff: use last_llm_run_at when available so the delta captures ALL data since
    # the last analysis (which may be 1/2/4h ago), not just since the last hourly snapshot.
    try:
        from tools.radar_sync.patient_context import get_context
        from tools.radar_sync.delta_extractor import extract_delta
        ctx = get_context(cpmrn, encounter)

        # Prefer last_llm_run_at over last_snapshot_at for delta cutoff
        last_llm_run_at_raw = sched_doc.get("last_llm_run_at")
        last_snapshot_ts    = ctx.get("last_snapshot_at")
        # GCS stores datetimes as ISO strings — coerce both with _coerce_dt
        last_ts = _coerce_dt(last_llm_run_at_raw) or _coerce_dt(last_snapshot_ts)

        prev_io = sched_doc.get("last_io_aggregate")
        delta = extract_delta(chart, last_ts, prev_io_aggregate=prev_io)
        status["delta"] = {
            "new_vitals": len(delta.get("new_vitals", [])),
            "new_labs":   len(delta.get("new_labs", [])),
            "new_notes":  len(delta.get("new_notes", [])),
            "io_changed": delta.get("io_changed", False),
        }
        logger.info("pipeline: delta extracted for %s — %s (cutoff: %s)",
                    cpmrn, status["delta"],
                    last_ts.strftime("%Y-%m-%d %H:%M UTC") if last_ts else "none")
    except Exception:
        logger.exception("pipeline: delta extraction failed for %s enc=%d", cpmrn, encounter)
        return status

    # Step 3b: Report selection (cheap, no LLM) — before gates so new reports can bypass them
    _report_sel: dict = {}
    _new_reports: list = []
    _has_new_reports = False
    try:
        from tools.radar_sync.report_interpret.orchestrator import select_for_cycle
        _report_sel = select_for_cycle(cpmrn, encounter, chart, db)
        _new_reports = _report_sel.get("new_reports") or []
        _has_new_reports = bool(_new_reports) and _report_sel.get("status") == "ok"
        status["report_select"] = {"n": len(_new_reports), "status": _report_sel.get("status")}
        logger.info(
            "pipeline: report select for %s enc=%d — %d new report(s) [%s]",
            cpmrn, encounter, len(_new_reports), _report_sel.get("status"),
        )
    except Exception:
        logger.exception("pipeline: report selection failed for %s enc=%d", cpmrn, encounter)

    # ── Prelude: check for overdue per-problem follow-ups ────────────────────
    # Must happen before any gate so the force flag is available to all of them.
    try:
        from tools.radar_sync.problem_tracker import overdue_next_checks
        _overdue_all = overdue_next_checks(cpmrn, encounter, db, now)
        # Vital-type next_checks recheck every 1h and are covered by the normal
        # Pass-1 screener each cycle. Only non-vital (lab, io) overdue checks
        # force an expensive run — they have longer intervals and may be missed.
        _overdue = [o for o in _overdue_all if o.get("type") == "lab"]
    except Exception:
        logger.exception("pipeline: overdue_next_checks failed for %s enc=%d — treating as empty", cpmrn, encounter)
        _overdue = []
    _overdue_glucose = [o for o in _overdue if _is_glucose_next_check(o)]
    _overdue_generic  = [o for o in _overdue if not _is_glucose_next_check(o)]
    # Only non-glucose overdue checks force the full expensive run.
    # Glucose-only overdue checks route to the cheap glucose pathway instead.
    force_expensive     = bool(_overdue_generic)
    force_glucose_check = bool(_overdue_glucose) and not force_expensive
    if force_expensive:
        logger.info(
            "pipeline: %d overdue generic lab next_check(s) for %s enc=%d — will force expensive run",
            len(_overdue_generic), cpmrn, encounter,
        )
    if force_glucose_check:
        logger.info(
            "pipeline: overdue glucose next_check for %s enc=%d — will route to cheap glucose pathway",
            cpmrn, encounter,
        )
    status["force_expensive"]     = force_expensive
    status["force_glucose_check"] = force_glucose_check

    # Build a human-readable scheduling trigger label for the audit table.
    _trigger_parts = []
    if force_expensive:
        _generic_labels = [
            f"{o.get('label') or o.get('key', '?')} ({o.get('problem_name', '?')})"
            for o in _overdue_generic
        ]
        _trigger_parts.append("overdue: " + ", ".join(_generic_labels))
    if force_glucose_check:
        _glucose_labels = [
            f"{o.get('label') or o.get('key', '?')} ({o.get('problem_name', '?')})"
            for o in _overdue_glucose
        ]
        _trigger_parts.append("overdue glucose: " + ", ".join(_glucose_labels))
    status["trigger_reason"] = " + ".join(_trigger_parts) if _trigger_parts else ""

    # ── Gate 0: Empty chart — skip all LLM if chart has no data whatsoever ────
    # A brand-new patient with no vitals, labs, or notes cannot be assessed.
    # Sending an empty chart to pass1/pass2 wastes tokens and always returns
    # "indeterminate" — hardcode skip instead and retry next cycle.
    _has_any_vitals = bool(chart.get("vitals"))
    _has_any_labs   = any(d.get("category") == "labs" for d in (chart.get("documents") or []))
    _has_any_notes  = bool((chart.get("notes") or {}).get("finalNotes"))
    if not _has_any_vitals and not _has_any_labs and not _has_any_notes and not _has_new_reports:
        logger.info(
            "pipeline: empty-chart gate — no vitals, labs, notes or reports for %s enc=%d, skipping LLM",
            cpmrn, encounter,
        )
        status["pass1"] = "skipped_empty_chart"
        status["pass2"] = "skipped_empty_chart"
        db.snapshot_schedule.update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"next_run_at": _next_run_at(snapshot_at, 1)}},
        )
        return status

    # ── Gate 1: Adaptive cadence — skip LLM pipeline if not yet due ──────────
    next_run_at = _coerce_dt(sched_doc.get("next_run_at"))
    if next_run_at is not None and now < next_run_at:
        if not _has_new_reports and not force_expensive and not force_glucose_check:
            logger.info(
                "pipeline: cadence gate — skipping LLM for %s enc=%d (next_run_at %s)",
                cpmrn, encounter, next_run_at.strftime("%H:%M UTC"),
            )
            status["cadence_gate"] = "skipped"
            status["_report_analysis"] = None
            status["_report_sel"] = _report_sel
            new_structured = ctx.get("structured_summary") or {}
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status
        if _has_new_reports:
            logger.info(
                "pipeline: cadence gate bypassed for %s enc=%d — %d new report(s)",
                cpmrn, encounter, len(_new_reports),
            )
        elif force_expensive:
            logger.info(
                "pipeline: cadence gate bypassed for %s enc=%d — forced by overdue next_check",
                cpmrn, encounter,
            )
        elif force_glucose_check:
            logger.info(
                "pipeline: cadence gate bypassed for %s enc=%d — overdue glucose next_check",
                cpmrn, encounter,
            )

    # ── Gate 2: Delta gate — skip LLM if nothing new since last analysis ─────
    last_llm_run_at = _coerce_dt(last_llm_run_at_raw)
    if last_llm_run_at is not None:
        has_new_data = any([
            delta.get("new_vitals"), delta.get("new_labs"), delta.get("new_notes"),
            delta.get("io_changed"),
        ])
        if not has_new_data and not _has_new_reports:
            if force_expensive or force_glucose_check:
                logger.info(
                    "pipeline: delta gate bypassed for %s enc=%d — forced by overdue next_check (no new data)",
                    cpmrn, encounter,
                )
                # Fall through to forced Pass-2 below — skip the normal LLM chain
                # by jumping directly past summary/pass-1
                status["delta_gate"] = "bypassed_forced"
            else:
                logger.info(
                    "pipeline: delta gate — no new data for %s enc=%d since last LLM run, skipping",
                    cpmrn, encounter,
                )
                status["delta_gate"] = "skipped"
                status["_report_analysis"] = None
                status["_report_sel"] = _report_sel
                db.snapshot_schedule.update_one(
                    {"CPMRN": cpmrn, "encounter": encounter},
                    {"$set": {"next_run_at": _next_run_at(snapshot_at, 1)}},
                )
                new_structured = ctx.get("structured_summary") or {}
                _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
                return status

    # ── Gate 2.5: Vital-normal gate — skip when vitals-only and all normal;
    #             force full when vitals-only and any abnormal.
    # Only fires when: no forced recheck, no new reports, delta is vitals-only.
    _gate_new_vitals = delta.get("new_vitals") or []
    _gate_new_labs   = delta.get("new_labs") or []
    _gate_new_notes  = delta.get("new_notes") or []
    _gate_new_rf     = delta.get("new_report_findings") or []
    force_full_vitals = False
    if (
        not force_expensive
        and not _has_new_reports
        and not status.get("delta_gate") == "bypassed_forced"
    ):
        vitals_only = bool(_gate_new_vitals) and not _gate_new_labs and not _gate_new_notes and not _gate_new_rf
        if vitals_only:
            try:
                from tools.radar_sync.fn_detector import all_new_vitals_normal
                if all_new_vitals_normal(_gate_new_vitals):
                    logger.info(
                        "pipeline: vital-normal gate — all %d new vital(s) normal for %s enc=%d, skipping LLM",
                        len(_gate_new_vitals), cpmrn, encounter,
                    )
                    status["vital_normal_gate"] = "skipped"
                    status["_report_analysis"] = None
                    status["_report_sel"] = _report_sel
                    db.snapshot_schedule.update_one(
                        {"CPMRN": cpmrn, "encounter": encounter},
                        {"$set": {"next_run_at": _next_run_at(snapshot_at, 1)}},
                    )
                    new_structured = ctx.get("structured_summary") or {}
                    _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
                    return status
                else:
                    logger.info(
                        "pipeline: vital-normal gate — abnormal vital(s) for %s enc=%d, forcing full analysis",
                        cpmrn, encounter,
                    )
                    force_full_vitals = True
                    status["vital_normal_gate"] = "triggered"
            except Exception:
                logger.exception("pipeline: vital-normal gate check failed for %s enc=%d — continuing", cpmrn, encounter)

    # ── Gate 2.6: Glucose-only gate — bypass full pipeline for standalone glucose labs ─
    # Fires when: delta has only new glucose-named lab docs, no vitals/notes/report_findings,
    # no forced recheck, no new reports. Routes directly to insulin_advice → alert card,
    # skipping summary update, Pass 1, status_classifier, and problem_tracker entirely.
    if (
        not force_expensive
        and not _has_new_reports
        and not status.get("delta_gate") == "bypassed_forced"
    ):
        _gl_new_vitals = delta.get("new_vitals") or []
        _gl_new_labs   = delta.get("new_labs") or []
        _gl_new_notes  = delta.get("new_notes") or []
        _gl_new_rf     = delta.get("new_report_findings") or []
        _glucose_only  = (
            bool(_gl_new_labs)
            and not _gl_new_vitals
            and not _gl_new_notes
            and not _gl_new_rf
            and all(_is_glucose_lab(d) for d in _gl_new_labs)
        )
        if _glucose_only:
            if _run_glucose_alert(cpmrn, encounter, db, snapshot_at, ctx, delta, status):
                _run_fn_detector_step(cpmrn, encounter, ctx.get("structured_summary") or {}, snapshot_at, db, status)
                return status

    # ── Gate 2.7: ABG gate — deterministic threshold check ───────────────────
    # Any new ABG with a value crossing a danger threshold → force full analysis.
    # ABG with no threshold breach → no action (notes screener may still fire).
    force_full_abg = False
    if not force_expensive and not status.get("delta_gate") == "bypassed_forced":
        _abg_labs = [lab for lab in _gate_new_labs if _is_abg_lab(lab)]
        if _abg_labs:
            if any(_check_abg_thresholds(lab) for lab in _abg_labs):
                logger.info(
                    "pipeline: ABG gate — threshold crossed for %s enc=%d, forcing full analysis",
                    cpmrn, encounter,
                )
                force_full_abg = True
                status["abg_gate"] = "triggered"
            else:
                logger.info(
                    "pipeline: ABG gate — ABG present, no threshold crossed for %s enc=%d",
                    cpmrn, encounter,
                )
                status["abg_gate"] = "no_threshold"

    # ── Gate 2.8: Non-ABG, non-glucose lab gate ───────────────────────────────
    # Any new lab that is neither an ABG panel nor a glucose test → always full.
    force_full_other_lab = False
    if not force_expensive and not status.get("delta_gate") == "bypassed_forced":
        _other_labs = [
            lab for lab in _gate_new_labs
            if not _is_abg_lab(lab) and not _is_glucose_lab(lab)
        ]
        if _other_labs:
            _other_lab_names = [lab.get("name", "?") for lab in _other_labs]
            logger.info(
                "pipeline: other-lab gate — new lab(s) %s for %s enc=%d, forcing full analysis",
                _other_lab_names, cpmrn, encounter,
            )
            force_full_other_lab = True
            status["other_lab_gate"] = "triggered: " + ", ".join(_other_lab_names)

    # ── Forced-recheck shortcut (no new data) ────────────────────────────────
    # When the delta gate was bypassed by a forced follow-up but there is genuinely
    # no new data, skip summary+pass-1 and run problem_tracker directly using the
    # last stored summary. The forced_block inside track_problems tells the model
    # what to focus on.
    if status.get("delta_gate") == "bypassed_forced":
        # Glucose-only overdue check with no new data → cheap glucose pathway, no LLM.
        if force_glucose_check and not force_expensive:
            status["pass1"] = "skipped_glucose_only"
            status["pass2"] = "skipped_glucose_only"
            if _run_glucose_alert(cpmrn, encounter, db, snapshot_at, ctx, delta, status):
                _run_fn_detector_step(cpmrn, encounter, ctx.get("structured_summary") or {}, snapshot_at, db, status)
                return status
            # No glucose value retrievable — fall through to full tracker as fallback.
            logger.warning(
                "pipeline: glucose-forced recheck — no glucose value for %s enc=%d, falling back to full tracker",
                cpmrn, encounter,
            )

        new_structured = ctx.get("structured_summary") or {}
        status["summary"] = "skipped_forced_no_data"
        status["pass1"]   = "skipped_forced_no_data"
        status["pass2"]   = "forced_by_overdue_next_check"
        status["classifier"] = "skipped_forced_only"
        try:
            from tools.radar_sync.problem_tracker import track_problems
            tracker_result = track_problems(
                cpmrn, encounter, new_structured, snapshot_at,
                screener_flag="",
                focus=_overdue,
                delta=delta,
            )
            status["problem_tracker"] = tracker_result
            logger.info(
                "pipeline: forced recheck (no new data) — problem tracker done for %s enc=%d",
                cpmrn, encounter,
            )
        except Exception:
            logger.exception("pipeline: forced recheck problem tracker failed for %s enc=%d", cpmrn, encounter)
            status["problem_tracker"] = {"error": "exception"}
        _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
        return status

    # Step 3c: Interpret new reports (if any) — inject findings into delta before summary update
    if _has_new_reports:
        try:
            from tools.radar_sync.report_interpret.orchestrator import analyze_new_reports
            _existing = ctx.get("structured_summary") or {}
            _pt_narrative = (
                _existing.get("narrative") or ctx.get("running_summary") or ""
            ).strip()
            _analysis = analyze_new_reports(cpmrn, encounter, _new_reports, _pt_narrative, db)
            if _analysis:
                delta["new_report_findings"] = [
                    f for r in _analysis.get("reports", []) for f in (r.get("findings") or [])
                ]
                status["_report_analysis"] = _analysis
                logger.info(
                    "pipeline: report interpret for %s enc=%d — %d finding(s) injected into delta",
                    cpmrn, encounter, len(delta["new_report_findings"]),
                )
            else:
                status["_report_analysis"] = None
                logger.warning(
                    "pipeline: report interpret returned None for %s enc=%d", cpmrn, encounter,
                )
        except Exception:
            logger.exception("pipeline: report interpretation failed for %s enc=%d", cpmrn, encounter)
            status["_report_analysis"] = None
    else:
        status["_report_analysis"] = None

    status["_report_sel"] = _report_sel

    # ── Step 4: rolling summary update (gemini-3.1-flash-lite, no thinking) ───
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

    # ── Gate 3: Pass 1 screener (notes-only) — decide if full Pass 2 is needed ─
    # Runs ONLY when there are new notes and no upstream lab/vital gate already
    # forced full analysis. Lab and vital decisions are handled deterministically
    # by Gates 2.5–2.8; the screener's role is solely to evaluate new note content.
    _force_full_upstream = force_full_vitals or force_full_abg or force_full_other_lab

    last_problems = list(db["patient_problems"].find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"problem_name": 1, "clinical_status": 1,
         "last_assessed_at": 1, "reasoning_fingerprint": 1},
    ))
    pass1 = None
    if _force_full_upstream:
        # Lab/vital gate already decided — skip screener LLM call entirely.
        logger.info(
            "pipeline: pass1 skipped for %s enc=%d — upstream gate forced full analysis",
            cpmrn, encounter,
        )
        status["pass1"] = "skipped_upstream_gate"
    elif not _gate_new_notes:
        # No new notes — nothing for the screener to evaluate.
        logger.info(
            "pipeline: pass1 skipped for %s enc=%d — no new notes",
            cpmrn, encounter,
        )
        status["pass1"] = "skipped_no_new_notes"
        if not force_expensive:
            status["pass2"] = "skipped_by_pass1"
            db.snapshot_schedule.update_one(
                {"CPMRN": cpmrn, "encounter": encounter},
                {"$set": {
                    "last_llm_run_at":    snapshot_at,
                    "next_run_at":        _next_run_at(snapshot_at, 1),
                    "last_io_aggregate":  delta.get("io_last_24h"),
                    "last_delta_content": _compact_delta(delta),
                }},
            )
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status
    else:
        # New notes present — run note screener.
        try:
            from tools.radar_sync.pass1_screener import screen_patient
            pass1 = screen_patient(cpmrn, encounter, delta, last_problems, db)
            status["pass1"] = {
                "needs_full_analysis": pass1.needs_full_analysis,
                "flag_reason":         pass1.flag_reason,
            }

            db["patient_contexts"].update_one(
                {"CPMRN": cpmrn, "encounter": encounter},
                {"$set": {"lightweight_summary": pass1.lightweight_summary}},
            )

            db.snapshot_schedule.update_one(
                {"CPMRN": cpmrn, "encounter": encounter},
                {"$set": {
                    "last_llm_run_at":    snapshot_at,
                    "next_run_at":        _next_run_at(snapshot_at, 1),
                    "last_io_aggregate":  delta.get("io_last_24h"),
                    "last_delta_content": _compact_delta(delta),
                }},
            )

            logger.info(
                "pipeline: pass1 done for %s enc=%d — needs_full=%s reason=%s",
                cpmrn, encounter, pass1.needs_full_analysis, pass1.flag_reason or "(none)",
            )
        except Exception:
            logger.exception("pipeline: pass1 screener failed for %s enc=%d — running full Pass 2", cpmrn, encounter)
            pass1 = None

    # If note screener said not needed (and didn't fail), skip Pass 2 —
    # unless force_expensive overrides.
    if pass1 is not None and not pass1.needs_full_analysis:
        if force_expensive:
            logger.info(
                "pipeline: pass1 gate overridden for %s enc=%d — forced by %d overdue next_check(s)",
                cpmrn, encounter, len(_overdue),
            )
            status["pass2"] = "forced_by_overdue_next_check"
        else:
            logger.info("pipeline: pass1 gate — skipping Pass 2 for %s enc=%d", cpmrn, encounter)
            status["pass2"] = "skipped_by_pass1"
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status

    # ── Step 4b: status classifier — only when pass-1 flagged (not forced-only) ─
    # A single-parameter forced recheck is driven by problem_tracker's live tool
    # fetches; re-running the classifier over the whole summary is unnecessary cost.
    if _force_full_upstream or pass1 is None or (pass1 is not None and pass1.needs_full_analysis):
        try:
            from tools.radar_sync.status_classifier import classify_statuses
            new_structured = classify_statuses(cpmrn, encounter, new_structured, delta=delta)
            status["classifier"] = "ok"
            logger.info("pipeline: status classification done for %s enc=%d", cpmrn, encounter)
        except Exception:
            logger.exception("pipeline: status classification failed for %s enc=%d", cpmrn, encounter)
            status["classifier"] = "error"
            # non-fatal — continue with draft statuses
    else:
        status["classifier"] = "skipped_forced_only"

    # Persist updated context
    try:
        from tools.radar_sync.patient_context import update_summary as _ctx_update_summary
        narrative = (
            new_structured.get("narrative", "")
            if isinstance(new_structured, dict)
            else str(new_structured)
        )
        _ctx_update_summary(cpmrn, encounter, narrative, snapshot_at)
        db["patient_contexts"].update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"structured_summary": new_structured}},
        )
    except Exception:
        logger.exception("pipeline: context save failed for %s enc=%d", cpmrn, encounter)

    # Step 5: problem tracker — manages persistent problem list + targeted alerts
    try:
        from tools.radar_sync.problem_tracker import track_problems
        screener_flag = pass1.flag_reason if pass1 is not None else ""
        tracker_result = track_problems(
            cpmrn, encounter, new_structured, snapshot_at,
            screener_flag=screener_flag,
            focus=_overdue if _overdue else None,
            delta=delta,
        )
        status["problem_tracker"] = tracker_result
        logger.info("pipeline: problem tracker done for %s enc=%d — %s", cpmrn, encounter, tracker_result)
    except Exception:
        logger.exception("pipeline: problem tracker failed for %s enc=%d", cpmrn, encounter)
        status["problem_tracker"] = {"error": "exception"}

    # Step 6: false-negative detector (always — zero LLM cost, safety net)
    _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)

    return status


_GLUCOSE_LAB_KEYWORDS = (
    "glucose", "rbs", "cbg", "grbs", "blood glucose",
    "blood sugar", "random blood glucose", "random blood sugar",
)


def _is_glucose_lab(lab_doc: dict) -> bool:
    """True if the lab document represents a standalone glucose test."""
    name = (lab_doc.get("name") or "").lower()
    return any(kw in name for kw in _GLUCOSE_LAB_KEYWORDS)


def _is_glucose_next_check(overdue_item: dict) -> bool:
    """True if an overdue next_check item is tracking a glucose-related lab."""
    key = (overdue_item.get("key") or overdue_item.get("label") or "").lower()
    return any(kw in key for kw in _GLUCOSE_LAB_KEYWORDS)


_ABG_LAB_KEYWORDS = (
    "gas panel", "abg", "vbg", "arterial blood gas",
    "venous blood gas", "blood gas",
)

# (attr_key, operator, threshold) — operator is "lt" or "gt"
_ABG_THRESHOLD_CHECKS: list[tuple[str, str, float]] = [
    ("ph",      "lt", 7.30), ("ph",      "gt", 7.50),
    ("pao2",    "lt", 60.0),
    ("paco2",   "gt", 50.0), ("paco2",   "lt", 30.0),
    ("lactic",  "gt", 2.0),
    ("lactate", "gt", 2.0),
    ("hco3",    "lt", 15.0), ("hco3",    "gt", 35.0),
    ("bicarb",  "lt", 15.0), ("bicarb",  "gt", 35.0),
]


def _is_abg_lab(lab_doc: dict) -> bool:
    """True if the lab document represents a blood gas panel."""
    name = (lab_doc.get("name") or "").lower()
    return any(kw in name for kw in _ABG_LAB_KEYWORDS)


def _check_abg_thresholds(lab_doc: dict) -> bool:
    """Return True if any ABG value crosses a danger threshold."""
    attrs = lab_doc.get("attributes") or {}

    def _get(key: str):
        v = attrs.get(key)
        if v is None:
            return None
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for attr_key, op, threshold in _ABG_THRESHOLD_CHECKS:
        val = _get(attr_key)
        if val is None:
            continue
        if op == "lt" and val < threshold:
            return True
        if op == "gt" and val > threshold:
            return True
    return False


def _run_glucose_alert(
    cpmrn: str,
    encounter: int,
    db: Any,
    snapshot_at: Any,
    ctx: dict,
    delta: dict,
    status: dict,
) -> bool:
    """
    Run the cheap glucose-only alert path: fetch latest glucose, compute insulin
    recommendation, send alert card, and update next_check.due_after using the
    insulin timing (not the generic 24h lab_default).

    Returns True if handled (glucose value found), False if no glucose data
    (caller should fall through to the full pipeline).
    """
    try:
        import uuid as _uuid
        from tools.radar_sync.insulin_advice import gather_inputs as _gi, attach_insulin_order as _aio
        from tools.radar_sync.chat_card_sender import get_alert_recipients, send_batch_alert_cards
        from datetime import timedelta

        engine_input, _ = _gi(cpmrn, encounter)
        if not engine_input.get("GRBS"):
            logger.warning(
                "pipeline: glucose alert — no glucose value retrievable for %s enc=%d",
                cpmrn, encounter,
            )
            status["glucose_only_gate"] = "no_glucose_value"
            return False

        grbs_val = int(engine_input["GRBS"][0])
        assessment = {
            "problem_name":      "Hyperglycemia",
            "clinical_status":   "worsening",
            "should_alert":      True,
            "being_addressed":   False,
            "alert_title":       f"Hyperglycemia — glucose {grbs_val} mg/dL",
            "alert_reason":      f"New glucose result: {grbs_val} mg/dL. Insulin recommendation below.",
            "note_vs_objective": "",
        }
        _aio(cpmrn, encounter, assessment)

        # Use insulin-recommended timing for next_check, not the generic 24h lab_default.
        _insulin_data = assessment.get("_insulin_order") or {}
        _next_h = (_insulin_data.get("reco") or {}).get("next_grbs_after")
        if _next_h and isinstance(_next_h, (int, float)):
            try:
                db["patient_problems"].update_one(
                    {"CPMRN": cpmrn, "encounter": encounter, "problem_name": "Hyperglycemia", "next_check": {"$ne": None}},
                    {"$set": {"next_check.due_after": snapshot_at + timedelta(hours=int(_next_h))}},
                )
                logger.info(
                    "pipeline: glucose alert — next_check.due_after set to +%dh for %s enc=%d",
                    int(_next_h), cpmrn, encounter,
                )
            except Exception:
                logger.exception("pipeline: glucose alert — failed to update next_check.due_after for %s", cpmrn)

        existing_ctx = ctx.get("structured_summary") or {}
        recipients = get_alert_recipients(db)
        if recipients:
            send_batch_alert_cards(
                cpmrn, encounter,
                [(assessment, _uuid.uuid4().hex)],
                existing_ctx,
                recipients,
            )
            logger.info(
                "pipeline: glucose alert — card sent for %s enc=%d (glucose %d mg/dL)",
                cpmrn, encounter, grbs_val,
            )
        else:
            logger.warning(
                "pipeline: glucose alert — no recipients configured for %s enc=%d",
                cpmrn, encounter,
            )

        status["glucose_only_gate"] = "routed"
        status["pass1"] = status.get("pass1") or "skipped_glucose_only"
        status["pass2"] = status.get("pass2") or "skipped_glucose_only"
        db.snapshot_schedule.update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {
                "last_llm_run_at":    snapshot_at,
                "next_run_at":        _next_run_at(snapshot_at, 1),
                "last_io_aggregate":  delta.get("io_last_24h"),
                "last_delta_content": _compact_delta(delta),
            }},
        )
        return True

    except Exception:
        logger.exception(
            "pipeline: glucose alert failed for %s enc=%d — falling through to full pipeline",
            cpmrn, encounter,
        )
        return False


def _next_run_at(base, hours: int) -> datetime:
    """
    Compute next_run_at anchored to the scheduler's hourly tick boundary.

    Problem: if `base` is mid-pipeline (e.g. 12:10 UTC) and hours=1, naively
    `base + 1h = 13:10` — which the 13:00 tick skips, so the patient doesn't
    run until 14:00 (effectively 2h cadence instead of 1h).

    Fix: snap the target down to the nearest whole hour.
      12:10 + 1h → 13:10 → truncate → 13:00  (caught by 13:00 tick)
      12:10 + 4h → 16:10 → truncate → 16:00  (caught by 16:00 tick)

    Uses `snapshot_at` (passed as base) so the whole batch in a cycle
    shares the same anchor, not `now` which drifts patient-by-patient.
    """
    if isinstance(base, str):
        base = datetime.fromisoformat(base.replace("Z", "+00:00"))
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    target = base + timedelta(hours=hours)
    return target.replace(minute=0, second=0, microsecond=0)


def _write_patient_run_audit(
    cpmrn: str,
    encounter: int,
    run_started_at: "datetime",
    pipeline_status: dict,
    db: "Any" = None,
) -> None:
    """Write per-patient-per-run rows to BQ: pipeline_patient_runs + patient_next_checks."""
    try:
        from backend.services.bq_store import get_bq_store
        pass1 = pipeline_status.get("pass1") or {}
        tracker = pipeline_status.get("problem_tracker") or {}
        problem_details = tracker.get("problem_details") if isinstance(tracker, dict) else None
        store = get_bq_store()
        delta = pipeline_status.get("delta") or {}
        store.insert_patient_run({
            "run_started_at":   run_started_at,
            "CPMRN":            cpmrn,
            "encounter":        encounter,
            "pipeline_outcome": _derive_pipeline_outcome(pipeline_status),
            "pass1_tag":        pass1.get("flag_reason", "") if isinstance(pass1, dict) else "",
            "pass1_needs_full": bool(pass1.get("needs_full_analysis", False)) if isinstance(pass1, dict) else False,
            "pass2_outcome":    str(pipeline_status.get("pass2", "")),
            "problem_details":  problem_details,
            "delta_vitals":     delta.get("new_vitals", 0),
            "delta_labs":       delta.get("new_labs", 0),
            "delta_notes":      delta.get("new_notes", 0),
            "delta_reports":    (pipeline_status.get("report_select") or {}).get("n", 0),
            "trigger_reason":   str(pipeline_status.get("trigger_reason", "")),
        })
        # Write next_check state for each assessed problem.
        # problem_details.next_check is the raw model output — it has type/key/label
        # but NOT due_after (that is computed inside _upsert_problem and written to GCS).
        # Read the stored problem docs to get the actual computed due_after.
        if problem_details:
            if db is None:
                from backend.services.gcs_store import get_gcs_db
                db = get_gcs_db()
            stored = list(db["patient_problems"].find({"CPMRN": cpmrn, "encounter": encounter}))
            stored_nc = {p.get("problem_name", ""): (p.get("next_check") or {}) for p in stored}
            nc_rows = []
            for pd in problem_details:
                pname = pd.get("problem_name", "")
                nc = stored_nc.get(pname) or {}
                nc_type  = nc.get("type", "")
                nc_key   = nc.get("vital_key") or nc.get("lab_name") or nc.get("key", "")
                nc_label = nc.get("label", "")
                due_raw  = nc.get("due_after")
                nc_rows.append({
                    "run_started_at":  run_started_at,
                    "CPMRN":           cpmrn,
                    "encounter":       encounter,
                    "problem_name":    pname,
                    "nc_type":         nc_type,
                    "nc_key":          nc_key,
                    "nc_label":        nc_label,
                    "due_after":       due_raw,
                    "clinical_status": pd.get("clinical_status", ""),
                })
            store.insert_next_check_events(nc_rows)
    except Exception:
        logger.exception("scheduler: patient run audit BQ write failed for %s enc=%d", cpmrn, encounter)


def _emit_discharge_next_check_closures(
    cpmrn: str,
    encounter: int,
    run_started_at: "datetime",
    db: Any,
) -> None:
    """
    On discharge, write NULL due_after rows for all open next_checks so the BQ
    dashboard query knows they are closed. Reads current patient_problems from GCS.
    """
    try:
        from backend.services.bq_store import get_bq_store
        problems = list(db["patient_problems"].find({"CPMRN": cpmrn, "encounter": encounter}))
        if not problems:
            return
        nc_rows = []
        for pd in problems:
            nc = pd.get("next_check")
            if not nc:
                continue  # already cleared — no open window to close
            nc_rows.append({
                "run_started_at":  run_started_at,
                "CPMRN":           cpmrn,
                "encounter":       encounter,
                "problem_name":    pd.get("problem_name", ""),
                "nc_type":         None,
                "nc_key":          None,
                "nc_label":        None,
                "due_after":       None,  # explicit closure
                "clinical_status": pd.get("clinical_status", ""),
            })
        if nc_rows:
            get_bq_store().insert_next_check_events(nc_rows)
            logger.info(
                "scheduler: emitted %d next_check closure row(s) for discharged %s enc=%d",
                len(nc_rows), cpmrn, encounter,
            )
    except Exception:
        logger.exception(
            "scheduler: next_check closure BQ write failed for discharged %s enc=%d", cpmrn, encounter,
        )


def _compact_delta(delta: dict) -> dict:
    """Extract a compact, display-ready summary of the delta for GCS persistence."""
    vitals = [
        {k: v for k, v in vit.items()
         if k in ("timestamp", "daysHR", "daysBP", "daysMAP", "daysSpO2",
                  "daysRR", "daysFiO2", "daysTemp", "daysGCS")}
        for vit in (delta.get("new_vitals") or [])
    ]
    labs = [
        {
            "name":       lab.get("name", ""),
            "reportedAt": str(lab.get("reportedAt", "")),
            "values": {
                k: (v.get("value") if isinstance(v, dict) else v)
                for k, v in (lab.get("attributes") or {}).items()
                if v is not None and (v.get("value") if isinstance(v, dict) else v) is not None
            },
        }
        for lab in (delta.get("new_labs") or [])
    ]
    notes = [
        {"timestamp": n.get("timestamp"), "note_type": n.get("note_type", ""),
         "author": n.get("author", ""), "text": (n.get("text") or "")[:300]}
        for n in (delta.get("new_notes") or [])
    ]
    return {
        "vitals":     vitals,
        "labs":       labs,
        "notes":      notes,
        "io_changed": delta.get("io_changed", False),
        "io_last_24h": delta.get("io_last_24h"),
    }


def _derive_pipeline_outcome(status: dict) -> str:
    """Summarise pipeline_status into a short outcome string for the audit table."""
    if not status:
        return "unknown"
    if "error" in status:
        return f"error:{status['error']}"
    pass2 = status.get("pass2", "")
    if pass2 == "skipped_by_pass1":
        return "cheap"
    pt = status.get("problem_tracker") or {}
    if isinstance(pt, dict) and pt.get("alerts_sent"):
        return "alerted"
    pass1 = status.get("pass1") or {}
    if isinstance(pass1, dict) and pass1.get("needs_full_analysis"):
        return "expensive_no_alert"
    if pass2:
        return f"pass2:{pass2}"
    return "ok"


def _run_fn_detector_step(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    snapshot_at: datetime,
    db: Any,
    status: dict,
) -> None:
    """Run fn_detector and write result into status dict. Always called, never skipped."""
    try:
        from tools.radar_sync.fn_detector import run_fn_detector
        fn_result = run_fn_detector(cpmrn, encounter, structured_summary, snapshot_at, db)
        status["fn_detector"] = fn_result
        logger.info("pipeline: fn_detector done for %s enc=%d — %s", cpmrn, encounter, fn_result)
    except Exception:
        logger.exception("pipeline: fn_detector failed for %s enc=%d", cpmrn, encounter)
        status["fn_detector"] = {"error": "exception"}


def _run_med_recon_step(
    cpmrn: str,
    encounter: int,
    chart: dict,
    snapshot_at: datetime,
    db: Any,
    status: dict,
) -> dict:
    """
    Run medication reconciliation and write result into status dict. Returns the result
    dict so the caller can check card_sent and update the per-cycle send counter.
    """
    try:
        from tools.radar_sync.med_recon.orchestrator import run_med_recon
        result = run_med_recon(cpmrn, encounter, chart, snapshot_at, db)
        status["med_recon"] = result
        logger.info("pipeline: med_recon done for %s enc=%d — %s", cpmrn, encounter, result)
        return result
    except Exception:
        logger.exception("pipeline: med_recon failed for %s enc=%d", cpmrn, encounter)
        status["med_recon"] = {"error": "exception"}
        return {"error": "exception"}


def _run_report_interpret_step(
    cpmrn: str,
    encounter: int,
    chart: dict,
    snapshot_at: datetime,
    db: Any,
    status: dict,
    send: bool,
) -> dict:
    """
    Finalize the diagnostic-report cycle. In Phase 2, the LLM analysis was pre-computed
    inside _run_live_pipeline (Step 3c) and stashed on status["_report_analysis"]. This
    step hosts images, builds+sends the batched card, writes audit rows, and advances the
    watermark. `send=False` (per-cycle card cap) still runs everything except the send.
    """
    analysis = status.pop("_report_analysis", None)
    sel = status.pop("_report_sel", {}) or {}

    try:
        from tools.radar_sync.report_interpret.orchestrator import (
            finalize_report_cycle, _run_skip_row, _advance_watermark,
        )

        if analysis is None:
            outcome = sel.get("status") or "no_new_docs"
            if outcome not in ("no_new_docs", "disabled", "error"):
                outcome = "no_new_docs"
            # First-run watermark seed — prevents bulk re-scan on next cycle
            if outcome == "no_new_docs" and sel.get("last_report_at") is None:
                _advance_watermark(db, cpmrn, encounter, snapshot_at, sel.get("seen_keys"), None)
            _run_skip_row(cpmrn, encounter, snapshot_at, outcome, db)
            result = {"outcome": outcome, "card_sent": False}
            status["report_interpret"] = result
            logger.info(
                "pipeline: report_interpret skip for %s enc=%d — %s", cpmrn, encounter, outcome,
            )
            return result

        result = finalize_report_cycle(
            cpmrn, encounter, analysis, snapshot_at, db,
            send=send, seen_keys=sel.get("seen_keys"),
        )
        status["report_interpret"] = result
        logger.info(
            "pipeline: report_interpret done for %s enc=%d — %s", cpmrn, encounter, result,
        )
        return result

    except Exception:
        logger.exception("pipeline: report_interpret failed for %s enc=%d", cpmrn, encounter)
        status["report_interpret"] = {"error": "exception"}
        return {"error": "exception"}


def _collect_all(max_patients: int | None = None):
    """Run one full pipeline cycle. Pass max_patients to limit for local testing."""
    global _last_run_at, _last_run_results
    _last_run_at = datetime.now(timezone.utc)
    _last_run_results = []

    try:
        from backend.services.emr.db import get_db
        from tools.radar_sync.snapshot_collector import collect
        from tools.radar_sync.chart_puller import get_admitted_patients

        db = get_db()

        # ── Step 0: auto-enroll new admissions ────────────────────────────────
        # Skipped when max_patients is set (local testing) — avoids downloading
        # all snapshot_schedule blobs before the limit is applied.
        admitted_by_workspace: dict[str, set | None] = {}
        if max_patients is None:
            ws_config = db["app_settings"].find_one({"_id": "monitored_workspaces"})
            configured_ws: list[str] = (ws_config or {}).get("workspaces", [])
            scheduled_ws: list[str] = db.snapshot_schedule.distinct(
                "workspace", {"workspace": {"$exists": True, "$ne": None}}
            )
            all_workspaces: list[str] = list(dict.fromkeys(configured_ws + scheduled_ws))  # ordered, deduped

            # Fetch admitted patients for every monitored workspace.
            # admitted_by_workspace[ws] = set of (CPMRN, encounter) tuples, or None on error.
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
        if max_patients is not None:
            patients = patients[:max_patients]
        logger.info("scheduler: collecting %d active patient(s)%s", len(patients),
                    f" (limited to {max_patients})" if max_patients is not None else "")

        recon_cards_sent = 0   # cap med-recon sends per cycle to _RECON_CARD_LIMIT
        report_cards_sent = 0  # cap report-interpret sends per cycle to _REPORT_CARD_LIMIT

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
                        # Emit clearing rows to close any open next_checks in BQ.
                        _emit_discharge_next_check_closures(cpmrn, encounter, _last_run_at, db)
                        _last_run_results.append({"cpmrn": cpmrn, "status": "skipped_discharged"})
                        continue

                # Idempotency guard — skip snapshot collection if a fresh one exists.
                # But still run the pipeline if it hasn't run against this snapshot yet
                # (e.g. snapshot was collected via the API but pipeline never fired).
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=50)
                recent = db.snapshots.find_one(
                    {"CPMRN": cpmrn, "encounter": encounter, "snapshot_at": {"$gte": cutoff}}
                )
                if recent:
                    sched_doc = db.snapshot_schedule.find_one(
                        {"CPMRN": cpmrn, "encounter": encounter}
                    ) or {}
                    last_llm = sched_doc.get("last_llm_run_at")
                    snap_ts  = recent["snapshot_at"]
                    if isinstance(snap_ts, datetime) and snap_ts.tzinfo is None:
                        snap_ts = snap_ts.replace(tzinfo=timezone.utc)
                    if isinstance(last_llm, datetime):
                        if last_llm.tzinfo is None:
                            last_llm = last_llm.replace(tzinfo=timezone.utc)
                    pipeline_ran = isinstance(last_llm, datetime) and last_llm >= snap_ts
                    if pipeline_ran:
                        logger.info(
                            "scheduler: skipping %s enc=%d — snapshot and pipeline both fresh",
                            cpmrn, encounter,
                        )
                        _last_run_results.append({"cpmrn": cpmrn, "status": "skipped_duplicate"})
                        continue
                    # Snapshot exists but pipeline hasn't run against it yet — run pipeline only
                    logger.info(
                        "scheduler: %s enc=%d — fresh snapshot exists but pipeline not yet run, running pipeline",
                        cpmrn, encounter,
                    )
                    pipeline_status = _run_live_pipeline(
                        cpmrn, encounter, recent["chart"], snap_ts, db
                    )
                    if recon_cards_sent < _RECON_CARD_LIMIT:
                        recon_result = _run_med_recon_step(cpmrn, encounter, recent["chart"], snap_ts, db, pipeline_status)
                        if recon_result.get("card_sent"):
                            recon_cards_sent += 1
                    else:
                        logger.info("pipeline: med_recon skipped for %s enc=%d — cycle limit (%d) reached",
                                    cpmrn, encounter, _RECON_CARD_LIMIT)
                        pipeline_status["med_recon"] = {"skipped": "cycle_limit"}
                    # report-interpret: always run (interpret + audit); cap only the card send
                    report_result = _run_report_interpret_step(
                        cpmrn, encounter, recent["chart"], snap_ts, db, pipeline_status,
                        send=(report_cards_sent < _REPORT_CARD_LIMIT),
                    )
                    if report_result.get("card_sent"):
                        report_cards_sent += 1
                    _write_patient_run_audit(cpmrn, encounter, _last_run_at, pipeline_status, db=db)
                    _last_run_results.append({
                        "cpmrn": cpmrn, "status": "pipeline_on_existing_snapshot",
                        "pipeline": pipeline_status,
                    })
                    logger.info(
                        "scheduler: pipeline done for %s enc=%d — %s", cpmrn, encounter, pipeline_status,
                    )
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
                    if recon_cards_sent < _RECON_CARD_LIMIT:
                        recon_result = _run_med_recon_step(cpmrn, encounter, snap_doc["chart"], snap_ts, db, pipeline_status)
                        if recon_result.get("card_sent"):
                            recon_cards_sent += 1
                    else:
                        logger.info("pipeline: med_recon skipped for %s enc=%d — cycle limit (%d) reached",
                                    cpmrn, encounter, _RECON_CARD_LIMIT)
                        pipeline_status["med_recon"] = {"skipped": "cycle_limit"}
                    # report-interpret: always run (interpret + audit); cap only the card send
                    report_result = _run_report_interpret_step(
                        cpmrn, encounter, snap_doc["chart"], snap_ts, db, pipeline_status,
                        send=(report_cards_sent < _REPORT_CARD_LIMIT),
                    )
                    if report_result.get("card_sent"):
                        report_cards_sent += 1
                else:
                    pipeline_status = {"error": "snapshot_not_found"}

                _write_patient_run_audit(cpmrn, encounter, _last_run_at, pipeline_status, db=db)
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
        # ── Study pipeline — runs only when explicitly enabled ───────────────
        if _study_pipeline_enabled(db):
            try:
                from tools.radar_sync.study_runner import run_study_jobs
                study_result = run_study_jobs(db)
                _last_run_results.append({"step": "study_jobs", **study_result})
                logger.info("scheduler: study jobs done — %s", study_result)
            except Exception:
                logger.exception("scheduler: study jobs failed")
                _last_run_results.append({"step": "study_jobs", "error": "exception"})
        else:
            logger.info("scheduler: study pipeline disabled — skipping study jobs")
            _last_run_results.append({"step": "study_jobs", "status": "disabled"})

        # ── Cost tracker — aggregate LLM token costs for this run ────────────
        try:
            from tools.radar_sync.study_cost_tracker import compute_run_cost
            cost_doc = compute_run_cost(db, _last_run_at, total_patients_scheduled=len(patients))
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


def _study_pipeline_enabled(db=None) -> bool:
    """Return True only when study_pipeline_enabled is explicitly set to True in app_settings."""
    try:
        if db is None:
            from backend.services.emr.db import get_db
            db = get_db()
        doc = db["app_settings"].find_one({"_id": "study_pipeline_enabled"})
        return bool(doc and doc.get("enabled", False))
    except Exception:
        logger.exception("scheduler: could not read study_pipeline_enabled — defaulting to disabled")
        return False


def _run_study_jobs():
    """Run SBAR + task sync, LLM matching, FP candidacy, and metrics. Called 10 min after each hourly snapshot."""
    try:
        from backend.services.emr.db import get_db
        db = get_db()
        if not _study_pipeline_enabled(db):
            logger.info("study_jobs: study pipeline disabled — skipping")
            return
        from tools.radar_sync.study_sbar_syncer import sync_sbars
        from tools.radar_sync.study_task_syncer import sync_tasks
        from tools.radar_sync.study_matcher import run_llm_matching
        from tools.radar_sync.study_metrics import compute_metrics
        sbar_sync_result = sync_sbars(db)
        task_sync_result = sync_tasks(db)
        match_result     = run_llm_matching(db)
        metrics_result   = compute_metrics(db)
        logger.info(
            "study_jobs: sbar_sync=%s  task_sync=%s  matching=%s  metrics=%s",
            sbar_sync_result, task_sync_result, match_result, metrics_result,
        )
    except Exception:
        logger.exception("study_jobs: crashed")


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
    _scheduler.add_job(
        _run_study_jobs,
        trigger=CronTrigger(minute=10),  # 10 min after snapshot so alerts exist before matching
        id="hourly_study_jobs",
        name="Hourly study matcher + metrics",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("APScheduler started (PID %d) — hourly snapshot + study jobs active", os.getpid())


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
