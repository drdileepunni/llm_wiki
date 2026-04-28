"""
Learning loop orchestrator.

Given a pre-existing patient timeline, runs the full training pipeline:
  1. Ingest truncated CSV → wiki pages + KGs
  2. Knowledge loop: resolve KGs → assess (10 Qs) → until status=passing

Clinical assessment is a separate manual step (see clinical_assess_pipeline.py).

State is persisted as JSON at {kb.wiki_root}/learn_runs/{run_id}.json so the
router can poll it and the UI can display live progress.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from ..cancellation import cleanup_run, get_resume_event, get_stop_event
from ..config import KBConfig, WIKI_ROOT
from .assess_pipeline import generate_assessment, run_assessment
from .chat_pipeline import run_chat
from .ingest_pipeline import run_ingest_chunked
from .resolve_service import resolve_all_gaps


class _Stopped(Exception):
    """Raised when the user cancels a run."""

log = logging.getLogger("wiki.learn_pipeline")

MAX_ITER = 3  # hard cap per loop — remaining KGs require human resolution


# ── Persistence helpers ───────────────────────────────────────────────────────

def _run_path(run_id: str, kb: KBConfig) -> Path:
    d = kb.wiki_root / "learn_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.json"


def _load_run(run_id: str, kb: KBConfig) -> dict:
    p = _run_path(run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No learn run {run_id!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_run(state: dict, kb: KBConfig):
    p = _run_path(state["run_id"], kb)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def list_learn_runs(kb: KBConfig) -> list[dict]:
    d = kb.wiki_root / "learn_runs"
    if not d.exists():
        return []
    runs = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            runs.append({
                "run_id":       data["run_id"],
                "cpmrn":        data["cpmrn"],
                "encounter":    data["encounter"],
                "slug":         data["slug"],
                "status":       data["status"],
                "current_phase": data.get("current_phase"),
                "started_at":   data.get("started_at"),
                "completed_at": data.get("completed_at"),
                "total_cost_usd": data.get("total_cost_usd", 0.0),
            })
        except Exception as exc:
            log.warning("Skipping corrupt run %s: %s", f.name, exc)
    return runs


# ── Timeline helpers ──────────────────────────────────────────────────────────

def _timeline_dir(slug: str) -> Path:
    return WIKI_ROOT / "timelines" / slug


def _find_timeline(slug: str) -> Path:
    d = _timeline_dir(slug)
    p = d / f"{slug}_timeline_truncated.csv"
    if not p.exists():
        raise FileNotFoundError(
            f"Timeline not found: {p}\n"
            f"Run: python -m tools.patient_timeline {slug.replace('_', '/', 1)}"
        )
    return p


def _snapshot_dirs(slug: str) -> list[Path]:
    d = _timeline_dir(slug)
    dirs = [p for p in d.glob("*snapshot*") if p.is_dir()]
    def _sort_key(p):
        m = re.search(r"(\d+)", p.name)
        return int(m.group(1)) if m else 0
    return sorted(dirs, key=_sort_key)


# ── Log entry helpers ─────────────────────────────────────────────────────────

def _log_entry(state: dict, phase: str, message: str, **kwargs) -> None:
    entry = {
        "phase": phase,
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
        **kwargs,
    }
    state["log"].append(entry)
    log.info("[%s] %s", phase, message)


def _update_phase(state: dict, phase: str, kb: KBConfig, stop_event=None):
    state["current_phase"] = phase
    _save_run(state, kb)
    if stop_event and stop_event.is_set():
        raise _Stopped()





# ── Resume helper ────────────────────────────────────────────────────────────

def _resume_iteration(state: dict) -> int:
    """
    Infer which knowledge-loop iteration to start from when resuming a stopped run.
    Returns the iteration number (1-based).
    - If stopped mid-assess on iter N → resume from iter N (re-assess)
    - If stopped mid-resolve on iter N → resume from iter N (gap files are source of truth)
    - If no iteration logged yet → start from iter 1
    """
    iterations_seen: dict[int, set] = {}
    for entry in state.get("log", []):
        i = entry.get("iteration")
        if i is not None:
            iterations_seen.setdefault(i, set()).add(entry.get("sub_phase", ""))

    if not iterations_seen:
        return 1

    last_iter = max(iterations_seen)
    phases_done = iterations_seen[last_iter]

    # If both resolve and assess completed for last_iter, move to next
    if "resolving" in phases_done and "assessing" in phases_done:
        return last_iter + 1

    # Otherwise re-run the current iter (gap files already reflect partial progress)
    return last_iter


# ── Main orchestrator ─────────────────────────────────────────────────────────

def start_learn_run(cpmrn: str, encounter: str, kb: KBConfig, run_id: str | None = None, stop_event=None, num_snapshots: int = 2, review_questions: bool = True) -> str:
    """
    Launch the full learning loop synchronously (intended to run in a thread).
    Accepts an optional run_id so the router can use the same ID it returned to
    the frontend. Generates one if not provided.
    Returns run_id.
    """
    slug   = f"{cpmrn}_{encounter}"
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]

    if stop_event is None:
        stop_event = get_stop_event(run_id)

    def _check_stop():
        if stop_event.is_set():
            raise _Stopped()

    state: dict = {
        "run_id":        run_id,
        "cpmrn":         cpmrn,
        "encounter":     encounter,
        "slug":          slug,
        "kb_name":       kb.name,
        "status":        "running",
        "current_phase": "exporting",
        "log":           [],
        "total_cost_usd": 0.0,
        "started_at":    datetime.utcnow().isoformat(),
        "completed_at":  None,
        "error":         None,
    }
    _save_run(state, kb)

    try:
        # ── PHASE 0: Export + snapshot generation (skipped if already exists) ─
        timeline_dir = _timeline_dir(slug)
        trunc_csv    = timeline_dir / f"{slug}_timeline_truncated.csv"
        snap1_dir    = timeline_dir / "snapshot_1"

        if not trunc_csv.exists() or not snap1_dir.exists():
            _update_phase(state, "exporting", kb, stop_event)
            _log_entry(state, "exporting", f"Generating timeline and snapshots for {slug} …")
            _save_run(state, kb)
            _check_stop()

            import sys as _sys
            _repo_root = str(Path(__file__).resolve().parents[3])
            if _repo_root not in _sys.path:
                _sys.path.insert(0, _repo_root)
            from tools.patient_timeline import generate as pt_generate
            pt_generate(
                f"{cpmrn}/{encounter}",
                num_snapshots=num_snapshots,
            )
            _log_entry(state, "exporting", f"Timeline and {num_snapshots} snapshots ready for {slug}")
            _save_run(state, kb)
            _check_stop()
        else:
            _log_entry(state, "exporting", f"Timeline and snapshots already exist for {slug} — skipping export")
            _save_run(state, kb)

        # ── PHASE 1: Ingest ───────────────────────────────────────────────────
        timeline_path = _find_timeline(slug)
        timeline_text = timeline_path.read_text(encoding="utf-8")

        _log_entry(state, "ingesting", f"Ingesting timeline for {slug}…")
        _save_run(state, kb)
        _check_stop()

        ingest_result = run_ingest_chunked(
            source_text=timeline_text,
            source_name=slug,
            citation=f"Patient timeline: {slug}",
            kb=kb,
            patient_dir=slug,
        )
        ingest_cost = ingest_result.get("cost_usd", 0.0)
        state["total_cost_usd"] += ingest_cost

        _log_entry(
            state, "ingesting",
            f"Ingested {slug} — {len(ingest_result.get('files_written', []))} pages, "
            f"{len(ingest_result.get('knowledge_gaps', []))} KGs",
            pages_written=len(ingest_result.get("files_written", [])),
            kgs_found=len(ingest_result.get("knowledge_gaps", [])),
            cost_usd=ingest_cost,
        )

        # Generate knowledge assessment (synchronously)
        try:
            gen_result = generate_assessment(
                source_slug=slug,
                source_name=slug,
                ingest_summary=ingest_result.get("summary", ""),
                knowledge_gaps=ingest_result.get("knowledge_gaps", []),
                files_written=ingest_result.get("files_written", []),
                kb=kb,
            )
            state["total_cost_usd"] += gen_result.get("cost_usd", 0.0)
            log.info("Assessment generated for %s", slug)
        except Exception as exc:
            log.warning("generate_assessment failed: %s", exc)

        _save_run(state, kb)
        _check_stop()

        # ── QUESTION REVIEW GATE ──────────────────────────────────────────────
        if review_questions:
            # Load the just-generated questions into run state so the UI can show them
            try:
                from .assess_pipeline import load_assessment
                assess_data = load_assessment(slug, kb)
                state["pending_questions"] = [
                    {"id": q["id"], "question": q["question"], "rationale": q.get("rationale", ""), "linked_kgs": q.get("linked_kgs", [])}
                    for q in assess_data.get("questions", [])
                ]
            except Exception as exc:
                log.warning("Could not load questions for review gate: %s", exc)
                state["pending_questions"] = []

            _update_phase(state, "pending_review", kb, stop_event)
            _log_entry(state, "pending_review", f"Waiting for question review — {len(state.get('pending_questions', []))} questions ready")
            _save_run(state, kb)

            # Block until user approves (or stop is requested)
            resume_event = get_resume_event(run_id)
            while not resume_event.is_set():
                if stop_event.is_set():
                    raise _Stopped()
                resume_event.wait(timeout=2.0)

            # Persist any edits the user made (router writes them to state before setting resume_event)
            _update_phase(state, "resolving", kb, stop_event)
            state.pop("pending_questions", None)
            _save_run(state, kb)
            _check_stop()

        # ── PHASE 2: Knowledge loop ───────────────────────────────────────────
        for i in range(1, MAX_ITER + 1):
            _update_phase(state, "resolving", kb, stop_event)

            def _resolve_progress(idx, total, gap_title, articles, cost, _i=i):
                _log_entry(state, "knowledge_loop",
                           f"Iter {_i} — gap {idx}/{total}: {gap_title} ({articles} articles)",
                           iteration=_i, sub_phase="resolving", cost_usd=cost)
                _save_run(state, kb)
                if stop_event.is_set():
                    raise _Stopped()

            resolve_stats = resolve_all_gaps(kb, progress_callback=_resolve_progress)
            resolve_cost  = resolve_stats.get("cost_usd", 0.0)
            state["total_cost_usd"] += resolve_cost

            _log_entry(
                state, "knowledge_loop",
                f"Iter {i} — resolved {resolve_stats['gaps_attempted']} gaps, "
                f"ingested {resolve_stats['articles_ingested']} articles",
                iteration=i,
                sub_phase="resolving",
                gaps_resolved=resolve_stats["gaps_attempted"],
                articles_ingested=resolve_stats["articles_ingested"],
                cost_usd=resolve_cost,
            )
            _save_run(state, kb)
            _check_stop()

            _update_phase(state, "knowledge_assessing", kb, stop_event)
            _log_entry(state, "knowledge_loop", f"Iter {i} — running knowledge assessment…",
                       iteration=i, sub_phase="assessing")
            _save_run(state, kb)
            _check_stop()

            try:
                assess = run_assessment(slug, kb)
            except FileNotFoundError:
                log.warning("Assessment file missing for %s — skipping knowledge loop", slug)
                break

            new_kgs = sum(
                len(q["runs"][-1].get("new_kgs_registered", []))
                for q in assess.get("questions", [])
                if q.get("runs")
            )
            assess_cost = sum(
                q["runs"][-1].get("cost_usd", 0.0)
                for q in assess.get("questions", [])
                if q.get("runs")
            )
            state["total_cost_usd"] += assess_cost

            _log_entry(
                state, "knowledge_loop",
                f"Iter {i} — assessment {assess.get('status')} · {new_kgs} new KGs",
                iteration=i,
                sub_phase="assessing",
                assessment_status=assess.get("status"),
                new_kgs=new_kgs,
                cost_usd=assess_cost,
            )
            _save_run(state, kb)
            _check_stop()

            if assess.get("status") == "passing":
                log.info("Knowledge assessment passing after iter %d", i)
                break

        # ── Done ──────────────────────────────────────────────────────────────
        state["status"]        = "complete"
        state["current_phase"] = "complete"
        state["completed_at"]  = datetime.utcnow().isoformat()
        _log_entry(
            state, "complete",
            f"Learning loop complete — total cost ${state['total_cost_usd']:.4f}",
        )
        _save_run(state, kb)
        log.info("Learn run %s complete for %s", run_id, slug)

    except _Stopped:
        log.info("Learn run %s stopped by user at phase=%s", run_id, state.get("current_phase"))
        state["status"]        = "stopped"
        state["current_phase"] = "stopped"
        state["completed_at"]  = datetime.utcnow().isoformat()
        _log_entry(state, "stopped", f"Run stopped by user — cost so far ${state['total_cost_usd']:.4f}")
        _save_run(state, kb)

    except Exception as exc:
        log.error("Learn run %s failed: %s", run_id, exc, exc_info=True)
        state["status"]        = "error"
        state["current_phase"] = "error"
        state["error"]         = str(exc)
        state["completed_at"]  = datetime.utcnow().isoformat()
        _save_run(state, kb)

    finally:
        cleanup_run(run_id)

    return run_id


def resume_learn_run(run_id: str, kb: KBConfig, stop_event=None) -> str:
    """
    Resume a stopped/error learn run from the knowledge-loop iteration where it stopped.
    Ingest and question-review phases are skipped (assumed already done).
    Appends to the existing run log and preserves accumulated cost.
    """
    state = _load_run(run_id, kb)

    if state.get("status") not in ("stopped", "error"):
        raise ValueError(f"Run {run_id!r} is not stopped (status={state.get('status')!r})")

    if stop_event is None:
        stop_event = get_stop_event(run_id)

    slug = state["slug"]
    start_iter = _resume_iteration(state)

    def _check_stop():
        if stop_event.is_set():
            raise _Stopped()

    state["status"] = "running"
    state["current_phase"] = "resolving"
    state["completed_at"] = None
    state["error"] = None
    _log_entry(state, "resumed", f"Resuming from knowledge loop iter {start_iter} — cost so far ${state['total_cost_usd']:.4f}")
    _save_run(state, kb)

    try:
        for i in range(start_iter, MAX_ITER + 1):
            _update_phase(state, "resolving", kb, stop_event)

            def _resolve_progress(idx, total, gap_title, articles, cost, _i=i):
                _log_entry(state, "knowledge_loop",
                           f"Iter {_i} — gap {idx}/{total}: {gap_title} ({articles} articles)",
                           iteration=_i, sub_phase="resolving", cost_usd=cost)
                _save_run(state, kb)
                if stop_event.is_set():
                    raise _Stopped()

            resolve_stats = resolve_all_gaps(kb, progress_callback=_resolve_progress)
            resolve_cost  = resolve_stats.get("cost_usd", 0.0)
            state["total_cost_usd"] += resolve_cost

            _log_entry(
                state, "knowledge_loop",
                f"Iter {i} — resolved {resolve_stats['gaps_attempted']} gaps, "
                f"ingested {resolve_stats['articles_ingested']} articles",
                iteration=i, sub_phase="resolving",
                gaps_resolved=resolve_stats["gaps_attempted"],
                articles_ingested=resolve_stats["articles_ingested"],
                cost_usd=resolve_cost,
            )
            _save_run(state, kb)
            _check_stop()

            _update_phase(state, "knowledge_assessing", kb, stop_event)
            _log_entry(state, "knowledge_loop", f"Iter {i} — running knowledge assessment…",
                       iteration=i, sub_phase="assessing")
            _save_run(state, kb)
            _check_stop()

            try:
                assess = run_assessment(slug, kb)
            except FileNotFoundError:
                log.warning("Assessment file missing for %s — skipping knowledge loop", slug)
                break

            new_kgs = sum(
                len(q["runs"][-1].get("new_kgs_registered", []))
                for q in assess.get("questions", [])
                if q.get("runs")
            )
            assess_cost = sum(
                q["runs"][-1].get("cost_usd", 0.0)
                for q in assess.get("questions", [])
                if q.get("runs")
            )
            state["total_cost_usd"] += assess_cost

            _log_entry(
                state, "knowledge_loop",
                f"Iter {i} — assessment {assess.get('status')} · {new_kgs} new KGs",
                iteration=i, sub_phase="assessing",
                assessment_status=assess.get("status"),
                new_kgs=new_kgs,
                cost_usd=assess_cost,
            )
            _save_run(state, kb)
            _check_stop()

            if assess.get("status") == "passing":
                log.info("Knowledge assessment passing after iter %d", i)
                break

        state["status"] = "complete"
        state["current_phase"] = "complete"
        state["completed_at"] = datetime.utcnow().isoformat()
        _log_entry(state, "complete",
                   f"Learning loop complete — total cost ${state['total_cost_usd']:.4f}")
        _save_run(state, kb)
        log.info("Learn run %s resumed and completed for %s", run_id, slug)

    except _Stopped:
        log.info("Resumed run %s stopped again at phase=%s", run_id, state.get("current_phase"))
        state["status"] = "stopped"
        state["current_phase"] = "stopped"
        state["completed_at"] = datetime.utcnow().isoformat()
        _log_entry(state, "stopped", f"Run stopped — cost so far ${state['total_cost_usd']:.4f}")
        _save_run(state, kb)

    except Exception as exc:
        log.error("Resumed run %s failed: %s", run_id, exc, exc_info=True)
        state["status"] = "error"
        state["current_phase"] = "error"
        state["error"] = str(exc)
        state["completed_at"] = datetime.utcnow().isoformat()
        _save_run(state, kb)

    finally:
        cleanup_run(run_id)

    return run_id
