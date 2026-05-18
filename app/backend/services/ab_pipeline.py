"""
A/B batch pipeline — Wiki-Grounded (Arm A) vs MedGemma-Only (Arm B).

Architecture mirrors viva_batch_pipeline.py:
  • Uses the same teacher, simulator, and patient chart infrastructure
  • Two CDS arms run per turn (instead of the viva student agent)
  • Arm A's structured_orders drive the simulator and clinical trajectory
  • Both arms always see the same patient state / question each turn
  • Uses AB_DUMMY_CPMRN — completely isolated from the Viva patient chart

Entry point:
  run_ab_batch(n_scenarios, kb, ..., on_progress=callback)

on_progress(case_dict) is called after each turn completes so callers can
stream live progress without waiting for the full batch to finish.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import KBConfig
from .emr import (
    AB_DUMMY_CPMRN,
    get_ab_patient,
    reset_ab_patient_chart,
    upsert_ab_patient,
)
from .order_gen_pipeline import run_order_generation
from .scenario_catalog import DIAGNOSIS_GROUPS, ScenarioEntry, sample_scenarios
from .viva_simulator import simulate_and_write, write_patient_state, get_result_delay
from .viva_teacher import generate_first_scenario, generate_next_turn, generate_trajectory

log = logging.getLogger("wiki.ab_pipeline")

# ── AB patient defaults ────────────────────────────────────────────────────────
# A generic adult ICU patient — same demographic profile as VIVA_DUMMY but
# on a separate CPMRN so the two pipelines never share chart state.
_AB_PATIENT_DEFAULTS = {
    "name":       "AB Test Patient",
    "age_years":  55,
    "gender":     "male",
    "weight_kg":  70,
    "height_cm":  170,
    "diagnoses":  ["Type 2 Diabetes", "Hypertension", "CKD Stage 3"],
    "home_meds":  ["Metformin 500 mg BD", "Amlodipine 5 mg OD", "Furosemide 40 mg OD"],
    "allergies":  [],
    "creatinine": "1.4",
    "egfr":       "52",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ensure_ab_patient() -> dict:
    """Create the AB patient if it doesn't exist yet; return its profile."""
    pt = get_ab_patient()
    if not pt:
        log.info("AB patient not found — creating with defaults")
        pt = upsert_ab_patient(_AB_PATIENT_DEFAULTS)
    return pt or {}


def _get_patient_context(pt: dict) -> str:
    """Build a concise patient background string for the teacher prompt."""
    parts = []
    if pt.get("home_meds"):
        parts.append("Home medications: " + ", ".join(pt["home_meds"]))
    if pt.get("diagnoses"):
        parts.append("Known diagnoses: " + ", ".join(pt["diagnoses"]))
    if pt.get("allergies"):
        parts.append("Allergies: " + ", ".join(pt["allergies"]))
    if pt.get("weight_kg"):
        parts.append(f"Weight: {pt['weight_kg']} kg")
    return "\n".join(parts)


def _orders_placed_text(orders: list[dict]) -> str:
    """Format placed orders as the 'student answer' for the teacher."""
    if not orders:
        return "Standard ICU management orders placed per clinical guidelines."
    lines = ["Orders placed this turn:"]
    for o in orders:
        name = o.get("orderable_name") or o.get("recommendation") or "—"
        t = o.get("order_type", "")
        det = o.get("order_details") or {}
        instr = det.get("instructions") or o.get("notes") or ""
        line = f"  • [{t.upper()}] {name}"
        if instr:
            line += f" — {instr}"
        lines.append(line)
    return "\n".join(lines)


def _build_question(scenario: dict, sim_summary: str, history: list | None = None) -> str:
    """Assemble the CDS question string from the current scenario + simulation state."""
    parts = []

    if history:
        prior_lines = []
        for h in history:
            prior_lines.append(f"Turn {h['turn_num']}: {h['student_answer_text']}")
        parts.append("Prior interventions (already actioned — do not repeat):\n" + "\n".join(prior_lines))

    ctx = (scenario.get("clinical_context") or "").strip()
    q   = (scenario.get("question") or "").strip()
    if ctx:
        parts.append(f"Clinical context: {ctx}")
    if sim_summary:
        parts.append(f"Current patient state:\n{sim_summary}")
    if q:
        parts.append(f"Question: {q}")
    return "\n\n".join(parts)


# ── CDS arm runner ─────────────────────────────────────────────────────────────

def _run_arm(
    data_brief: str,
    scenario: dict,
    kb: KBConfig,
    grounding_mode: str,
    label: str,
    do_order_gen: bool,
) -> dict:
    """
    Run Phase 2 only for one CDS arm (wiki or medgemma).
    Phase 1 (tool loop / patient data brief) is shared and pre-computed by
    _run_scenario() so both arms see identical patient data.
    """
    from .viva_student_agent import run_cds_from_brief

    t0 = time.time()
    try:
        snap = run_cds_from_brief(
            data_brief=data_brief,
            clinical_context=scenario.get("clinical_context", ""),
            question=scenario.get("question", ""),
            phase=scenario.get("phase", "MANAGEMENT"),
            difficulty=scenario.get("difficulty", "MEDIUM"),
            kb=kb,
            grounding_mode=grounding_mode,
            cpmrn=AB_DUMMY_CPMRN,
        )
        steps = snap.get("immediate_next_steps", [])

        arm: dict = {
            "label":                       label,
            "grounding_mode":              grounding_mode,
            "ok":                          True,
            "elapsed_s":                   round(time.time() - t0, 1),
            "immediate_next_steps":        steps,
            "clinical_direction":          snap.get("clinical_direction", []),
            "clinical_reasoning":          snap.get("clinical_reasoning", []),
            "monitoring_followup":         snap.get("monitoring_followup", []),
            "alternative_considerations":  snap.get("alternative_considerations", []),
            "specific_parameters":         snap.get("specific_parameters", []),
            "pages_consulted":             snap.get("pages_consulted", []),
            "gap_registered":              snap.get("gap_registered"),
            "data_brief":                  snap.get("data_brief", ""),
            "markers_remaining":           sum(
                1 for s in steps
                if "not in wiki" in s.lower() or "consult local protocol" in s.lower()
            ),
            "markers_resolved_by_medgemma": snap.get("markers_resolved_by_medgemma", 0),
            "tokens": {
                "input":  snap.get("tokens_in", 0),
                "output": snap.get("tokens_out", 0),
            },
            "cost_usd":        snap.get("cost_usd", 0.0),
            "model":           "",
            "structured_orders": None,
            "order_gen_run_id":  None,
        }

        if do_order_gen and steps:
            log.info("    → order_gen on %d step(s)…", len(steps))
            t1 = time.time()
            try:
                og = run_order_generation(
                    recommendations=steps,
                    cpmrn=AB_DUMMY_CPMRN,
                    patient_type="adult",
                    kb=kb,
                    parent_run_id=snap.get("chat_run_id"),
                )
                arm["structured_orders"]   = og.get("orders", [])
                arm["order_gen_run_id"]    = og.get("run_id")
                arm["order_gen_elapsed_s"] = round(time.time() - t1, 1)
                arm["order_gen_cost_usd"]  = og.get("cost_usd", 0.0)
                arm["cost_usd"]            = round(arm["cost_usd"] + og.get("cost_usd", 0.0), 6)
                log.info("    order_gen: %d orders  $%.4f  %.1fs",
                         len(arm["structured_orders"]),
                         og.get("cost_usd", 0.0), arm["order_gen_elapsed_s"])
            except Exception as oe:
                log.warning("    order_gen failed (non-fatal): %s", oe)
                arm["order_gen_error"] = str(oe)

        return arm

    except Exception as e:
        log.error("Arm %s failed: %s  grounding_mode=%s", label, e, grounding_mode, exc_info=True)
        return {
            "label":          label,
            "grounding_mode": grounding_mode,
            "ok":             False,
            "error":          str(e),
            "elapsed_s":      round(time.time() - t0, 1),
        }


# ── Per-scenario runner ────────────────────────────────────────────────────────

def _run_scenario(
    entry: ScenarioEntry,
    max_turns: int,
    kb: KBConfig,
    skip_arm_a: bool,
    skip_arm_b: bool,
    do_order_gen: bool,
    model: str | None,
    on_progress: Callable[[dict], None] | None,
) -> list[dict]:
    """
    Run one scenario end-to-end with full simulation loop.

    Turn flow:
      1. Build question from current scenario + simulation state
      2. Run Arm A (wiki) and Arm B (MedGemma) on the same question
      3. Emit progress callback with the completed case
      4. Simulate patient response using Arm A's orders (canonical trajectory driver)
      5. Generate next scenario from teacher using real simulation summary
      6. Repeat
    """
    scenario_id = f"ab_{entry.diagnosis_id}_c{entry.complication_id}"

    # ── Setup patient and generate trajectory ─────────────────────────────────
    pt = _ensure_ab_patient()
    patient_context = _get_patient_context(pt)

    log.info("  Generating trajectory: [%s] + [%s]",
             entry.diagnosis_label, entry.complication_label)
    try:
        trajectory = generate_trajectory(entry.topic_string, model, patient_context)
        first = generate_first_scenario(entry.topic_string, trajectory, model, patient_context)
    except Exception as e:
        log.error("  Failed to generate trajectory/first scenario: %s", e)
        return []

    # ── Reset chart and seed initial patient state ────────────────────────────
    try:
        reset_ab_patient_chart()
    except Exception as e:
        log.warning("  Chart reset failed (non-fatal): %s", e)

    patient_state = first.pop("patient_state", {})
    initial_sim_summary = ""
    patient_history: dict = {}   # static PMH — carried through every turn's sim_raw
    if patient_state:
        try:
            initial_sim_summary = write_patient_state(patient_state, AB_DUMMY_CPMRN)
            log.info("  Seeded initial patient state: %s", initial_sim_summary[:80])
        except Exception as e:
            log.warning("  Could not write initial patient state: %s", e)
        patient_history = {
            "home_medications": patient_state.get("home_medications", []),
            "diagnoses":        patient_state.get("diagnoses", []),
            "allergies":        patient_state.get("allergies", []),
        }

    # ── Turn loop ─────────────────────────────────────────────────────────────
    cases: list[dict] = []
    current_scenario = first
    history: list[dict] = []
    sim_summary = initial_sim_summary   # what the patient looks like at the START of each turn
    # Seed sim_raw from initial patient_state so turn 1 shows actual vitals/labs/notes
    sim_raw: dict = {
        "vitals": patient_state.get("vitals", {}),
        "labs":   patient_state.get("labs", []),
        "notes":  patient_state.get("notes", []),
        "history": patient_history,
    } if patient_state else {}

    # Time tracking state
    session_elapsed_minutes: int = 0
    pending_lab_queue: list[dict] = []

    for turn_num in range(1, max_turns + 1):
        log.info("  ─── Turn %d (phase=%s, diff=%s) ───",
                 turn_num,
                 current_scenario.get("phase", "?"),
                 current_scenario.get("difficulty", "?"))

        # Build the question string (carries narrative history for the LLM)
        display_question = _build_question(current_scenario, sim_summary, history if history else None)
        arm_scenario = dict(current_scenario)
        arm_scenario["question"] = display_question

        # ── Phase 1 (shared): one tool loop for both arms ─────────────────────
        log.info("    Phase 1: collecting patient brief from AB chart…")
        from .viva_student_agent import collect_patient_brief
        from .token_tracker import calculate_cost as _calc_cost
        from ..config import MODEL as _MODEL
        brief_text, brief_in, brief_out = collect_patient_brief(
            clinical_context=current_scenario.get("clinical_context", ""),
            question=display_question,
            cpmrn=AB_DUMMY_CPMRN,
        )
        brief_cost = _calc_cost(brief_in, brief_out, _MODEL)
        log.info("    Phase 1 done: %d chars  in=%d  out=%d  $%.4f",
                 len(brief_text), brief_in, brief_out, brief_cost)

        # ── Phase 2: each arm runs CDS synthesis from the shared brief ────────
        arm_a, arm_b = None, None
        if not skip_arm_a:
            log.info("    Arm A (wiki-grounded)…")
            arm_a = _run_arm(brief_text, arm_scenario, kb, "wiki", "wiki_grounded", do_order_gen)
            log.info("    Arm A: steps=%d  orders=%s  markers=%d  $%.4f  %.1fs",
                     len(arm_a.get("immediate_next_steps", [])),
                     len(arm_a.get("structured_orders") or []),
                     arm_a.get("markers_remaining", 0),
                     arm_a.get("cost_usd", 0),
                     arm_a.get("elapsed_s", 0))

        if not skip_arm_b:
            log.info("    Arm B (MedGemma-only)…")
            arm_b = _run_arm(brief_text, arm_scenario, kb, "medgemma", "medgemma_only", do_order_gen)
            log.info("    Arm B: steps=%d  orders=%s  mg_resolved=%d  %.1fs",
                     len(arm_b.get("immediate_next_steps", [])),
                     len(arm_b.get("structured_orders") or []),
                     arm_b.get("markers_resolved_by_medgemma", 0),
                     arm_b.get("elapsed_s", 0))

        # ── Time delta for this turn ─────────────────────────────────────────
        turn_time_delta: int = current_scenario.get("time_delta_minutes", 60)
        turn_end_minutes: int = session_elapsed_minutes + turn_time_delta

        # Record case ──────────────────────────────────────────────────────────
        case = {
            "scenario_id":              scenario_id,
            "turn_num":                 turn_num,
            "phase":                    current_scenario.get("phase", ""),
            "difficulty":               current_scenario.get("difficulty", ""),
            "topic":                    entry.topic_string,
            "diagnosis":                entry.diagnosis_label,
            "complication":             entry.complication_label,
            "question":                 display_question,
            "display_question":         current_scenario.get("question", "") or current_scenario.get("clinical_context", ""),
            "clinical_context":         current_scenario.get("clinical_context", ""),
            "simulation_summary":       sim_summary,
            "simulation_raw":           sim_raw,
            "patient_brief":            brief_text,
            "phase1_cost_usd":          brief_cost,
            "reference_orders":         [],
            "arm_a":                    arm_a,
            "arm_b":                    arm_b,
            "turn_delta_minutes":       turn_time_delta,
            "session_elapsed_minutes":  turn_end_minutes,
            "vital_timeline":           [],   # filled in after simulation below
        }
        cases.append(case)

        if on_progress:
            try:
                on_progress(case)
            except Exception:
                pass

        if turn_num >= max_turns:
            break

        # Simulate using Arm A's orders (Arm B if Arm A skipped) ──────────────
        driver = arm_a if (not skip_arm_a and arm_a and arm_a.get("ok")) else arm_b
        orders_for_sim = (driver.get("structured_orders") or []) if driver else []

        # Auto-place driver orders into MongoDB so the chart reflects what was actioned
        if orders_for_sim:
            from .emr.patient import place_viva_order
            for order in orders_for_sim:
                try:
                    place_viva_order(order, cpmrn=AB_DUMMY_CPMRN)
                except Exception as oe:
                    log.warning("    Auto-place order failed (non-fatal): %s", oe)
            log.info("    Auto-placed %d order(s) to AB chart", len(orders_for_sim))

        # Build lab queue context for simulator
        available_labs = [e["name"] for e in pending_lab_queue if e["available_at_minutes"] <= turn_end_minutes]
        still_pending  = [e["name"] for e in pending_lab_queue if e["available_at_minutes"] > turn_end_minutes]
        new_queue_entries: list[dict] = []
        for o in orders_for_sim:
            if o.get("order_type") in ("lab", "procedure"):
                name = o.get("orderable_name", "")
                if name:
                    delay = get_result_delay(name)
                    new_queue_entries.append({
                        "name": name,
                        "ordered_at_minutes": turn_end_minutes,
                        "available_at_minutes": turn_end_minutes + delay,
                        "ordered_turn": turn_num,
                    })
        pending_lab_queue = (
            [e for e in pending_lab_queue if e["available_at_minutes"] > turn_end_minutes]
            + new_queue_entries
        )
        log.info("    time_delta=%dm  elapsed=%dm  available_labs=%s  pending=%s",
                 turn_time_delta, turn_end_minutes, available_labs, still_pending)

        try:
            sim_result = simulate_and_write(
                trajectory=trajectory,
                orders=orders_for_sim,
                cpmrn=AB_DUMMY_CPMRN,
                clinical_context=current_scenario.get("clinical_context", ""),
                turn_num=turn_num,
                max_turns=max_turns,
                model=model,
                time_delta_minutes=turn_time_delta,
                available_labs=available_labs or None,
                pending_labs=still_pending or None,
            )
            sim_summary = sim_result.get("summary", "")
            sim_raw     = sim_result.get("raw", {})
            sim_raw["history"] = patient_history   # carry PMH through every turn
            # Backfill vital_timeline into the case that was already appended
            cases[-1]["vital_timeline"] = sim_result.get("vital_timeline", [])
            log.info("    Simulator: %s", sim_summary[:100])
        except Exception as e:
            log.warning("    Simulation failed (non-fatal): %s", e)
            sim_summary = ""
            sim_raw     = {}

        session_elapsed_minutes = turn_end_minutes

        # Advance teacher ──────────────────────────────────────────────────────
        student_answer_text = _orders_placed_text(orders_for_sim)
        history.append({
            "turn_num":            turn_num,
            "scenario":            current_scenario,
            "student_answer_text": student_answer_text,
            "simulation_summary":  sim_summary,
        })

        try:
            teacher_result = generate_next_turn(
                trajectory, history, student_answer_text,
                turn_num, max_turns, sim_summary, model,
            )
        except Exception as e:
            log.warning("    generate_next_turn failed: %s", e)
            break

        if teacher_result.get("complete"):
            log.info("    Teacher signalled case complete after turn %d", turn_num)
            break

        # Write any supplementary notes the teacher added ──────────────────────
        additional_notes = teacher_result.get("scenario", {}).pop("additional_notes", [])
        if additional_notes:
            try:
                write_patient_state({"notes": additional_notes}, AB_DUMMY_CPMRN)
            except Exception:
                pass

        current_scenario = teacher_result.get("scenario", {})

    log.info("  Scenario %s complete — %d turns", scenario_id, len(cases))
    return cases


# ── MedGemma health check ──────────────────────────────────────────────────────

def _check_medgemma(url: str) -> bool:
    try:
        import httpx
        from ..config import OLLAMA_API_KEY
        headers = {"Authorization": f"Bearer {OLLAMA_API_KEY}"} if OLLAMA_API_KEY else {}
        r = httpx.get(f"{url}/api/tags", headers=headers, timeout=8)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        log.info("MedGemma OK at %s  models=%s", url, models)
        return True
    except Exception as e:
        log.warning("MedGemma not reachable at %s: %s", url, e)
        return False


def _get_medgemma_url() -> str | None:
    from ..config import MEDGEMMA_URL, MEDGEMMA_CPU_URL
    for url in [MEDGEMMA_URL, MEDGEMMA_CPU_URL]:
        if url and _check_medgemma(url):
            return url
    return None


# ── Main entry point ───────────────────────────────────────────────────────────

def run_ab_batch(
    n_scenarios: int,
    kb: KBConfig,
    mode: str = "weighted",
    seed: int = 42,
    max_turns: int = 4,
    diagnosis_ids: list[str] | None = None,
    skip_arm_a: bool = False,
    skip_arm_b: bool = False,
    skip_order_gen: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """
    Run the A/B batch and return the complete results dict.

    on_progress(case_dict) is called after each turn completes — use this for
    live streaming to the UI without waiting for the full batch.

    Raises RuntimeError if MedGemma is required but unreachable.
    """
    from ..config import MODEL, KG_FALLBACK_MODEL

    # ── MedGemma health check ──────────────────────────────────────────────────
    if not skip_arm_b:
        import os
        mg_url = _get_medgemma_url()
        if mg_url is None:
            raise RuntimeError(
                "MedGemma is not reachable. "
                "Start the Ollama instance or set skip_arm_b=True to run Arm A only."
            )
        os.environ["MEDGEMMA_URL"] = mg_url
        log.info("Using MedGemma at %s", mg_url)

    model = None   # use the default model from config

    # ── Sample scenarios ───────────────────────────────────────────────────────
    diagnosis_filter: str | None = None
    multi_filter: list[str] | None = None

    if diagnosis_ids:
        valid = {d["id"] for d in DIAGNOSIS_GROUPS}
        bad = [i for i in diagnosis_ids if i not in valid]
        if bad:
            raise ValueError(f"Unknown diagnosis IDs: {bad}")
        if len(diagnosis_ids) == 1:
            diagnosis_filter = diagnosis_ids[0]
        else:
            multi_filter = diagnosis_ids

    entries = sample_scenarios(n_scenarios, mode, seed=seed, diagnosis_filter=diagnosis_filter)

    if multi_filter:
        pool = sample_scenarios(n_scenarios * 10, "random", seed=seed)
        entries = [e for e in pool if e.diagnosis_id in multi_filter][:n_scenarios]

    log.info("Sampled %d scenario entries (mode=%s, seed=%d)", len(entries), mode, seed)

    # ── Run scenarios ──────────────────────────────────────────────────────────
    arm_a_config = {"grounding_mode": "wiki",     "model": MODEL}
    arm_b_config = {"grounding_mode": "medgemma", "model": KG_FALLBACK_MODEL}
    do_og = not skip_order_gen
    all_cases: list[dict] = []

    for idx, entry in enumerate(entries):
        log.info("═══ Scenario %d/%d: [%s] + [%s] ═══",
                 idx + 1, len(entries), entry.diagnosis_label, entry.complication_label)
        cases = _run_scenario(
            entry=entry,
            max_turns=max_turns,
            kb=kb,
            skip_arm_a=skip_arm_a,
            skip_arm_b=skip_arm_b,
            do_order_gen=do_og,
            model=model,
            on_progress=on_progress,
        )
        all_cases.extend(cases)

    # ── Assemble result ────────────────────────────────────────────────────────
    result = {
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "kb":              kb.name,
        "mode":            mode,
        "seed":            seed,
        "diagnosis_filter": ",".join(diagnosis_ids) if diagnosis_ids else "all",
        "max_turns":       max_turns,
        "arm_a_config":    arm_a_config,
        "arm_b_config":    arm_b_config,
        "n_cases":         len(all_cases),
        "cases":           all_cases,
    }

    # Quick summary log
    a_ok = [c for c in all_cases if (c.get("arm_a") or {}).get("ok")]
    b_ok = [c for c in all_cases if (c.get("arm_b") or {}).get("ok")]
    if a_ok:
        avg_m = sum(c["arm_a"]["markers_remaining"] for c in a_ok) / len(a_ok)
        log.info("Arm A: %d/%d succeeded  avg_markers=%.1f", len(a_ok), len(all_cases), avg_m)
    if b_ok:
        avg_m = sum(c["arm_b"]["markers_remaining"] for c in b_ok) / len(b_ok)
        avg_r = sum(c["arm_b"]["markers_resolved_by_medgemma"] for c in b_ok) / len(b_ok)
        log.info("Arm B: %d/%d succeeded  avg_markers=%.1f  avg_mg_resolved=%.1f",
                 len(b_ok), len(all_cases), avg_m, avg_r)

    return result
