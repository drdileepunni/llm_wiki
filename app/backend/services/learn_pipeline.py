"""
Learning loop orchestrator.

Given a pre-existing patient timeline, runs the full training pipeline:
  1. Ingest truncated CSV → wiki pages + KGs
  2. Knowledge loop: resolve KGs → assess (10 Qs) → until status=passing
  3. Clinical loop:  run snapshots via run_chat → if new KGs: resolve → repeat

State is persisted as JSON at {kb.wiki_root}/learn_runs/{run_id}.json so the
router can poll it and the UI can display live progress.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

from ..config import KBConfig, WIKI_ROOT
from .assess_pipeline import generate_assessment, run_assessment
from .chat_pipeline import run_chat
from .clinical_assess_pipeline import _parse_answer_txt
from .ingest_pipeline import run_ingest_chunked
from .resolve_service import resolve_all_gaps

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
    return sorted(d.glob("snapshot_*/"), key=lambda p: int(re.search(r"(\d+)", p.name).group(1)))


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


def _update_phase(state: dict, phase: str, kb: KBConfig):
    state["current_phase"] = phase
    _save_run(state, kb)


# ── Clinical loop helper ──────────────────────────────────────────────────────

def _run_clinical_via_chat(slug: str, kb: KBConfig) -> tuple[int, float]:
    """Run each snapshot question through run_chat. Returns (new_kg_count, cost_usd)."""
    snap_dirs = _snapshot_dirs(slug)
    if not snap_dirs:
        log.warning("No snapshot dirs found for %s", slug)
        return 0, 0.0

    total_new_kgs = 0
    total_cost    = 0.0
    for snap_dir in snap_dirs:
        csv_path      = snap_dir / "snapshot.csv"
        question_path = snap_dir / "question.txt"
        answer_path   = snap_dir / "answer.txt"

        if not csv_path.exists():
            log.warning("Missing snapshot.csv in %s — skipping", snap_dir)
            continue

        csv_text  = csv_path.read_text(encoding="utf-8")
        question  = question_path.read_text(encoding="utf-8").strip() if question_path.exists() else ""
        answer_raw = answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
        context   = _parse_answer_txt(answer_raw).get("clinical_context", "")

        prompt = (
            f"{csv_text}\n\n"
            f"CLINICAL CONTEXT:\n{context}\n\n"
            f"Question: {question}"
        )

        try:
            result = run_chat(prompt, kb)
            total_cost += result.get("cost_usd", 0.0)
            if result.get("gap_registered"):
                total_new_kgs += 1
                log.info("Clinical snapshot %s registered new KG", snap_dir.name)
        except Exception as exc:
            log.warning("run_chat failed for %s: %s", snap_dir.name, exc)

    return total_new_kgs, total_cost


# ── Main orchestrator ─────────────────────────────────────────────────────────

def start_learn_run(cpmrn: str, encounter: str, kb: KBConfig, run_id: str | None = None) -> str:
    """
    Launch the full learning loop synchronously (intended to run in a thread).
    Accepts an optional run_id so the router can use the same ID it returned to
    the frontend. Generates one if not provided.
    Returns run_id.
    """
    slug   = f"{cpmrn}_{encounter}"
    if run_id is None:
        run_id = str(uuid.uuid4())[:8]

    state: dict = {
        "run_id":        run_id,
        "cpmrn":         cpmrn,
        "encounter":     encounter,
        "slug":          slug,
        "kb_name":       kb.name,
        "status":        "running",
        "current_phase": "ingesting",
        "log":           [],
        "total_cost_usd": 0.0,
        "started_at":    datetime.utcnow().isoformat(),
        "completed_at":  None,
        "error":         None,
    }
    _save_run(state, kb)

    try:
        # ── PHASE 1: Ingest ───────────────────────────────────────────────────
        timeline_path = _find_timeline(slug)
        timeline_text = timeline_path.read_text(encoding="utf-8")

        ingest_result = run_ingest_chunked(
            source_text=timeline_text,
            source_name=slug,
            citation=f"Patient timeline: {slug}",
            kb=kb,
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

        # ── PHASE 2: Knowledge loop ───────────────────────────────────────────
        for i in range(1, MAX_ITER + 1):
            _update_phase(state, "resolving", kb)

            resolve_stats = resolve_all_gaps(kb)
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

            _update_phase(state, "knowledge_assessing", kb)

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

            if assess.get("status") == "passing":
                log.info("Knowledge assessment passing after iter %d", i)
                break

        # ── PHASE 3: Clinical loop ────────────────────────────────────────────
        # Track gap stems already resolved in this run.  If the same gap
        # reappears after being resolved, the wiki has the content but the
        # retriever can't surface it for the clinical question — that's a
        # retrieval plateau, not a knowledge gap. We stop in that case.
        previously_resolved_gaps: set[str] = set()

        def _current_gap_stems() -> set[str]:
            gaps_dir = kb.wiki_dir / "gaps"
            if not gaps_dir.exists():
                return set()
            return {
                f.stem for f in gaps_dir.glob("*.md")
                if not f.stem.startswith("patient-")
            }

        for i in range(1, MAX_ITER + 1):
            _update_phase(state, "clinical_assessing", kb)

            gaps_before             = _current_gap_stems()
            new_kgs, clinical_cost  = _run_clinical_via_chat(slug, kb)
            state["total_cost_usd"] += clinical_cost
            gaps_after              = _current_gap_stems()

            # Gaps registered during this iteration
            new_gap_stems    = gaps_after - gaps_before
            # Gaps that are genuinely new (not resolved in a prior iteration)
            genuinely_new    = new_gap_stems - previously_resolved_gaps
            plateau_reoccur  = new_gap_stems & previously_resolved_gaps

            if plateau_reoccur:
                log.info(
                    "Clinical loop: gap(s) %s reappeared after prior resolution — retrieval plateau, stopping",
                    plateau_reoccur,
                )

            _log_entry(
                state, "clinical_loop",
                f"Iter {i} — {new_kgs} KGs registered, {len(genuinely_new)} genuinely new"
                + (f", {len(plateau_reoccur)} plateau (already resolved)" if plateau_reoccur else ""),
                iteration=i,
                sub_phase="assessing",
                new_kgs=new_kgs,
                genuinely_new=len(genuinely_new),
                plateau=len(plateau_reoccur),
            )
            _save_run(state, kb)

            if not genuinely_new:
                log.info("Clinical loop complete after iter %d (no genuinely new KGs)", i)
                break

            _update_phase(state, "resolving", kb)

            resolve_stats = resolve_all_gaps(kb)
            resolve_cost  = resolve_stats.get("cost_usd", 0.0)
            state["total_cost_usd"] += resolve_cost
            previously_resolved_gaps |= new_gap_stems  # mark as resolved

            _log_entry(
                state, "clinical_loop",
                f"Iter {i} — resolved {resolve_stats['gaps_attempted']} gaps",
                iteration=i,
                sub_phase="resolving",
                gaps_resolved=resolve_stats["gaps_attempted"],
                articles_ingested=resolve_stats["articles_ingested"],
                cost_usd=resolve_cost,
            )
            _save_run(state, kb)

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

    except Exception as exc:
        log.error("Learn run %s failed: %s", run_id, exc, exc_info=True)
        state["status"]        = "error"
        state["current_phase"] = "error"
        state["error"]         = str(exc)
        state["completed_at"]  = datetime.utcnow().isoformat()
        _save_run(state, kb)

    return run_id
