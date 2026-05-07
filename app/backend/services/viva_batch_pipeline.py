"""
Viva batch pipeline.

Automates the full viva loop without human interaction:
  1. Sample N scenarios from the diagnosis × complication catalog
  2. For each scenario: create a session, run every turn automatically
     (pending_orders → place on dummy chart → advance)
  3. After all sessions in an iteration: bulk gap resolution
  4. Track gaps_per_session as the convergence metric
  5. Repeat for up to `iterations` cycles or until converged

State is persisted to {kb.wiki_root}/viva_batch_runs/{run_id}.json so the
router can poll it and the UI can display live progress.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..cancellation import cleanup_run
from ..config import KBConfig
from .emr import (
    VIVA_DUMMY_CPMRN,
    get_dummy_patient,
    place_viva_order,
    reset_dummy_patient_chart,
    upsert_dummy_patient,
)
from .order_gen_pipeline import run_order_generation
from .order_safety_annotator import annotate_recommendations
from .resolve_service import resolve_all_gaps
from .scenario_catalog import ScenarioEntry, sample_scenarios
from .viva_session import create_session, load_session, save_session
from .viva_simulator import simulate_and_write, write_patient_state
from .viva_teacher import generate_first_scenario, generate_next_turn, generate_trajectory

log = logging.getLogger("wiki.viva_batch")


class _Stopped(Exception):
    """Raised when the user cancels a run."""


# ── Convergence thresholds ────────────────────────────────────────────────────

_CONVERGENCE_GAPS_PER_SESSION = 0.5   # below this = converged
_CONVERGENCE_MIN_ITERS = 2             # need at least 2 data points
_CONVERGENCE_PLATEAU_RATIO = 0.10      # < 10% improvement = plateau

# Hard cap on turns per auto-run session (safety net in addition to max_turns)
_MAX_TURNS_SAFETY = 30


# ── Persistence helpers ───────────────────────────────────────────────────────

def _batch_run_path(run_id: str, kb: KBConfig) -> Path:
    d = kb.wiki_root / "viva_batch_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{run_id}.json"


def _load_batch(run_id: str, kb: KBConfig) -> dict:
    p = _batch_run_path(run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No viva batch run {run_id!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save_batch(state: dict, kb: KBConfig) -> None:
    p = _batch_run_path(state["run_id"], kb)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def list_batch_runs(kb: KBConfig) -> list[dict]:
    d = kb.wiki_root / "viva_batch_runs"
    if not d.exists():
        return []
    runs = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            runs.append({
                "run_id":        data["run_id"],
                "status":        data["status"],
                "current_phase": data.get("current_phase"),
                "n_sessions":    data.get("n_sessions"),
                "iterations":    data.get("iterations"),
                "current_iteration": data.get("current_iteration", 0),
                "started_at":    data.get("started_at"),
                "completed_at":  data.get("completed_at"),
                "converged":     data.get("converged", False),
                "total_cost_usd": data.get("total_cost_usd", 0.0),
                "metrics":       data.get("metrics", []),
            })
        except Exception as exc:
            log.warning("Skipping corrupt batch run %s: %s", f.name, exc)
    return runs


# ── State helpers ─────────────────────────────────────────────────────────────

def _log_entry(state: dict, phase: str, message: str, **extra) -> None:
    state.setdefault("log", []).append({
        "phase":     phase,
        "message":   message,
        "timestamp": datetime.utcnow().isoformat(),
        **extra,
    })


def _update_phase(state: dict, phase: str, kb: KBConfig, stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise _Stopped()
    state["current_phase"] = phase
    _save_batch(state, kb)


def _count_pending_gaps(kb: KBConfig) -> int:
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return 0
    return sum(1 for f in gaps_dir.glob("*.md") if not f.stem.startswith("patient-"))


def _check_convergence(metrics: list[dict]) -> bool:
    if len(metrics) < _CONVERGENCE_MIN_ITERS:
        return False
    last = metrics[-1].get("gaps_per_session", 999)
    prev = metrics[-2].get("gaps_per_session", 999)
    if last < _CONVERGENCE_GAPS_PER_SESSION:
        return True
    if prev > 0 and abs(prev - last) / prev < _CONVERGENCE_PLATEAU_RATIO:
        return True
    return False


# ── Shared viva helpers (mirrors viva.py) ────────────────────────────────────

def _dedup_orders(raw_orders: list[dict]) -> list[dict]:
    seen: dict[tuple, int] = {}
    deduped: list[dict] = []
    for order in raw_orders:
        name = (order.get("orderable_name") or "").strip()
        if not name or name == "—":
            order["orderable_name"] = None
            order["confidence"] = "low"
        key = (
            (order.get("order_type") or "").lower(),
            (order.get("orderable_name") or "").lower().strip(),
        )
        if key[1] and key in seen:
            existing = deduped[seen[key]]
            extra_notes = order.get("notes") or ""
            existing_notes = existing.get("notes") or ""
            if extra_notes and extra_notes not in existing_notes:
                existing["notes"] = f"{existing_notes}; {extra_notes}".lstrip("; ")
        else:
            seen[key] = len(deduped)
            deduped.append(order)
    return deduped


def _orders_placed_text(orders: list[dict]) -> str:
    if not orders:
        return "No orders placed this turn."
    lines = ["AI student placed the following orders:"]
    for o in orders:
        name = o.get("orderable_name") or "—"
        t = o.get("order_type", "")
        instr = (o.get("order_details") or {}).get("instructions") or o.get("notes") or ""
        line = f"  • [{t.upper()}] {name}"
        if instr:
            line += f" — {instr}"
        lines.append(line)
    return "\n".join(lines)


def _generate_pending_orders_sync(
    clinical_context: str,
    question: str,
    phase: str,
    difficulty: str,
    cpmrn: str,
    kb: KBConfig,
    model: str | None,
) -> tuple[list[dict], float]:
    """Synchronous version of _generate_pending_orders from viva.py."""
    try:
        from .viva_student_agent import run_viva_student_turn
        snap_result = run_viva_student_turn(
            clinical_context, question, phase, difficulty, cpmrn, kb, model,
        )
        student_snap = snap_result["snapshots"][0]
        all_steps = (
            student_snap.get("immediate_next_steps", []) +
            student_snap.get("monitoring_followup", [])
        )
        cost = student_snap.get("cost_usd", 0.0)
        if not all_steps:
            return [], cost
        try:
            all_steps = annotate_recommendations(all_steps, cpmrn, model)
        except Exception:
            pass
        order_result = run_order_generation(all_steps, cpmrn, "adult", model, kb, None, None)
        raw = order_result.get("orders", [])
        cost += order_result.get("cost_usd", 0.0)
        return _dedup_orders(raw), cost
    except Exception as exc:
        log.warning("_generate_pending_orders_sync failed: %s", exc)
        return [], 0.0


# ── Session creation ──────────────────────────────────────────────────────────

def _create_batch_session(
    session_id: str,
    entry: ScenarioEntry,
    max_turns: int,
    model: str | None,
    kb: KBConfig,
) -> dict:
    """
    Create a new viva session for a batch scenario entry.
    Mirrors start_viva() in viva.py but runs synchronously.
    """
    # Build patient context from existing dummy patient (if any)
    patient_context = ""
    try:
        pt = get_dummy_patient()
        if pt:
            parts = []
            if pt.get("home_meds"):
                parts.append("Home medications: " + ", ".join(pt["home_meds"]))
            if pt.get("diagnoses"):
                parts.append("Known diagnoses: " + ", ".join(pt["diagnoses"]))
            if pt.get("allergies"):
                parts.append("Allergies: " + ", ".join(pt["allergies"]))
            patient_context = "\n".join(parts)
    except Exception:
        pass

    # Ensure dummy patient exists
    try:
        if not get_dummy_patient():
            upsert_dummy_patient({
                "name": "Viva Patient",
                "age_years": 60,
                "gender": "male",
            })
    except Exception as exc:
        log.warning("Could not ensure dummy patient: %s", exc)

    trajectory = generate_trajectory(entry.topic_string, model, patient_context)
    first_scenario = generate_first_scenario(entry.topic_string, trajectory, model, patient_context)

    # Reset chart and seed initial patient state
    try:
        reset_dummy_patient_chart()
    except Exception as exc:
        log.warning("Could not reset dummy patient chart for batch session: %s", exc)

    patient_state = first_scenario.pop("patient_state", {})
    if patient_state:
        try:
            write_patient_state(patient_state, VIVA_DUMMY_CPMRN)
        except Exception as exc:
            log.warning("Could not seed initial patient state: %s", exc)

    session = create_session(
        session_id=session_id,
        topic=entry.topic_string,
        trajectory=trajectory,
        first_scenario=first_scenario,
        max_turns=max_turns,
        model=model or "",
        kb_name=kb.name,
        kb=kb,
    )

    # Tag session as batch-generated for list filtering
    session["source"] = "batch"
    session["diagnosis"] = entry.diagnosis_label
    session["complication"] = entry.complication_label

    # Generate initial pending_orders for turn 1
    pending_orders, pending_cost = _generate_pending_orders_sync(
        first_scenario.get("clinical_context", ""),
        first_scenario.get("question", ""),
        first_scenario.get("phase", "MANAGEMENT"),
        first_scenario.get("difficulty", "MEDIUM"),
        VIVA_DUMMY_CPMRN, kb, model,
    )
    session["pending_orders"] = pending_orders
    session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + pending_cost, 6)
    save_session(session, kb)

    log.info("Batch session %s created for [%s] + [%s]",
             session_id, entry.diagnosis_label, entry.complication_label)
    return session


# ── Auto-run a single session ─────────────────────────────────────────────────

def _auto_run_session(
    session_id: str,
    kb: KBConfig,
    model: str | None,
    stop_event: threading.Event,
) -> dict:
    """
    Drive a single viva session to completion without human interaction.

    Each turn:
      1. Reset dummy chart
      2. Place this turn's pending_orders on the chart
      3. Run simulator
      4. Teacher generates next scenario
      5. Generate next pending_orders
      6. Persist turn record
    Gap resolution is intentionally skipped per-turn; batch pipeline resolves
    all gaps in bulk after each iteration (far more efficient).
    """
    for _attempt in range(_MAX_TURNS_SAFETY):
        if stop_event.is_set():
            raise _Stopped()

        session = load_session(session_id, kb)
        if session["status"] != "active":
            break

        next_scenario = session.get("next_scenario")
        if not next_scenario:
            break

        pending_orders: list[dict] = session.get("pending_orders", [])
        model_used = model or session.get("model") or None
        turn_num = session["current_turn"] + 1

        # ── 1. Reset chart + place this turn's orders ────────────────────────
        try:
            reset_dummy_patient_chart()
        except Exception as exc:
            log.warning("Auto-run %s turn %d: chart reset failed: %s", session_id, turn_num, exc)

        placed_count = 0
        for order in pending_orders:
            try:
                result = place_viva_order(order)
                if not result.get("error"):
                    placed_count += 1
            except Exception as exc:
                log.warning("Auto-run %s turn %d: order placement failed: %s", session_id, turn_num, exc)
        log.info("Auto-run %s turn %d: placed %d/%d orders", session_id, turn_num, placed_count, len(pending_orders))

        # ── 2. Simulator ─────────────────────────────────────────────────────
        simulation_summary = ""
        try:
            sim_result = simulate_and_write(
                session["trajectory"],
                pending_orders,
                VIVA_DUMMY_CPMRN,
                next_scenario.get("clinical_context", ""),
                turn_num,
                session["max_turns"],
                model_used,
            )
            simulation_summary = sim_result.get("summary", "")
            log.info("Auto-run %s turn %d: simulation done — %s", session_id, turn_num, simulation_summary[:80])
        except Exception as exc:
            log.warning("Auto-run %s turn %d: simulation failed: %s", session_id, turn_num, exc)

        # ── 3. Teacher generates next scenario ───────────────────────────────
        student_answer_text = _orders_placed_text(pending_orders)
        teacher_result: dict = {}
        try:
            teacher_result = generate_next_turn(
                session["trajectory"],
                session["turns"],
                student_answer_text,
                turn_num,
                session["max_turns"],
                simulation_summary,
                model_used,
            )
            # Write supplementary notes if any
            if not teacher_result.get("complete"):
                additional_notes = teacher_result["scenario"].pop("additional_notes", [])
                if additional_notes:
                    try:
                        write_patient_state({"notes": additional_notes}, VIVA_DUMMY_CPMRN)
                    except Exception as exc:
                        log.warning("Auto-run %s: could not write additional notes: %s", session_id, exc)
        except Exception as exc:
            log.warning("Auto-run %s turn %d: teacher failed: %s", session_id, turn_num, exc)
            teacher_result = {"complete": True, "outcome": f"Auto-run teacher error: {exc}"}

        complete = teacher_result.get("complete", False) or turn_num >= session["max_turns"]

        # ── 4. Generate pending_orders for next scenario ─────────────────────
        next_pending: list[dict] = []
        pending_cost = 0.0
        if not complete and teacher_result.get("scenario"):
            next_s = teacher_result["scenario"]
            try:
                next_pending, pending_cost = _generate_pending_orders_sync(
                    next_s.get("clinical_context", ""),
                    next_s.get("question", ""),
                    next_s.get("phase", "MANAGEMENT"),
                    next_s.get("difficulty", "MEDIUM"),
                    VIVA_DUMMY_CPMRN, kb, model_used,
                )
            except Exception as exc:
                log.warning("Auto-run %s turn %d: pending orders gen failed: %s", session_id, turn_num, exc)

        # ── 5. Count gaps generated this turn ────────────────────────────────
        # Gaps accumulate in wiki/gaps/ during student assessment calls inside
        # _generate_pending_orders_sync. We track the delta.
        gaps_after_turn = _count_pending_gaps(kb)

        # ── 6. Persist turn ──────────────────────────────────────────────────
        turn_cost = pending_cost
        turn_record = {
            "turn_num":           turn_num,
            "scenario":           next_scenario,
            "chat_run_id":        None,
            "order_run_id":       None,
            "student_answer_text": student_answer_text,
            "student_snap":       {},
            "orders":             pending_orders,
            "simulation_summary": simulation_summary,
            "gaps_resolved":      0,   # batch resolves gaps in bulk after all sessions
            "cost_usd":           turn_cost,
        }
        session["turns"].append(turn_record)
        session["current_turn"] = turn_num
        session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + turn_cost, 6)

        if complete:
            session["status"] = "complete"
            session["outcome"] = teacher_result.get("outcome", "Case concluded.")
            session["next_scenario"] = None
            session["pending_orders"] = []
        else:
            session["next_scenario"] = teacher_result.get("scenario")
            session["pending_orders"] = next_pending

        save_session(session, kb)

        if complete:
            log.info("Auto-run %s complete after %d turns", session_id, turn_num)
            break

    return load_session(session_id, kb)


# ── Batch orchestrator ────────────────────────────────────────────────────────

def start_viva_batch(
    n_sessions: int,
    mode: str,
    iterations: int,
    max_turns: int,
    model: str | None,
    kb: KBConfig,
    run_id: str,
    stop_event: threading.Event,
    seed: int | None = None,
    diagnosis_filter: str | None = None,
) -> str:
    """
    Synchronous batch orchestrator — call via asyncio.to_thread.

    Runs `iterations` cycles of:
      A. Sample n_sessions scenarios from catalog
      B. Create + auto-run each session
      C. Bulk gap resolution
      D. Compute gaps_per_session convergence metric
    """
    state = _init_state(run_id, n_sessions, mode, iterations, max_turns, model, kb, seed, diagnosis_filter)
    _save_batch(state, kb)

    try:
        for iteration in range(1, iterations + 1):
            state["current_iteration"] = iteration

            # ── A. Sample scenarios ──────────────────────────────────────────
            _update_phase(state, f"sampling_scenarios_iter_{iteration}", kb, stop_event)
            iter_seed = None if seed is None else (seed * 31 + iteration) & 0xFFFFFFFF
            entries = sample_scenarios(n_sessions, mode, seed=iter_seed, diagnosis_filter=diagnosis_filter)  # type: ignore[arg-type]
            state["sessions_by_iteration"][str(iteration)] = []
            _log_entry(state, "sampling",
                       f"Iter {iteration}: sampled {len(entries)} scenarios (mode={mode})")
            _save_batch(state, kb)

            # ── B. Create + auto-run each session ────────────────────────────
            gaps_before_iter = _count_pending_gaps(kb)
            session_costs: list[float] = []

            for idx, entry in enumerate(entries):
                if stop_event.is_set():
                    raise _Stopped()

                session_id = f"batch_{run_id}_{iteration}_{idx}"
                _update_phase(
                    state,
                    f"session_{idx + 1}_of_{len(entries)}_iter_{iteration}",
                    kb, stop_event,
                )
                _log_entry(state, "session_start",
                           f"Iter {iteration} session {idx + 1}/{len(entries)}: {entry.topic_string}",
                           iteration=iteration, session_idx=idx, session_id=session_id,
                           diagnosis=entry.diagnosis_label, complication=entry.complication_label)
                _save_batch(state, kb)

                try:
                    _create_batch_session(session_id, entry, max_turns, model, kb)
                    session = _auto_run_session(session_id, kb, model, stop_event)
                    sess_cost = session.get("total_cost_usd", 0.0)
                    session_costs.append(sess_cost)
                    state["sessions_by_iteration"][str(iteration)].append({
                        "session_id":   session_id,
                        "diagnosis":    entry.diagnosis_label,
                        "complication": entry.complication_label,
                        "turns":        session.get("current_turn", 0),
                        "cost_usd":     round(sess_cost, 6),
                    })
                    state["total_cost_usd"] = round(
                        state["total_cost_usd"] + sess_cost, 6
                    )
                    _log_entry(state, "session_done",
                               f"Session {idx + 1} complete: "
                               f"{session.get('current_turn', 0)} turns, "
                               f"${sess_cost:.4f}",
                               session_id=session_id, iteration=iteration)
                except _Stopped:
                    raise
                except Exception as exc:
                    log.warning("Batch %s iter %d session %d failed: %s", run_id, iteration, idx, exc)
                    _log_entry(state, "session_error",
                               f"Session {idx + 1} failed: {exc}",
                               iteration=iteration, session_idx=idx)

                _save_batch(state, kb)

            # ── C. Bulk gap resolution ────────────────────────────────────────
            _update_phase(state, f"resolving_gaps_iter_{iteration}", kb, stop_event)
            gaps_before_resolve = _count_pending_gaps(kb)
            _log_entry(state, "resolve_start",
                       f"Iter {iteration}: resolving {gaps_before_resolve} pending gaps",
                       iteration=iteration)
            _save_batch(state, kb)

            try:
                gap_stats = resolve_all_gaps(kb)
                resolve_cost = gap_stats.get("cost_usd", 0.0)
                state["total_cost_usd"] = round(state["total_cost_usd"] + resolve_cost, 6)
            except Exception as exc:
                log.warning("Batch %s iter %d: gap resolution failed: %s", run_id, iteration, exc)
                resolve_cost = 0.0

            gaps_after_resolve = _count_pending_gaps(kb)
            gaps_resolved_count = max(0, gaps_before_resolve - gaps_after_resolve)

            # ── D. Compute convergence metric ─────────────────────────────────
            gaps_generated = max(0, gaps_before_resolve - gaps_before_iter)
            session_count = len(state["sessions_by_iteration"].get(str(iteration), []))
            gaps_per_session = gaps_generated / max(session_count, 1)

            metrics_entry = {
                "iteration":          iteration,
                "sessions_run":       session_count,
                "gaps_generated":     gaps_generated,
                "gaps_resolved":      gaps_resolved_count,
                "gaps_per_session":   round(gaps_per_session, 2),
                "session_cost_usd":   round(sum(session_costs), 6),
                "resolve_cost_usd":   round(resolve_cost, 6),
                "total_cost_usd":     round(sum(session_costs) + resolve_cost, 6),
            }
            state["metrics"].append(metrics_entry)

            _log_entry(state, "iteration_done",
                       f"Iter {iteration} done: {gaps_per_session:.1f} gaps/session, "
                       f"{gaps_resolved_count} gaps resolved",
                       **metrics_entry)

            if _check_convergence(state["metrics"]):
                state["converged"] = True
                _log_entry(state, "converged",
                           f"Convergence detected after iteration {iteration}: "
                           f"{gaps_per_session:.2f} gaps/session")
                _save_batch(state, kb)
                break

            _save_batch(state, kb)

        state["status"] = "complete"
        state["current_phase"] = "complete"
        state["completed_at"] = datetime.utcnow().isoformat()
        _save_batch(state, kb)

    except _Stopped:
        state["status"] = "stopped"
        state["current_phase"] = "stopped"
        state["completed_at"] = datetime.utcnow().isoformat()
        _log_entry(state, "stopped", "Batch run cancelled by user")
        _save_batch(state, kb)

    except Exception as exc:
        log.error("Batch run %s failed: %s", run_id, exc, exc_info=True)
        state["status"] = "error"
        state["current_phase"] = "error"
        state["error"] = str(exc)
        state["completed_at"] = datetime.utcnow().isoformat()
        _save_batch(state, kb)

    finally:
        cleanup_run(run_id)

    return run_id


def continue_viva_batch(
    run_id: str,
    additional_iterations: int,
    kb: KBConfig,
    stop_event: threading.Event,
) -> str:
    """
    Extend a completed or stopped batch run with additional iterations.
    Reuses all settings (n_sessions, mode, max_turns, model, seed) stored in the state.
    """
    state = _load_batch(run_id, kb)
    if state.get("status") == "running":
        raise ValueError("Batch run is already running")

    prev_iterations = state["iterations"]
    new_total = prev_iterations + additional_iterations
    state["iterations"] = new_total
    state["status"] = "running"
    state["converged"] = False
    state["completed_at"] = None
    state["error"] = None
    state["current_phase"] = "initializing"
    _log_entry(state, "extended",
               f"Run extended: +{additional_iterations} iterations (new total: {new_total})")
    _save_batch(state, kb)

    n_sessions       = state["n_sessions"]
    mode             = state["mode"]
    max_turns        = state["max_turns_per_session"]
    model            = state.get("model")
    seed             = state.get("seed")
    diagnosis_filter = state.get("diagnosis_filter")
    start_iter       = state["current_iteration"] + 1

    try:
        for iteration in range(start_iter, new_total + 1):
            state["current_iteration"] = iteration

            _update_phase(state, f"sampling_scenarios_iter_{iteration}", kb, stop_event)
            iter_seed = None if seed is None else (seed * 31 + iteration) & 0xFFFFFFFF
            entries = sample_scenarios(n_sessions, mode, seed=iter_seed, diagnosis_filter=diagnosis_filter)  # type: ignore[arg-type]
            state["sessions_by_iteration"][str(iteration)] = []
            _log_entry(state, "sampling",
                       f"Iter {iteration}: sampled {len(entries)} scenarios (mode={mode})")
            _save_batch(state, kb)

            gaps_before_iter = _count_pending_gaps(kb)
            session_costs: list[float] = []

            for idx, entry in enumerate(entries):
                if stop_event.is_set():
                    raise _Stopped()

                session_id = f"batch_{run_id}_{iteration}_{idx}"
                _update_phase(
                    state,
                    f"session_{idx + 1}_of_{len(entries)}_iter_{iteration}",
                    kb, stop_event,
                )
                _log_entry(state, "session_start",
                           f"Iter {iteration} session {idx + 1}/{len(entries)}: {entry.topic_string}",
                           iteration=iteration, session_idx=idx, session_id=session_id,
                           diagnosis=entry.diagnosis_label, complication=entry.complication_label)
                _save_batch(state, kb)

                try:
                    _create_batch_session(session_id, entry, max_turns, model, kb)
                    session = _auto_run_session(session_id, kb, model, stop_event)
                    sess_cost = session.get("total_cost_usd", 0.0)
                    session_costs.append(sess_cost)
                    state["sessions_by_iteration"][str(iteration)].append({
                        "session_id":   session_id,
                        "diagnosis":    entry.diagnosis_label,
                        "complication": entry.complication_label,
                        "turns":        session.get("current_turn", 0),
                        "cost_usd":     round(sess_cost, 6),
                    })
                    state["total_cost_usd"] = round(
                        state["total_cost_usd"] + sess_cost, 6
                    )
                    _log_entry(state, "session_done",
                               f"Session {idx + 1} complete: "
                               f"{session.get('current_turn', 0)} turns, "
                               f"${sess_cost:.4f}",
                               session_id=session_id, iteration=iteration)
                except _Stopped:
                    raise
                except Exception as exc:
                    log.warning("Batch %s iter %d session %d failed: %s", run_id, iteration, idx, exc)
                    _log_entry(state, "session_error",
                               f"Session {idx + 1} failed: {exc}",
                               iteration=iteration, session_idx=idx)

                _save_batch(state, kb)

            _update_phase(state, f"resolving_gaps_iter_{iteration}", kb, stop_event)
            gaps_before_resolve = _count_pending_gaps(kb)
            _log_entry(state, "resolve_start",
                       f"Iter {iteration}: resolving {gaps_before_resolve} pending gaps",
                       iteration=iteration)
            _save_batch(state, kb)

            try:
                gap_stats = resolve_all_gaps(kb)
                resolve_cost = gap_stats.get("cost_usd", 0.0)
                state["total_cost_usd"] = round(state["total_cost_usd"] + resolve_cost, 6)
            except Exception as exc:
                log.warning("Batch %s iter %d: gap resolution failed: %s", run_id, iteration, exc)
                resolve_cost = 0.0

            gaps_after_resolve = _count_pending_gaps(kb)
            gaps_resolved_count = max(0, gaps_before_resolve - gaps_after_resolve)

            gaps_generated = max(0, gaps_before_resolve - gaps_before_iter)
            session_count = len(state["sessions_by_iteration"].get(str(iteration), []))
            gaps_per_session = gaps_generated / max(session_count, 1)

            metrics_entry = {
                "iteration":          iteration,
                "sessions_run":       session_count,
                "gaps_generated":     gaps_generated,
                "gaps_resolved":      gaps_resolved_count,
                "gaps_per_session":   round(gaps_per_session, 2),
                "session_cost_usd":   round(sum(session_costs), 6),
                "resolve_cost_usd":   round(resolve_cost, 6),
                "total_cost_usd":     round(sum(session_costs) + resolve_cost, 6),
            }
            state["metrics"].append(metrics_entry)

            _log_entry(state, "iteration_done",
                       f"Iter {iteration} done: {gaps_per_session:.1f} gaps/session, "
                       f"{gaps_resolved_count} gaps resolved",
                       **metrics_entry)

            if _check_convergence(state["metrics"]):
                state["converged"] = True
                _log_entry(state, "converged",
                           f"Convergence detected after iteration {iteration}: "
                           f"{gaps_per_session:.2f} gaps/session")
                _save_batch(state, kb)
                break

            _save_batch(state, kb)

        state["status"] = "complete"
        state["current_phase"] = "complete"
        state["completed_at"] = datetime.utcnow().isoformat()
        _save_batch(state, kb)

    except _Stopped:
        state["status"] = "stopped"
        state["current_phase"] = "stopped"
        state["completed_at"] = datetime.utcnow().isoformat()
        _log_entry(state, "stopped", "Batch run cancelled by user")
        _save_batch(state, kb)

    except Exception as exc:
        log.error("Batch run %s (extend) failed: %s", run_id, exc, exc_info=True)
        state["status"] = "error"
        state["current_phase"] = "error"
        state["error"] = str(exc)
        state["completed_at"] = datetime.utcnow().isoformat()
        _save_batch(state, kb)

    finally:
        cleanup_run(run_id)

    return run_id


def _init_state(
    run_id: str,
    n_sessions: int,
    mode: str,
    iterations: int,
    max_turns: int,
    model: str | None,
    kb: KBConfig,
    seed: int | None,
    diagnosis_filter: str | None = None,
) -> dict:
    return {
        "run_id":              run_id,
        "kb_name":             kb.name,
        "status":              "running",
        "current_phase":       "initializing",
        "started_at":          datetime.utcnow().isoformat(),
        "completed_at":        None,
        "error":               None,
        # Config
        "n_sessions":          n_sessions,
        "mode":                mode,
        "iterations":          iterations,
        "max_turns_per_session": max_turns,
        "model":               model,
        "seed":                seed,
        "diagnosis_filter":    diagnosis_filter,
        # Progress
        "current_iteration":   0,
        "sessions_by_iteration": {},
        # Metrics
        "metrics":             [],
        "converged":           False,
        # Cost
        "total_cost_usd":      0.0,
        # Log
        "log":                 [],
    }
