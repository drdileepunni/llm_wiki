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
    Per-chart pipeline organised into four named phases:

      Prelude   — note indexing, delta extraction, report selection, overdue next-checks
      Phase 0   — ENTRY: empty-chart / cadence / delta gates
      Phase 1   — SKIP GATE: independent per-category significance check;
                  skip only when ALL present categories are NORMAL
      Phase 2   — TRIAGE: glucose-only path, forced-recheck path, or full analysis
      Phase 3   — ANALYSIS: report interpret → summary → classifier → tracker
                  → glucose side-step → fn_detector

    Appends structured records to status["gate_trace"] throughout.
    All existing status[...] keys are preserved for backward compatibility.
    """
    from backend.services.emr.db import get_db as _get_db
    if db is None:
        db = _get_db()

    status: dict = {}
    status["gate_trace"] = []

    def _trace(phase: str, gate: str, verdict: str, detail: str = "") -> None:
        status["gate_trace"].append({"phase": phase, "gate": gate, "verdict": verdict, "detail": detail})

    # ── PRELUDE: note indexing ────────────────────────────────────────────────
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
    sched_doc = db.snapshot_schedule.find_one({"CPMRN": cpmrn, "encounter": encounter}) or {}

    # ── PRELUDE: delta extraction ─────────────────────────────────────────────
    try:
        from tools.radar_sync.patient_context import get_context
        from tools.radar_sync.delta_extractor import extract_delta
        ctx = get_context(cpmrn, encounter)
        from tools.radar_sync.clinical_timeline import empty_timeline as _empty_timeline
        clinical_timeline = ctx.get("clinical_timeline") or _empty_timeline()

        last_llm_run_at_raw = sched_doc.get("last_llm_run_at")
        last_snapshot_ts    = ctx.get("last_snapshot_at")
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

    # ── PRELUDE: report selection ─────────────────────────────────────────────
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

    # ── PRELUDE: overdue per-problem next-checks ──────────────────────────────
    # force_expensive / force_glucose_check originate here from prior-run scheduling
    # (stored next_check.due_after on patient_problems). Consumed by all phases.
    #
    # Every overdue check (vital, lab, or io — no type filtering) is partitioned
    # by whether the specific value it's watching for actually showed up in this
    # cycle's delta:
    #   "arrived" — the watched value is here; this is the only case that
    #               warrants a full LLM re-assessment.
    #   "missing" — the watched value never showed up, regardless of whether
    #               something else in the chart changed. Never forces an
    #               expensive run — just a lightweight care-gap alert, sent
    #               unconditionally below regardless of what the rest of this
    #               cycle's pipeline does.
    try:
        from tools.radar_sync.problem_tracker import overdue_next_checks
        from tools.radar_sync.delta_scope import next_check_data_arrived
        _overdue_all = overdue_next_checks(cpmrn, encounter, db, now)
        _arrived = [o for o in _overdue_all if next_check_data_arrived(o, delta)]
        _missing = [o for o in _overdue_all if o not in _arrived]
    except Exception:
        logger.exception("pipeline: overdue_next_checks failed for %s enc=%d — treating as empty", cpmrn, encounter)
        _arrived = []
        _missing = []
    _overdue = _arrived  # arrived-only list feeds the existing force/glucose logic below
    _overdue_glucose = [o for o in _overdue if _is_glucose_next_check(o)]
    _overdue_generic  = [o for o in _overdue if not _is_glucose_next_check(o)]
    force_expensive     = bool(_overdue_generic)
    force_glucose_check = bool(_overdue_glucose) and not force_expensive
    if force_expensive:
        logger.info(
            "pipeline: %d overdue next_check(s) with arrived data for %s enc=%d — will force expensive run",
            len(_overdue_generic), cpmrn, encounter,
        )
    if force_glucose_check:
        logger.info(
            "pipeline: overdue glucose next_check for %s enc=%d — will route to cheap glucose pathway",
            cpmrn, encounter,
        )
    if _missing:
        logger.info(
            "pipeline: %d overdue next_check(s) still missing watched data for %s enc=%d — will care-gap alert only",
            len(_missing), cpmrn, encounter,
        )
    # Fire care-gap alerts for missing checks now, unconditionally — independent
    # of whether the rest of this cycle's pipeline runs, skips, or forces a full
    # analysis for unrelated reasons.
    _send_missing_care_gap_alerts(cpmrn, encounter, _missing, db, ctx, now, status)
    status["force_expensive"]     = force_expensive
    status["force_glucose_check"] = force_glucose_check

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

    # ── PHASE 0: ENTRY GATES ─────────────────────────────────────────────────

    # Entry: empty chart
    _has_any_vitals = bool(chart.get("vitals"))
    _has_any_labs   = any(d.get("category") == "labs" for d in (chart.get("documents") or []))
    _has_any_notes  = bool((chart.get("notes") or {}).get("finalNotes"))
    if not _has_any_vitals and not _has_any_labs and not _has_any_notes and not _has_new_reports:
        logger.info(
            "pipeline: empty-chart gate — no vitals, labs, notes or reports for %s enc=%d, skipping LLM",
            cpmrn, encounter,
        )
        _trace("entry", "empty_chart", "skip", "no vitals, labs, notes, or reports")
        status["pass1"] = "skipped_empty_chart"
        status["pass2"] = "skipped_empty_chart"
        db.snapshot_schedule.update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {"next_run_at": _next_run_at(snapshot_at, 1)}},
        )
        return status
    _trace("entry", "empty_chart", "ran", "chart has data")

    # Entry: cadence
    next_run_at = _coerce_dt(sched_doc.get("next_run_at"))
    if next_run_at is not None and now < next_run_at:
        if not _has_new_reports and not force_expensive and not force_glucose_check:
            _next_run_ist = next_run_at.astimezone(timezone(timedelta(hours=5, minutes=30)))
            logger.info(
                "pipeline: cadence gate — skipping LLM for %s enc=%d (next_run_at %s)",
                cpmrn, encounter, _next_run_ist.strftime("%H:%M IST"),
            )
            _trace("entry", "cadence", "skip", f"not due until {_next_run_ist.strftime('%H:%M IST')}")
            status["cadence_gate"] = "skipped"
            status["_report_analysis"] = None
            status["_report_sel"] = _report_sel
            new_structured = ctx.get("structured_summary") or {}
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status
        if _has_new_reports:
            logger.info("pipeline: cadence gate bypassed for %s enc=%d — %d new report(s)", cpmrn, encounter, len(_new_reports))
            _trace("entry", "cadence", "bypassed", f"{len(_new_reports)} new report(s)")
        elif force_expensive:
            logger.info("pipeline: cadence gate bypassed for %s enc=%d — forced by overdue next_check", cpmrn, encounter)
            _trace("entry", "cadence", "bypassed", "overdue next_check")
        elif force_glucose_check:
            logger.info("pipeline: cadence gate bypassed for %s enc=%d — overdue glucose next_check", cpmrn, encounter)
            _trace("entry", "cadence", "bypassed", "overdue glucose next_check")
    else:
        _trace("entry", "cadence", "ran", "due now")

    # Entry: delta
    # Note: force_expensive/force_glucose_check can never save a truly-empty
    # delta from being skipped here — both are derived purely from _arrived
    # (data that showed up in THIS cycle's delta), which by definition implies
    # has_new_data is already True. An overdue check with no arrived data is
    # "missing" and gets a care-gap alert (handled unconditionally above in
    # the Prelude), never a forced run.
    last_llm_run_at = _coerce_dt(last_llm_run_at_raw)
    if last_llm_run_at is not None:
        has_new_data = any([
            delta.get("new_vitals"), delta.get("new_labs"), delta.get("new_notes"),
            delta.get("io_changed"),
        ])
        if not has_new_data and not _has_new_reports:
            logger.info(
                "pipeline: delta gate — no new data for %s enc=%d since last LLM run, skipping",
                cpmrn, encounter,
            )
            _trace("entry", "delta", "skip", "no new data since last LLM run")
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
        else:
            _trace("entry", "delta", "ran", "new data found")
    else:
        _trace("entry", "delta", "ran", "first run (no prior LLM run)")

    # ── PHASE 1: SKIP GATE ───────────────────────────────────────────────────
    # Evaluate each present data category independently. SKIP only when ALL
    # present categories are NORMAL. Vitals now evaluated regardless of whether
    # other categories are also present (unified — was vitals-only previously).

    _gate_new_vitals = delta.get("new_vitals") or []
    _gate_new_labs   = delta.get("new_labs") or []
    _gate_new_notes  = delta.get("new_notes") or []
    _gate_new_rf     = delta.get("new_report_findings") or []

    _abg_labs   = [lab for lab in _gate_new_labs if _is_abg_lab(lab)]
    _other_labs = [lab for lab in _gate_new_labs if not _is_abg_lab(lab) and not _is_glucose_lab(lab)]

    # Skip gate only evaluates when not already forced to full/bypass
    _skip_gate_active = (
        not force_expensive
        and not _has_new_reports
    )

    force_full_vitals    = False
    force_full_abg       = False
    force_full_other_lab = False

    # Vitals — unified: independent of whether other categories are present
    if _gate_new_vitals and _skip_gate_active:
        try:
            from tools.radar_sync.fn_detector import all_new_vitals_normal
            if all_new_vitals_normal(_gate_new_vitals):
                logger.info(
                    "pipeline: vital-normal gate — all %d new vital(s) normal for %s enc=%d",
                    len(_gate_new_vitals), cpmrn, encounter,
                )
                status["vital_normal_gate"] = "normal"
                _trace("skip_gate", "vitals", "normal", f"{len(_gate_new_vitals)} vital(s) in range")
            else:
                logger.info(
                    "pipeline: vital-normal gate — abnormal vital(s) for %s enc=%d, checking NEWS2 delta",
                    cpmrn, encounter,
                )
                # Smart skip: check whether the NEWS2 component profile has meaningfully
                # changed vs the last-run baseline AND vs the 6h baseline (drift check).
                # Applies to ALL patients — not just those in cooldown.
                # If both baselines are stable, skip the expensive run.
                try:
                    from tools.radar_sync.fn_detector import check_vitals_news2_delta
                    _skip_vitals, _vitals_detail = check_vitals_news2_delta(
                        _gate_new_vitals, cpmrn, encounter, db,
                    )
                except Exception:
                    logger.exception(
                        "pipeline: NEWS2 delta check failed for %s enc=%d — treating as significant",
                        cpmrn, encounter,
                    )
                    _skip_vitals, _vitals_detail = False, "check error"

                if _skip_vitals:
                    logger.info(
                        "pipeline: vital smart skip for %s enc=%d — %s",
                        cpmrn, encounter, _vitals_detail,
                    )
                    status["vital_normal_gate"] = "news2_stable_skip"
                    _trace("skip_gate", "vitals", "news2_stable_skip", _vitals_detail)
                else:
                    logger.info(
                        "pipeline: vital-normal gate — forcing full analysis for %s enc=%d — %s",
                        cpmrn, encounter, _vitals_detail,
                    )
                    force_full_vitals = True
                    status["vital_normal_gate"] = "triggered"
                    _trace("skip_gate", "vitals", "significant", _vitals_detail)

            # Always write the NEWS2 snapshot for this run so future runs have a baseline.
            # Done unconditionally (normal or abnormal vitals) — normal runs anchor the baseline too.
            try:
                from tools.radar_sync.fn_detector import write_news2_run_snapshot
                write_news2_run_snapshot(_gate_new_vitals, cpmrn, encounter, db)
            except Exception:
                logger.exception("pipeline: write_news2_run_snapshot failed for %s enc=%d", cpmrn, encounter)
        except Exception:
            logger.exception("pipeline: vital-normal gate check failed for %s enc=%d — treating as significant", cpmrn, encounter)
            force_full_vitals = True
            status["vital_normal_gate"] = "error"
            _trace("skip_gate", "vitals", "significant", "check error — treating as significant")
    elif _gate_new_vitals:
        _trace("skip_gate", "vitals", "ran", "forced run — vitals present")

    # ABG — deterministic threshold check
    if _abg_labs and _skip_gate_active:
        if any(_check_abg_thresholds(lab) for lab in _abg_labs):
            logger.info("pipeline: ABG gate — threshold crossed for %s enc=%d, forcing full analysis", cpmrn, encounter)
            force_full_abg = True
            status["abg_gate"] = "triggered"
            _trace("skip_gate", "abg", "significant", "threshold crossed")
        else:
            logger.info("pipeline: ABG gate — ABG present, no threshold crossed for %s enc=%d", cpmrn, encounter)
            status["abg_gate"] = "no_threshold"
            _trace("skip_gate", "abg", "normal", "ABG present, no threshold crossed")
    elif _abg_labs:
        _trace("skip_gate", "abg", "ran", "forced run — ABG present")

    # Other labs — rule-based normal-range check (GCS-backed config, cached).
    # Only force full if any value is out of range or panel is unrecognised.
    if _other_labs and _skip_gate_active:
        from tools.radar_sync.lab_normal_ranges import (
            load_config as _load_normal_cfg,
            check_lab_normal as _check_lab_normal,
        )
        _normal_cfg = _load_normal_cfg(db)
        _abnormal_labs  = []
        _unknown_labs   = []
        _normal_labs    = []
        for _lab in _other_labs:
            _check = _check_lab_normal(_lab, _normal_cfg, _GLUCOSE_LAB_KEYWORDS)
            _lname = _lab.get("name", "?")
            if _check is False:
                _abnormal_labs.append(_lname)
            elif _check is None:
                _unknown_labs.append(_lname)
            else:
                _normal_labs.append(_lname)
        _flagged = _abnormal_labs + _unknown_labs
        if _flagged:
            logger.info(
                "pipeline: other-lab gate — abnormal/unknown lab(s) %s for %s enc=%d, forcing full analysis",
                _flagged, cpmrn, encounter,
            )
            force_full_other_lab = True
            _detail = (
                ("abnormal: " + ", ".join(_abnormal_labs) if _abnormal_labs else "") +
                (" | unknown: " + ", ".join(_unknown_labs) if _unknown_labs else "")
            ).strip(" |")
            status["other_lab_gate"] = "triggered: " + _detail
            _trace("skip_gate", "other_labs", "significant", _detail)
        else:
            logger.info(
                "pipeline: other-lab gate — all %d lab(s) within normal range for %s enc=%d",
                len(_normal_labs), cpmrn, encounter,
            )
            status["other_lab_gate"] = "normal: " + ", ".join(_normal_labs)
            _trace("skip_gate", "other_labs", "normal",
                   "all within range: " + ", ".join(_normal_labs))
    elif _other_labs:
        _trace("skip_gate", "other_labs", "ran", "forced run — other labs present")

    _force_full_upstream = force_full_vitals or force_full_abg or force_full_other_lab

    # Notes screener — runs BEFORE summary update (no dependency on new summary)
    last_problems = list(db["patient_problems"].find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"problem_name": 1, "clinical_status": 1,
         "last_assessed_at": 1, "reasoning_fingerprint": 1},
    ))
    pass1 = None

    if _force_full_upstream:
        logger.info(
            "pipeline: pass1 skipped for %s enc=%d — upstream gate forced full analysis",
            cpmrn, encounter,
        )
        _trace("skip_gate", "notes", "bypassed", "upstream gate forced full analysis")
        status["pass1"] = "skipped_upstream_gate"
        db.snapshot_schedule.update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": {
                "last_llm_run_at":    snapshot_at,
                "next_run_at":        _next_run_at(snapshot_at, 1),
                "last_io_aggregate":  delta.get("io_last_24h"),
                "last_delta_content": _compact_delta(delta),
            }},
        )
    elif not _gate_new_notes:
        logger.info("pipeline: pass1 skipped for %s enc=%d — no new notes", cpmrn, encounter)
        _trace("skip_gate", "notes", "normal", "no new notes")
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
            status["_report_analysis"] = None
            status["_report_sel"] = _report_sel
            new_structured = ctx.get("structured_summary") or {}
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status
    else:
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
            if pass1.needs_full_analysis:
                _trace("skip_gate", "notes", "significant", pass1.flag_reason or "screener flagged")
            else:
                _trace("skip_gate", "notes", "normal", "screener: no new findings")
        except Exception:
            logger.exception("pipeline: pass1 screener failed for %s enc=%d — running full Pass 2", cpmrn, encounter)
            pass1 = None
            _trace("skip_gate", "notes", "significant", "screener error — fail-safe")

    # Combined skip decision: skip only when ALL present categories are NORMAL
    if _skip_gate_active and not _force_full_upstream:
        _vitals_ok = not _gate_new_vitals or status.get("vital_normal_gate") in ("normal", "news2_stable_skip")
        _abg_ok    = not _abg_labs or status.get("abg_gate") == "no_threshold"
        _labs_ok   = not bool(_other_labs)
        _notes_ok  = not _gate_new_notes or (pass1 is not None and not pass1.needs_full_analysis)
        _glucose_present = any(_lab_contains_glucose(lab) for lab in _gate_new_labs if not _is_abg_lab(lab))
        if _vitals_ok and _abg_ok and _labs_ok and _notes_ok and not _glucose_present:
            _trace("skip_gate", "combined", "skip", "all categories normal")
            if pass1 is not None and not pass1.needs_full_analysis:
                status["pass2"] = "skipped_by_pass1"
            status["_report_analysis"] = None
            status["_report_sel"] = _report_sel
            new_structured = ctx.get("structured_summary") or {}
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            return status
        else:
            _sig = [c for c, ok in [("vitals", _vitals_ok), ("abg", _abg_ok), ("labs", _labs_ok), ("notes", _notes_ok)] if not ok]
            if _glucose_present:
                _sig.append("glucose")
            _trace("skip_gate", "combined", "proceed", ", ".join(_sig) + " significant")
    elif force_expensive or _has_new_reports:
        _trace("skip_gate", "combined", "forced", "overdue checks or new reports")

    # ── PHASE 2: TRIAGE ──────────────────────────────────────────────────────
    # Note: the old "forced-recheck shortcut" (delta_gate == "bypassed_forced")
    # is gone — it's structurally unreachable now that force_expensive/
    # force_glucose_check are derived purely from arrived data, which by
    # definition means the delta gate above already found new data and never
    # skipped. Overdue checks with no arrived data are "missing" and get a
    # care-gap alert in the Prelude instead (see _send_missing_care_gap_alerts).

    # Glucose-only gate — bypass full pipeline for standalone glucose labs
    _glucose_only_delta = (
        bool(_gate_new_labs)
        and not _gate_new_vitals
        and not _gate_new_notes
        and not _gate_new_rf
        and all(_is_glucose_lab(d) for d in _gate_new_labs)
        and not force_expensive
        and not _has_new_reports
    )
    if _glucose_only_delta:
        _trace("triage", "glucose", "limited", "glucose-only delta — insulin path")
        status["glucose_only_gate"] = status.get("glucose_only_gate") or "routing"
        if _run_glucose_alert(cpmrn, encounter, db, snapshot_at, ctx, delta, status):
            _run_fn_detector_step(cpmrn, encounter, ctx.get("structured_summary") or {}, snapshot_at, db, status)
            _trace("analysis", "fn_detector", "ran", "")
            return status

    # Full analysis — determine trigger scope
    _sig_cats = []
    if force_full_vitals:    _sig_cats.append("vitals")
    if force_full_abg:       _sig_cats.append("abg")
    if force_full_other_lab: _sig_cats.append("other_labs")
    if pass1 is not None and pass1.needs_full_analysis: _sig_cats.append("notes")
    _trace("triage", "scope", "full", ", ".join(_sig_cats) if _sig_cats else "full run")

    # ── PHASE 3: ANALYSIS ────────────────────────────────────────────────────

    # Report interpretation — inject findings into delta before summary update
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
                logger.warning("pipeline: report interpret returned None for %s enc=%d", cpmrn, encounter)
        except Exception:
            logger.exception("pipeline: report interpretation failed for %s enc=%d", cpmrn, encounter)
            status["_report_analysis"] = None
    else:
        status["_report_analysis"] = None

    status["_report_sel"] = _report_sel

    # Summary update
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
        _trace("analysis", "summary", "ran", "summary updated")
    except Exception:
        logger.exception("pipeline: summary update failed for %s enc=%d", cpmrn, encounter)
        status["summary"] = "error"
        _trace("analysis", "summary", "error", "summary update failed")
        return status

    # Skip Pass 2 if screener said not needed (and no override)
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
            _trace("analysis", "classifier", "skipped", "pass1 said not needed")
            _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
            _trace("analysis", "fn_detector", "ran", "")
            return status

    # Status classifier
    if _force_full_upstream or pass1 is None or (pass1 is not None and pass1.needs_full_analysis):
        try:
            from tools.radar_sync.status_classifier import classify_statuses
            new_structured = classify_statuses(cpmrn, encounter, new_structured, delta=delta, clinical_timeline=clinical_timeline)
            status["classifier"] = "ok"
            logger.info("pipeline: status classification done for %s enc=%d", cpmrn, encounter)
            _trace("analysis", "classifier", "ran", "status classification done")
        except Exception:
            logger.exception("pipeline: status classification failed for %s enc=%d", cpmrn, encounter)
            status["classifier"] = "error"
            _trace("analysis", "classifier", "error", "classification failed")
    else:
        status["classifier"] = "skipped_forced_only"
        _trace("analysis", "classifier", "skipped", "forced-only run")

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

    # Trigger-scope: narrow which problems are assessed for upstream-gate runs
    _problems_filter: list[str] | None = None
    if _force_full_upstream:
        try:
            from tools.radar_sync.delta_scope import get_scoped_problem_names
            _summary_problems = new_structured.get("problems") or [] if isinstance(new_structured, dict) else []
            _problems_filter = get_scoped_problem_names(delta, _summary_problems)
            status["problems_scoped"] = _problems_filter
            if _problems_filter:
                logger.info(
                    "pipeline: trigger-scope — assessing %d/%d problem(s) for %s enc=%d: %s",
                    len(_problems_filter), len(_summary_problems), cpmrn, encounter, _problems_filter,
                )
        except Exception:
            logger.exception("pipeline: trigger-scope failed for %s enc=%d — assessing all", cpmrn, encounter)

    # Problem tracker
    try:
        from tools.radar_sync.problem_tracker import track_problems
        screener_flag = pass1.flag_reason if pass1 is not None else ""
        tracker_result = track_problems(
            cpmrn, encounter, new_structured, snapshot_at,
            screener_flag=screener_flag,
            focus=_overdue if _overdue else None,
            delta=delta,
            clinical_timeline=clinical_timeline,
            problems_filter=_problems_filter,
        )
        status["problem_tracker"] = tracker_result
        if _force_full_upstream and not status.get("pass2"):
            status["pass2"] = "upstream_forced"
        updated_timeline = tracker_result.get("clinical_timeline")
        if updated_timeline is not None:
            db["patient_contexts"].update_one(
                {"CPMRN": cpmrn, "encounter": encounter},
                {"$set": {"clinical_timeline": updated_timeline}},
            )
        logger.info("pipeline: problem tracker done for %s enc=%d — %s", cpmrn, encounter, tracker_result)
        _alerts_sent = tracker_result.get("alerts_sent", [])
        _delivery_ok = tracker_result.get("alert_delivery_ok")
        if _alerts_sent and _delivery_ok is False:
            _trace("analysis", "problem_tracker", "delivery_failed",
                   f"alert decided but NOT delivered: {_alerts_sent}")
        else:
            _trace("analysis", "problem_tracker", "ran", f"alerts: {_alerts_sent}")
    except Exception:
        logger.exception("pipeline: problem tracker failed for %s enc=%d", cpmrn, encounter)
        status["problem_tracker"] = {"error": "exception"}
        _trace("analysis", "problem_tracker", "error", "tracker failed")

    # Glucose side-step — runs whenever a glucose lab is in the delta (including panels)
    _new_labs_for_glucose = delta.get("new_labs") or []
    if any(_lab_contains_glucose(lab) for lab in _new_labs_for_glucose):
        try:
            _pt_result = status.get("problem_tracker") or {}
            _pt_alerted = _pt_result.get("alerts_sent") or [] if isinstance(_pt_result, dict) else []
            _glucose_already_alerted = any(
                a.lower() in ("hyperglycemia", "hypoglycemia") for a in _pt_alerted
            )
            _run_glucose_alert(
                cpmrn, encounter, db, snapshot_at, ctx, delta, status,
                suppress_card=_glucose_already_alerted,
            )
            _trace("analysis", "glucose_sidestep", "ran", f"card_suppressed={_glucose_already_alerted}")
            logger.info(
                "pipeline: glucose side-step ran for %s enc=%d (card_suppressed=%s)",
                cpmrn, encounter, _glucose_already_alerted,
            )
        except Exception:
            logger.exception("pipeline: glucose side-step failed for %s enc=%d", cpmrn, encounter)
            _trace("analysis", "glucose_sidestep", "error", "glucose side-step failed")

    # fn_detector — always runs (zero LLM cost, safety net)
    _run_fn_detector_step(cpmrn, encounter, new_structured, snapshot_at, db, status)
    _trace("analysis", "fn_detector", "ran", "")

    return status


_GLUCOSE_LAB_KEYWORDS = (
    "glucose", "rbs", "cbg", "grbs", "blood glucose",
    "blood sugar", "random blood glucose", "random blood sugar",
)


def _is_glucose_lab(lab_doc: dict) -> bool:
    """True if the lab document name identifies it as a glucose test.
    Name-only check — used by Gates 2.6/2.8 to route lab-triggered runs.
    For detecting glucose within panel attributes, use _lab_contains_glucose."""
    name = (lab_doc.get("name") or "").lower()
    return any(kw in name for kw in _GLUCOSE_LAB_KEYWORDS)


def _lab_contains_glucose(lab_doc: dict) -> bool:
    """True if the lab is a glucose test OR contains glucose as a panel attribute."""
    if _is_glucose_lab(lab_doc):
        return True
    attrs = lab_doc.get("attributes") or {}
    return any(
        any(kw in (k or "").lower() for kw in _GLUCOSE_LAB_KEYWORDS)
        for k in attrs
    )


def _is_glucose_next_check(overdue_item: dict) -> bool:
    """True if an overdue next_check item is tracking a glucose-related lab."""
    key = (overdue_item.get("key") or overdue_item.get("label") or "").lower()
    return any(kw in key for kw in _GLUCOSE_LAB_KEYWORDS)


_CARE_GAP_COOLDOWN_H = 4  # minimum hours between repeat care-gap alerts for the same missing check


def _send_missing_care_gap_alerts(
    cpmrn: str,
    encounter: int,
    missing: list[dict],
    db: Any,
    ctx: dict,
    now: datetime,
    status: dict,
) -> None:
    """
    Send a lightweight care-gap alert for each overdue next_check whose watched
    value hasn't shown up yet — regardless of what else changed in the chart
    this cycle. Never forces an expensive LLM run; this is a fire-and-forget
    notification only.

    Subject to a per-check cooldown (next_check.last_care_gap_alert_at) so a
    chronically-missing item doesn't re-alert on every pipeline tick.
    """
    if not missing:
        return

    _due_for_alert = []
    for o in missing:
        doc = o.get("doc") or {}
        nc = doc.get("next_check") or {}
        last_sent = nc.get("last_care_gap_alert_at")
        if isinstance(last_sent, str):
            try:
                last_sent = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
            except ValueError:
                last_sent = None
        if isinstance(last_sent, datetime):
            if last_sent.tzinfo is None:
                last_sent = last_sent.replace(tzinfo=timezone.utc)
            if now - last_sent < timedelta(hours=_CARE_GAP_COOLDOWN_H):
                continue
        _due_for_alert.append(o)

    if not _due_for_alert:
        return

    try:
        from tools.radar_sync.chat_card_sender import get_alert_recipients, send_batch_alert_cards
        import uuid as _uuid
        recipients = get_alert_recipients(db)
        if not recipients:
            logger.warning("pipeline: care-gap — no recipients configured for %s enc=%d", cpmrn, encounter)
            return

        care_gap_alerts = []
        for o in _due_for_alert:
            pname = o.get("problem_name", "Unknown problem")
            label = o.get("label") or o.get("key") or o.get("type") or "check"
            clinical_status = (o.get("doc") or {}).get("clinical_status", "")
            care_gap_alerts.append(({
                "problem_name":    pname,
                "clinical_status": clinical_status,
                "should_alert":    True,
                "being_addressed": False,
                "alert_title":     f"Care gap — {pname}",
                "alert_reason": (
                    f"Expected {label} check is overdue and no new {label} data has been "
                    f"charted since the check window opened. Please document or perform the check."
                ),
                "note_vs_objective": "",
            }, _uuid.uuid4().hex))

        send_batch_alert_cards(
            cpmrn, encounter, care_gap_alerts,
            ctx.get("structured_summary") or {},
            recipients,
        )
        for o in _due_for_alert:
            db["patient_problems"].update_one(
                {"CPMRN": cpmrn, "encounter": encounter, "problem_name": o.get("problem_name")},
                {"$set": {"next_check.last_care_gap_alert_at": now}},
            )
        logger.info(
            "pipeline: care-gap alert(s) sent for %s enc=%d — %d missing next_check(s)",
            cpmrn, encounter, len(care_gap_alerts),
        )
        status["care_gap_alerts_sent"] = len(care_gap_alerts)
    except Exception:
        logger.exception("pipeline: care-gap alert failed for %s enc=%d", cpmrn, encounter)


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


# Other-lab normal-range gate — logic lives in tools/radar_sync/lab_normal_ranges.py.
# The module is imported inside _run_live_pipeline (Phase 1) and cached there.
def _run_glucose_alert(
    cpmrn: str,
    encounter: int,
    db: Any,
    snapshot_at: Any,
    ctx: dict,
    delta: dict,
    status: dict,
    suppress_card: bool = False,
) -> bool:
    """
    Run the glucose alert path: fetch latest glucose, compute insulin
    recommendation, optionally send alert card, and update next_check.due_after.

    suppress_card=True skips the alert card (used when problem_tracker already
    sent a Hyperglycemia/Hypoglycemia alert in the same run).

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
        is_hypoglycemia = grbs_val < 70

        if is_hypoglycemia:
            assessment = {
                "problem_name":      "Hypoglycemia",
                "clinical_status":   "critical",
                "should_alert":      True,
                "being_addressed":   False,
                "alert_title":       f"Hypoglycemia — glucose {grbs_val} mg/dL",
                "alert_reason":      f"Glucose dropped to {grbs_val} mg/dL. Hypoglycemia requires urgent assessment.",
                "note_vs_objective": "",
            }
            # No insulin guidance for hypoglycemia — do not call _aio()
            _next_h = 1  # recheck in 1h
        else:
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
            _insulin_data = assessment.get("_insulin_order") or {}
            _reco = _insulin_data.get("reco") or {}
            _next_h = _reco.get("next_grbs_after") or None
            _dose = _reco.get("Suggested_insulin_dose", -1)

            # Suppress alert when there is no recommendation or dose is 0 IU — nothing to show.
            if _dose in (-1, 0):
                logger.info(
                    "pipeline: glucose alert — dose is %s (no actionable recommendation), suppressing alert for %s enc=%d",
                    _dose, cpmrn, encounter,
                )
                if _next_h and isinstance(_next_h, (int, float)):
                    try:
                        db["patient_problems"].update_one(
                            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": "Hyperglycemia", "next_check": {"$ne": None}},
                            {"$set": {"next_check.due_after": snapshot_at + timedelta(hours=int(_next_h))}},
                        )
                    except Exception:
                        logger.exception("pipeline: glucose alert — failed to update next_check.due_after for %s", cpmrn)
                status["glucose_only_gate"] = "suppressed_zero_dose"
                status["pass1"] = status.get("pass1") or "skipped_glucose_only"
                status["pass2"] = status.get("pass2") or "skipped_glucose_only"
                db.snapshot_schedule.update_one(
                    {"CPMRN": cpmrn, "encounter": encounter},
                    {"$set": {
                        "last_llm_run_at":    snapshot_at,
                        "next_run_at":        _next_run_at(snapshot_at, int(_next_h) if _next_h else 6),
                        "last_io_aggregate":  delta.get("io_last_24h"),
                        "last_delta_content": _compact_delta(delta),
                    }},
                )
                return True

        # Use insulin-recommended timing for next_check, not the generic 24h lab_default.
        if _next_h and isinstance(_next_h, (int, float)):
            try:
                problem_name_for_nc = "Hypoglycemia" if is_hypoglycemia else "Hyperglycemia"
                db["patient_problems"].update_one(
                    {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name_for_nc, "next_check": {"$ne": None}},
                    {"$set": {"next_check.due_after": snapshot_at + timedelta(hours=int(_next_h))}},
                )
                logger.info(
                    "pipeline: glucose alert — next_check.due_after set to +%dh for %s enc=%d",
                    int(_next_h), cpmrn, encounter,
                )
            except Exception:
                logger.exception("pipeline: glucose alert — failed to update next_check.due_after for %s", cpmrn)

        existing_ctx = ctx.get("structured_summary") or {}
        if suppress_card:
            logger.info(
                "pipeline: glucose alert — card suppressed for %s enc=%d (problem_tracker already alerted)",
                cpmrn, encounter,
            )
        else:
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
        # pass1 may be a dict (screener ran) or a string ("skipped_upstream_gate", etc.)
        if isinstance(pass1, dict):
            _pass1_tag       = pass1.get("flag_reason", "")
            _pass1_needs_full = bool(pass1.get("needs_full_analysis", False))
        elif pass1 == "skipped_upstream_gate":
            _pass1_tag       = "upstream_gate"
            _pass1_needs_full = False
        else:
            _pass1_tag       = ""
            _pass1_needs_full = False
        store.insert_patient_run({
            "run_started_at":   run_started_at,
            "CPMRN":            cpmrn,
            "encounter":        encounter,
            "pipeline_outcome": _derive_pipeline_outcome(pipeline_status),
            "pass1_tag":        _pass1_tag,
            "pass1_needs_full": _pass1_needs_full,
            "pass2_outcome":    str(pipeline_status.get("pass2", "")),
            "problem_details":  problem_details,
            "delta_vitals":     delta.get("new_vitals", 0),
            "delta_labs":       delta.get("new_labs", 0),
            "delta_notes":      delta.get("new_notes", 0),
            "delta_reports":    (pipeline_status.get("report_select") or {}).get("n", 0),
            "trigger_reason":   str(pipeline_status.get("trigger_reason", "")),
            "problems_scoped":  pipeline_status.get("problems_scoped"),  # None = full run
            "gate_trace":       pipeline_status.get("gate_trace"),
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
                    "run_started_at":    run_started_at,
                    "CPMRN":             cpmrn,
                    "encounter":         encounter,
                    "problem_name":      pname,
                    "nc_type":           nc_type,
                    "nc_key":            nc_key,
                    "nc_label":          nc_label,
                    "due_after":         due_raw,
                    "clinical_status":   pd.get("clinical_status", ""),
                    "tracker_reasoning": pd.get("tracker_reasoning", ""),
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
        # alerts_sent means the tracker DECIDED to alert — alert_delivery_ok reflects
        # whether the Google Chat send actually succeeded. False means the decision
        # was made but nothing reached a clinician (see problem_tracker logs).
        if pt.get("alert_delivery_ok") is False:
            return "alert_send_failed"
        return "alerted"
    pass1 = status.get("pass1") or {}
    if isinstance(pass1, dict) and pass1.get("needs_full_analysis"):
        return "expensive_no_alert"
    # Upstream gate (lab/vital) forced a full expensive run but no alert was sent
    if pass1 == "skipped_upstream_gate":
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


def run_single_patient(cpmrn: str, encounter: int = 1) -> dict:
    """Pull a FRESH chart for one patient and run the live pipeline end-to-end,
    returning a rich debug dict. Used by the /debug/run-one endpoint.

    Unlike _collect_all, this always pulls a fresh chart (bypasses the 50-min
    snapshot-reuse idempotency guard) and surfaces the exact inputs to the delta
    gate — raw vs verified vital counts, newest vital timestamps, the cutoff used,
    delta counts, and the full gate_trace — so a "no new data" skip can be
    diagnosed from a single call without sifting all 30-40 charts.
    """
    from backend.services.emr.db import get_db
    from tools.radar_sync.chart_puller import pull_chart

    db = get_db()
    snapshot_at = datetime.now(timezone.utc)
    debug: dict = {
        "cpmrn": cpmrn,
        "encounter": encounter,
        "snapshot_at": snapshot_at.isoformat(),
    }
    logger.info("debug-run: ===== single-patient run start for %s enc=%d =====", cpmrn, encounter)

    # ── 1. Snapshot-schedule state (the cutoff inputs) ───────────────────────
    sched_doc = db.snapshot_schedule.find_one({"CPMRN": cpmrn, "encounter": encounter}) or {}
    if not sched_doc:
        debug["warning"] = "patient not found in snapshot_schedule (not enrolled / inactive)"
    last_llm_run_at = _coerce_dt(sched_doc.get("last_llm_run_at"))
    next_run_at     = _coerce_dt(sched_doc.get("next_run_at"))
    debug["schedule"] = {
        "active":          sched_doc.get("active"),
        "last_llm_run_at": last_llm_run_at.isoformat() if last_llm_run_at else None,
        "next_run_at":     next_run_at.isoformat() if next_run_at else None,
        "last_io_aggregate": sched_doc.get("last_io_aggregate"),
    }

    # patient_contexts cutoff fallback
    try:
        from tools.radar_sync.patient_context import get_context
        _ctx = get_context(cpmrn, encounter)
        _last_snap = _coerce_dt(_ctx.get("last_snapshot_at"))
        debug["context_last_snapshot_at"] = _last_snap.isoformat() if _last_snap else None
    except Exception:
        logger.exception("debug-run: get_context failed for %s enc=%d", cpmrn, encounter)
        debug["context_last_snapshot_at"] = "error"

    cutoff = last_llm_run_at or _coerce_dt(debug.get("context_last_snapshot_at"))
    debug["delta_cutoff_used"] = cutoff.isoformat() if cutoff else None

    # ── 2. Fresh chart pull with vital-verification diagnostics ──────────────
    try:
        chart = pull_chart(cpmrn, encounter, _debug=debug)
    except Exception as exc:
        logger.exception("debug-run: pull_chart failed for %s enc=%d", cpmrn, encounter)
        debug["pull_chart_error"] = str(exc)
        return {"debug": debug, "pipeline_status": None}

    # Newest verified vital timestamp vs cutoff — the crux of "no new data"
    from tools.radar_sync.delta_extractor import _parse_ts as _dx_parse_ts
    verified_ts = [
        _dx_parse_ts(v.get("timestamp")) for v in (chart.get("vitals") or [])
        if _dx_parse_ts(v.get("timestamp"))
    ]
    latest_verified = max(verified_ts) if verified_ts else None
    debug["latest_verified_vital_ts"] = latest_verified.isoformat() if latest_verified else None
    if latest_verified and cutoff:
        debug["latest_verified_vital_is_after_cutoff"] = latest_verified > cutoff
        debug["vital_to_cutoff_gap_minutes"] = round((cutoff - latest_verified).total_seconds() / 60, 1)

    # ── 3. Store the fresh snapshot so the pipeline writes are consistent ────
    try:
        db.snapshots.insert_one({
            "CPMRN": cpmrn, "encounter": encounter,
            "snapshot_at": snapshot_at, "chart": chart,
        })
    except Exception:
        logger.exception("debug-run: snapshot insert failed for %s enc=%d", cpmrn, encounter)

    # ── 4. Run the live pipeline and capture the full status/gate_trace ──────
    try:
        pipeline_status = _run_live_pipeline(cpmrn, encounter, chart, snapshot_at, db)
    except Exception as exc:
        logger.exception("debug-run: pipeline failed for %s enc=%d", cpmrn, encounter)
        debug["pipeline_error"] = str(exc)
        return {"debug": debug, "pipeline_status": None}

    logger.info("debug-run: ===== single-patient run done for %s enc=%d =====", cpmrn, encounter)
    return {"debug": debug, "pipeline_status": pipeline_status}


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

    _run_documentation_audits()


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


def _run_documentation_audits():
    """
    Hourly sweep: queries the documentation_audit_queue BQ table for problems
    whose audit window has elapsed and that haven't been audited yet, then runs
    the LLM documentation check and writes results to documentation_audit.

    The queue is populated by problem_tracker when it first assesses a problem
    that matches a protocol with an audit section. The sweep never reads GCS —
    all candidate discovery goes through BigQuery.
    """
    try:
        from backend.services.bq_store import get_bq_store
        from backend.services.emr.db import get_db
        from backend.config import GOOGLE_API_KEY
        from backend.services.llm_client import GeminiLLMClient
        from tools.radar_sync.documentation_audit import check_documentation
        from tools.radar_sync.protocol_engine import load_protocols

        bq = get_bq_store()
        db = get_db()
        now = datetime.now(timezone.utc)

        pending = bq.find_pending_audit_queue()
        if not pending:
            return

        protocols = load_protocols(db)
        proto_by_id = {p["protocol_id"]: p for p in protocols}
        client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model="gemini-3.1-flash-lite")
        audited = 0

        for item in pending:
            cpmrn        = item["CPMRN"]
            encounter    = int(item["encounter"] or 0)
            prob_name    = item["problem_name"]
            protocol_id  = item["protocol_id"]
            detected_at  = item["detected_at"]

            proto = proto_by_id.get(protocol_id)
            if not proto or not proto.get("audit"):
                logger.warning(
                    "documentation_audit: queue item references unknown protocol '%s' — skipping",
                    protocol_id,
                )
                continue

            if isinstance(detected_at, str):
                try:
                    detected_at = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=timezone.utc)

            audit_spec = proto["audit"]
            logger.info(
                "documentation_audit: running for %s enc=%d problem='%s' protocol='%s'",
                cpmrn, encounter, prob_name, protocol_id,
            )
            try:
                result = check_documentation(
                    cpmrn, encounter, audit_spec, detected_at, client, db
                )
                bq.insert_documentation_audit({
                    "CPMRN":            cpmrn,
                    "encounter":        encounter,
                    "problem_name":     prob_name,
                    "protocol_id":      protocol_id,
                    "detected_at":      detected_at,
                    "audited_at":       now,
                    "window_hours":     int(audit_spec.get("window_hours") or 12),
                    "verdict":          result.get("verdict"),
                    "note_count":       result.get("note_count", 0),
                    "required_items":   audit_spec.get("required_documentation") or [],
                    "documented_items": result.get("documented_items") or [],
                    "missing_items":    result.get("missing_items") or [],
                })
                audited += 1
                logger.info(
                    "documentation_audit: done for %s enc=%d '%s' — verdict=%s (%d/%d items documented)",
                    cpmrn, encounter, prob_name,
                    result.get("verdict"),
                    len(result.get("documented_items") or []),
                    len(audit_spec.get("required_documentation") or []),
                )
            except Exception:
                logger.exception(
                    "documentation_audit: failed for %s enc=%d '%s'",
                    cpmrn, encounter, prob_name,
                )

        if audited:
            logger.info("documentation_audit sweep: %d problem(s) audited", audited)

    except Exception:
        logger.exception("_run_documentation_audits: sweep crashed")


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
