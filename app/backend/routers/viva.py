"""
Viva (teacher-student loop) endpoints.

POST /api/viva/patient              — create / upsert dummy patient
GET  /api/viva/patient              — get dummy patient
POST /api/viva/patient/place-order  — place / edit / stop one order on dummy patient
POST /api/viva/start                — start new session (trajectory + first scenario + pending orders)
POST /api/viva/{session_id}/advance — advance after user places orders (simulator → teacher → new pending orders)
POST /api/viva/{session_id}/turn    — legacy automated turn (student → order-gen → gaps → teacher)
GET  /api/viva/                     — list sessions
GET  /api/viva/{session_id}         — get session state
DELETE /api/viva/{session_id}       — delete session
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.viva_student_agent import run_viva_student_turn
from ..services.resolve_service import resolve_all_gaps
from ..services.viva_teacher import (
    generate_first_scenario,
    generate_next_turn,
    generate_trajectory,
)
from ..services.viva_session import (
    create_session,
    delete_session,
    list_sessions,
    load_session,
    save_session,
)
from ..services.emr import (
    get_active_orders,
    get_dummy_patient,
    place_viva_order,
    reset_dummy_patient_chart,
    upsert_dummy_patient,
    VIVA_DUMMY_CPMRN,
)
from ..services.emr.db import get_db
from ..services.order_gen_pipeline import run_order_generation
from ..services.order_safety_annotator import annotate_recommendations
from ..services.viva_simulator import simulate_and_write, write_patient_state, get_result_delay

log = logging.getLogger("wiki.viva")

router = APIRouter(prefix="/api/viva", tags=["viva"])

_VIVA_GAP_MAX_RESULTS = 1  # keep turns fast; LLM escalation kicks in from turn 3+


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _dedup_orders(raw_orders: list[dict]) -> list[dict]:
    """Deduplicate orders by (order_type, orderable_name), merging notes."""
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
            extra_instr = (order.get("order_details") or {}).get("instructions") or ""
            existing_notes = existing.get("notes") or ""
            existing_instr = (existing.get("order_details") or {}).get("instructions") or ""
            if extra_notes and extra_notes not in existing_notes:
                existing["notes"] = f"{existing_notes}; {extra_notes}".lstrip("; ")
            if extra_instr and extra_instr not in existing_instr:
                if existing.get("order_details"):
                    existing["order_details"]["instructions"] = f"{existing_instr}; {extra_instr}".lstrip("; ")
        else:
            seen[key] = len(deduped)
            deduped.append(order)
    return deduped


def _flatten_active_orders(active_orders: dict | None) -> list[dict]:
    """Convert MongoDB active_orders dict into a flat list for the simulator.
    Stopped/held orders are excluded — they represent decisions already made."""
    if not active_orders:
        return []
    result = []
    for cat_key, type_label in [
        ("medications", "med"), ("labs", "lab"), ("procedures", "procedure"),
        ("vents", "vents"), ("diets", "diet"), ("bloods", "blood"),
    ]:
        for item in active_orders.get(cat_key, []):
            if item.get("orderStatus") in ("stopped", "held"):
                continue
            result.append({
                "order_type": type_label,
                "orderable_name": (
                    item.get("name") or item.get("investigation") or item.get("pType") or ""
                ),
                "order_details": {
                    k: item[k] for k in ("quantity", "unit", "route", "frequency", "instructions")
                    if item.get(k)
                },
                "notes": item.get("notes", ""),
                "confidence": "high",
                "is_home_med": bool(item.get("isHomeMed")),
            })
    return result


def _orders_placed_text(orders: list[dict]) -> str:
    """Build a human-readable summary of placed orders for the teacher prompt."""
    if not orders:
        return "No orders placed by user this turn."
    lines = ["User placed the following orders:"]
    for o in orders:
        name = o.get("orderable_name") or "—"
        t = o.get("order_type", "")
        instr = (o.get("order_details") or {}).get("instructions") or o.get("notes") or ""
        line = f"  • [{t.upper()}] {name}"
        if instr:
            line += f" — {instr}"
        lines.append(line)
    return "\n".join(lines)


async def _generate_pending_orders(
    clinical_context: str,
    question: str,
    phase: str,
    difficulty: str,
    cpmrn: str,
    kb: KBConfig,
    model: str | None,
    pending_conditionals: list[dict] | None = None,
) -> tuple[list[dict], float, list[dict]]:
    """
    Run student agent + order gen for a scenario to produce AI-suggested orders.
    Returns (orders, cost_usd, conditional_orders). Never raises — returns ([], 0.0, []) on failure.
    """
    try:
        snap_result = await asyncio.to_thread(
            run_viva_student_turn,
            clinical_context, question, phase, difficulty, cpmrn, kb, model,
            pending_conditionals,
        )
        student_snap = snap_result["snapshots"][0]
        conditional_orders = student_snap.get("conditional_orders", [])
        all_steps = (
            student_snap.get("immediate_next_steps", []) +
            student_snap.get("monitoring_followup", [])
        )
        cost = student_snap.get("cost_usd", 0.0)
        if not all_steps:
            return [], cost, conditional_orders
        try:
            all_steps = await asyncio.to_thread(
                annotate_recommendations, all_steps, cpmrn, model,
            )
        except Exception:
            pass
        active_resp = get_active_orders(cpmrn)
        active = active_resp.get("active_orders") if not active_resp.get("error") else None
        order_result = await asyncio.to_thread(
            run_order_generation, all_steps, cpmrn, "adult", model, kb, active, None,
        )
        raw_orders = order_result.get("orders", [])
        cost += order_result.get("cost_usd", 0.0)
        run_id = order_result.get("run_id")
        if run_id:
            for o in raw_orders:
                o["order_run_id"] = run_id
        return _dedup_orders(raw_orders), cost, conditional_orders
    except Exception as exc:
        log.warning("_generate_pending_orders failed: %s", exc)
        return [], 0.0, []


# ── Pydantic models ────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    topic: str
    model: str | None = None
    max_turns: int = 8


class TurnRequest(BaseModel):
    model: str | None = None


class DummyPatientRequest(BaseModel):
    name: str = "Viva Patient"
    age_years: int = 50
    gender: str = "male"
    weight_kg: float | None = None
    height_cm: float | None = None
    creatinine: float | None = None
    egfr: float | None = None
    allergies: list[str] = []
    diagnoses: list[str] = []
    home_meds: list[str] = []


class PlaceOrderRequest(BaseModel):
    action: str = "new"          # new | edit | stop
    order_type: str = "med"      # med | lab | procedure | comm | vents | diet | blood
    orderable_name: str | None = None
    order_details: dict | None = None
    existing_order_no: str | None = None
    from_dose: str | None = None
    to_dose: str | None = None
    recommendation: str | None = None
    dose_calculation: str | None = None
    confidence: str | None = None
    notes: str | None = None


# ── Dummy patient endpoints ────────────────────────────────────────────────────
# NOTE: these must be registered BEFORE /{session_id} routes so FastAPI
# matches /patient as a literal segment, not as a session_id parameter.

@router.get("/patient")
def get_viva_patient():
    """Return the current dummy patient, or null if not created yet."""
    patient = get_dummy_patient()
    return {"patient": patient}


@router.post("/patient")
def create_viva_patient(req: DummyPatientRequest):
    """Create or replace the dummy patient. Clears all active orders."""
    patient = upsert_dummy_patient(req.model_dump())
    return {"patient": patient}


@router.post("/patient/place-order")
def place_order(req: PlaceOrderRequest):
    """Place, edit, or stop a single order on the dummy patient chart."""
    result = place_viva_order(req.model_dump())
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/patient/live-state")
def get_viva_patient_live_state():
    """
    Return a structured snapshot of the dummy patient's current MongoDB state:
    latest vitals, all recent lab documents, IO summary, active orders, and
    recent clinical notes. Used to populate the Patient Chart drawer in the UI.
    """
    from datetime import datetime as _dt, timedelta as _td

    cpmrn = VIVA_DUMMY_CPMRN
    p = get_db().patients.find_one({"CPMRN": cpmrn})

    if not p:
        return {
            "vitals": None,
            "labs": [],
            "io": {"period_hours": 12, "entries_counted": 0, "total_urine_ml": 0,
                   "total_intake_ml": 0, "net_balance_ml": 0, "urine_rate_ml_per_hr": 0},
            "orders": {"medications": [], "labs": [], "procedures": [],
                       "diets": [], "vents": [], "bloods": []},
            "notes": [],
            "vitals_history_count": 0,
            "history": {"home_medications": [], "diagnoses": [], "allergies": []},
        }

    # ── Latest vitals ─────────────────────────────────────────────────────────
    vitals_list = sorted(p.get("vitals", []), key=lambda v: v.get("timestamp", ""), reverse=True)
    latest_vitals = None
    if vitals_list:
        v = vitals_list[0]
        latest_vitals = {
            "timestamp":      v.get("timestamp"),
            "SpO2":           v.get("daysSpO2"),
            "HR":             v.get("daysHR"),
            "RR":             v.get("daysRR"),
            "BP":             v.get("daysBP"),
            "MAP":            v.get("daysMAP"),
            "Temperature":    v.get("daysTemperature"),
            "TemperatureUnit":v.get("daysTemperatureUnit", "C"),
            "FiO2":           v.get("daysFiO2"),
            "OxygenFlow":     v.get("daysOxygenFlow"),
            "TherapyDevice":  v.get("daysTherapyDevice"),
            "AVPU":           v.get("daysAVPU"),
            "CVP":            v.get("daysCVP"),
            "VentMode":       v.get("daysVentMode"),
            "VentPEEP":       v.get("daysVentPEEP"),
            "VentPIP":        v.get("daysVentPip"),
            "VentRRSet":      v.get("daysVentRRset"),
            "isIntubated":    (p.get("isIntubated") or {}).get("value"),
            "isNIV":          (p.get("isNIV") or {}).get("value"),
            "isHFNC":         (p.get("isHFNC") or {}).get("value"),
            "dataBy":         v.get("dataBy"),
        }

    # ── All lab documents (most recent first, up to 10) ───────────────────────
    docs = p.get("documents", [])
    lab_docs = sorted(
        [d for d in docs if d.get("category") == "labs"],
        key=lambda d: d.get("reportedAt", ""),
        reverse=True,
    )
    labs = []
    for doc in lab_docs[:10]:
        attrs_raw = doc.get("attributes") or {}
        attrs_display = {}
        for key, val in attrs_raw.items():
            if isinstance(val, dict):
                attrs_display[key] = {
                    "value": val.get("value"),
                    "normalRange": {
                        "min": (val.get("errorRange") or {}).get("min"),
                        "max": (val.get("errorRange") or {}).get("max"),
                    },
                }
        labs.append({
            "name":       doc.get("name"),
            "reportedAt": doc.get("reportedAt"),
            "text":       doc.get("text"),
            "attributes": attrs_display,
        })

    # ── IO summary (last 12 h) ─────────────────────────────────────────────────
    io_hours = 12
    cutoff = (_dt.utcnow() - _td(hours=io_hours)).isoformat()
    io_entries = [e for e in p.get("io", []) if e.get("timestamp", "") >= cutoff]
    total_urine  = sum(e.get("urine_ml", 0)  for e in io_entries)
    total_intake = sum(e.get("intake_ml", 0) for e in io_entries)
    io_summary = {
        "period_hours":       io_hours,
        "entries_counted":    len(io_entries),
        "total_urine_ml":     total_urine,
        "total_intake_ml":    total_intake,
        "net_balance_ml":     total_intake - total_urine,
        "urine_rate_ml_per_hr": round(total_urine / io_hours, 1),
    }

    # ── Active orders ──────────────────────────────────────────────────────────
    active = (p.get("orders") or {}).get("active") or {}
    orders = {
        "medications": active.get("medications", []),
        "labs":        active.get("labs", []),
        "procedures":  active.get("procedures", []),
        "diets":       active.get("diets", []),
        "vents":       active.get("vents", []),
        "bloods":      active.get("bloods", []),
    }

    # ── Recent clinical notes (non-lab documents, last 5) ─────────────────────
    note_docs = sorted(
        [d for d in docs if d.get("category") != "labs"],
        key=lambda d: d.get("reportedAt", ""),
        reverse=True,
    )
    notes = [
        {
            "category":   d.get("category"),
            "name":       d.get("name"),
            "text":       d.get("text", ""),
            "reportedAt": d.get("reportedAt"),
        }
        for d in note_docs[:5]
    ]

    return {
        "vitals":               latest_vitals,
        "labs":                 labs,
        "io":                   io_summary,
        "orders":               orders,
        "notes":                notes,
        "vitals_history_count": len(vitals_list),
        "history": {
            "home_medications": p.get("home_medications", []),
            "diagnoses":        p.get("chronic", []),
            "allergies":        p.get("allergies", []),
        },
    }


# ── Session endpoints ──────────────────────────────────────────────────────────

@router.post("/start")
async def start_viva(req: StartRequest, kb: KBConfig = Depends(resolve_kb)):
    """Generate trajectory + first scenario and persist a new session."""
    session_id = f"viva_{uuid.uuid4().hex[:8]}"
    model = req.model

    # Build patient context string from MongoDB (home meds, diagnoses, allergies)
    patient_context = ""
    try:
        from ..services.emr import get_dummy_patient as _gdp
        _pt = _gdp()
        if _pt:
            parts = []
            if _pt.get("home_meds"):
                parts.append("Home medications: " + ", ".join(_pt["home_meds"]))
            if _pt.get("diagnoses"):
                parts.append("Known diagnoses: " + ", ".join(_pt["diagnoses"]))
            if _pt.get("allergies"):
                parts.append("Allergies: " + ", ".join(_pt["allergies"]))
            patient_context = "\n".join(parts)
    except Exception as _exc:
        log.warning("Could not fetch patient context for teacher: %s", _exc)

    trajectory = await asyncio.to_thread(generate_trajectory, req.topic, model, patient_context)
    first_scenario = await asyncio.to_thread(
        generate_first_scenario, req.topic, trajectory, model, patient_context
    )

    # Wipe all clinical chart data so the teacher's patient_state seeds a clean slate
    try:
        reset_dummy_patient_chart()
    except Exception as exc:
        log.warning("Could not reset dummy patient chart: %s", exc)

    # Seed initial patient state from teacher's first scenario into MongoDB
    patient_state = first_scenario.pop("patient_state", {})
    if patient_state:
        try:
            seed_summary = await asyncio.to_thread(
                write_patient_state, patient_state, VIVA_DUMMY_CPMRN
            )
            log.info("Seeded initial patient state: %s", seed_summary[:200])
        except Exception as exc:
            log.warning("Could not seed initial patient state: %s", exc)

    session = create_session(
        session_id=session_id,
        topic=req.topic,
        trajectory=trajectory,
        first_scenario=first_scenario,
        max_turns=req.max_turns,
        model=model or "",
        kb_name=kb.name,
        kb=kb,
    )

    # Generate AI-suggested orders for turn 1 so the user sees suggestions immediately
    log.info("Viva %s start: generating initial pending orders", session_id)
    pending_orders, pending_cost = await _generate_pending_orders(
        first_scenario.get("clinical_context", ""),
        first_scenario.get("question", ""),
        first_scenario.get("phase", "MANAGEMENT"),
        first_scenario.get("difficulty", "MEDIUM"),
        VIVA_DUMMY_CPMRN, kb, model,
    )
    session["pending_orders"] = pending_orders
    session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + pending_cost, 6)
    save_session(session, kb)
    log.info("Viva %s start: %d initial pending orders generated", session_id, len(pending_orders))
    return {"session": session}


@router.post("/{session_id}/fork")
async def fork_viva(session_id: str, kb: KBConfig = Depends(resolve_kb)):
    """
    Create a new session that replays the exact same scenarios as an existing one.
    The student answers fresh against the current (post-gap-resolution) wiki.
    Teacher is bypassed — scenarios are served from the stored replay_queue.
    """
    try:
        original = load_session(session_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    turns = original.get("turns", [])
    if not turns:
        raise HTTPException(status_code=400, detail="Session has no completed turns to replay")

    scenarios = [t["scenario"] for t in turns]
    new_id = f"viva_{uuid.uuid4().hex[:8]}"

    session = {
        "session_id": new_id,
        "created_at": __import__("datetime").datetime.utcnow().isoformat(),
        "topic": original["topic"],
        "trajectory": original["trajectory"],
        "max_turns": len(scenarios),
        "current_turn": 0,
        "status": "active",
        "model": original.get("model", ""),
        "kb_name": original.get("kb_name", ""),
        "turns": [],
        "next_scenario": scenarios[0],
        "replay_queue": scenarios[1:],
        "forked_from": session_id,
        "outcome": None,
        "total_cost_usd": 0.0,
    }

    save_session(session, kb)
    return {"session": session}


@router.post("/{session_id}/advance")
async def advance_session(
    session_id: str,
    req: TurnRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Advance the viva after the user has placed/ignored orders:
      1. Read active orders from MongoDB (what the user actually placed)
      2. Run simulator on those orders
      3. Gap resolution
      4. Teacher generates next scenario
      5. Student agent + order gen → pending_orders for next scenario
    """
    try:
        session = load_session(session_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    if session["status"] != "active":
        raise HTTPException(status_code=400, detail="Session is already complete")

    next_scenario = session.get("next_scenario")
    if not next_scenario:
        raise HTTPException(status_code=400, detail="No pending scenario in session")

    model = req.model or session.get("model") or None
    turn_num = session["current_turn"] + 1

    # ── 0. Gather pending conditionals from prior turns (deduplicated) ────────
    all_prior_conditionals: list[dict] = []
    for t in session.get("turns", []):
        for c in t.get("conditional_orders", []):
            if not any(
                p.get("condition") == c.get("condition") and p.get("action") == c.get("action")
                for p in all_prior_conditionals
            ):
                all_prior_conditionals.append(c)
    pending_conditionals_for_turn = all_prior_conditionals or None
    log.info("Viva %s advance turn %d: %d pending conditional(s) from prior turns",
             session_id, turn_num, len(all_prior_conditionals))

    # ── 1. Read active orders from MongoDB ────────────────────────────────────
    try:
        active_resp = get_active_orders(VIVA_DUMMY_CPMRN)
        active_orders_dict = active_resp.get("active_orders") if not active_resp.get("error") else {}
    except Exception as exc:
        log.warning("Viva %s advance turn %d: could not load active orders: %s", session_id, turn_num, exc)
        active_orders_dict = {}
    placed_orders = _flatten_active_orders(active_orders_dict)
    log.info("Viva %s advance turn %d: %d orders placed by user", session_id, turn_num, len(placed_orders))

    # ── 1.5. Time delta + pending lab queue ──────────────────────────────────
    time_delta_minutes: int = next_scenario.get("time_delta_minutes", 60)
    session_elapsed_minutes: int = session.get("session_elapsed_minutes", 0)
    turn_end_minutes: int = session_elapsed_minutes + time_delta_minutes

    pending_lab_queue: list[dict] = session.get("pending_lab_queue", [])
    available_labs = [e["name"] for e in pending_lab_queue if e["available_at_minutes"] <= turn_end_minutes]
    still_pending = [e["name"] for e in pending_lab_queue if e["available_at_minutes"] > turn_end_minutes]

    new_queue_entries: list[dict] = []
    for o in placed_orders:
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
    updated_lab_queue = (
        [e for e in pending_lab_queue if e["available_at_minutes"] > turn_end_minutes]
        + new_queue_entries
    )
    log.info(
        "Viva %s advance turn %d: time_delta=%dm  elapsed=%dm  available_labs=%s  pending=%s",
        session_id, turn_num, time_delta_minutes, turn_end_minutes,
        available_labs, still_pending,
    )

    # ── 2. Simulator ──────────────────────────────────────────────────────────
    simulation_summary = ""
    vital_timeline: list[dict] = []
    log.info("Viva %s advance turn %d: running simulator", session_id, turn_num)
    try:
        sim_result = await asyncio.to_thread(
            simulate_and_write,
            session["trajectory"],
            placed_orders,
            VIVA_DUMMY_CPMRN,
            next_scenario.get("clinical_context", ""),
            turn_num,
            session["max_turns"],
            model,
            time_delta_minutes,
            available_labs or None,
            still_pending or None,
        )
        simulation_summary = sim_result.get("summary", "")
        vital_timeline = sim_result.get("vital_timeline", [])
        log.info("Viva %s advance turn %d: simulation complete — %s", session_id, turn_num, simulation_summary[:120])
    except Exception as exc:
        log.warning("Viva %s advance turn %d: simulation failed: %s", session_id, turn_num, exc)

    # ── 3. Gap resolution ─────────────────────────────────────────────────────
    log.info("Viva %s advance turn %d: resolving gaps", session_id, turn_num)
    gaps_before = _count_pending_gaps(kb)
    gap_stats = await asyncio.to_thread(resolve_all_gaps, kb, _VIVA_GAP_MAX_RESULTS)
    gaps_resolved = max(0, gaps_before - _count_pending_gaps(kb))
    gap_cost = gap_stats.get("cost_usd", 0.0)
    log.info("Viva %s advance turn %d: %d gaps resolved", session_id, turn_num, gaps_resolved)

    # ── 4. Teacher generates next scenario ────────────────────────────────────
    student_answer_text = _orders_placed_text(placed_orders)
    log.info("Viva %s advance turn %d: teacher generating next step", session_id, turn_num)

    replay_queue: list = session.get("replay_queue", [])
    if "replay_queue" in session:
        if replay_queue:
            next_queued = replay_queue.pop(0)
            session["replay_queue"] = replay_queue
            teacher_result = {"complete": False, "scenario": next_queued}
        else:
            teacher_result = {"complete": True, "outcome": "Replay complete."}
    else:
        teacher_result = await asyncio.to_thread(
            generate_next_turn,
            session["trajectory"],
            session["turns"],
            student_answer_text,
            turn_num,
            session["max_turns"],
            simulation_summary,
            model,
        )
        if not teacher_result.get("complete"):
            additional_notes = teacher_result["scenario"].pop("additional_notes", [])
            if additional_notes:
                try:
                    await asyncio.to_thread(write_patient_state, {"notes": additional_notes}, VIVA_DUMMY_CPMRN)
                    log.info("Viva %s advance turn %d: wrote %d additional notes", session_id, turn_num, len(additional_notes))
                except Exception as exc:
                    log.warning("Could not write teacher additional notes: %s", exc)

    # ── 5. Generate pending orders for next scenario ──────────────────────────
    complete = teacher_result["complete"] or turn_num >= session["max_turns"]
    pending_orders: list[dict] = []
    pending_cost = 0.0
    if not complete and teacher_result.get("scenario"):
        next_s = teacher_result["scenario"]
        log.info("Viva %s advance turn %d: generating pending orders for next scenario", session_id, turn_num)
        pending_orders, pending_cost, new_conditionals = await _generate_pending_orders(
            next_s.get("clinical_context", ""),
            next_s.get("question", ""),
            next_s.get("phase", "MANAGEMENT"),
            next_s.get("difficulty", "MEDIUM"),
            VIVA_DUMMY_CPMRN, kb, model,
            pending_conditionals=pending_conditionals_for_turn,
        )
        log.info("Viva %s advance turn %d: %d pending orders generated  %d conditional(s)",
                 session_id, turn_num, len(pending_orders), len(new_conditionals))
    else:
        new_conditionals = []

    # ── Persist ───────────────────────────────────────────────────────────────
    turn_cost = gap_cost + pending_cost
    turn_record = {
        "turn_num": turn_num,
        "scenario": next_scenario,
        "chat_run_id": None,
        "order_run_id": None,
        "student_answer_text": student_answer_text,
        "student_snap": {},
        "orders": placed_orders,
        "simulation_summary": simulation_summary,
        "gaps_resolved": gaps_resolved,
        "cost_usd": turn_cost,
        "conditional_orders": new_conditionals,
        "turn_delta_minutes": time_delta_minutes,
        "session_elapsed_minutes": turn_end_minutes,
        "vital_timeline": vital_timeline,
        "available_labs_this_turn": available_labs,
    }
    session["turns"].append(turn_record)
    session["current_turn"] = turn_num
    session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + turn_cost, 6)
    session["session_elapsed_minutes"] = turn_end_minutes
    session["pending_lab_queue"] = updated_lab_queue

    if complete:
        session["status"] = "complete"
        session["outcome"] = teacher_result.get("outcome", "Case concluded.")
        session["next_scenario"] = None
        session["pending_orders"] = []
    else:
        session["next_scenario"] = teacher_result["scenario"]
        session["pending_orders"] = pending_orders

    save_session(session, kb)
    return {
        "turn_num": turn_num,
        "placed_orders": placed_orders,
        "gaps_resolved": gaps_resolved,
        "complete": complete,
        "outcome": session.get("outcome"),
        "next_scenario": session.get("next_scenario"),
        "pending_orders": pending_orders,
        "cost_usd": turn_cost,
        "session": session,
    }


@router.post("/{session_id}/turn")
async def run_turn(
    session_id: str,
    req: TurnRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Legacy automated turn (kept for backward compatibility):
      1. Student assessment (wiki-grounded CDS)
      2. Order generation from immediate_next_steps (with active orders context)
      3. Gap resolution (blocking)
      4. Teacher generates next scenario or signals completion
    """
    try:
        session = load_session(session_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    if session["status"] != "active":
        raise HTTPException(status_code=400, detail="Session is already complete")

    next_scenario = session.get("next_scenario")
    if not next_scenario:
        raise HTTPException(status_code=400, detail="No pending scenario in session")

    model = req.model or session.get("model") or None
    turn_num = session["current_turn"] + 1

    # ── 0. Gather pending conditionals from prior turns ───────────────────────
    _prior_conditionals: list[dict] = []
    for t in session.get("turns", []):
        for c in t.get("conditional_orders", []):
            if not any(
                p.get("condition") == c.get("condition") and p.get("action") == c.get("action")
                for p in _prior_conditionals
            ):
                _prior_conditionals.append(c)
    _pending_conds = _prior_conditionals or None
    log.info("Viva %s turn %d: %d pending conditional(s) from prior turns",
             session_id, turn_num, len(_prior_conditionals))

    # ── 1. Student assessment (agentic — queries MongoDB with tools) ──────────
    log.info("Viva %s turn %d: student assessment", session_id, turn_num)
    snap_result = await asyncio.to_thread(
        run_viva_student_turn,
        next_scenario.get("clinical_context", ""),
        next_scenario.get("question", ""),
        next_scenario.get("phase", "MANAGEMENT"),
        next_scenario.get("difficulty", "MEDIUM"),
        VIVA_DUMMY_CPMRN,
        kb,
        model,
        pending_conditionals=_pending_conds,
    )
    student_snap = snap_result["snapshots"][0]
    student_answer_text = _build_student_answer_text(student_snap)
    turn_cost = student_snap.get("cost_usd", 0.0)
    chat_run_id = student_snap.get("chat_run_id")

    # ── 2. Order generation ───────────────────────────────────────────────────
    log.info("Viva %s turn %d: generating orders", session_id, turn_num)
    immediate_steps: list[str] = student_snap.get("immediate_next_steps", [])
    monitoring_steps: list[str] = student_snap.get("monitoring_followup", [])
    all_steps = immediate_steps + monitoring_steps
    generated_orders: list[dict] = []
    order_run_id: str | None = None
    order_cost = 0.0

    if all_steps:
        # ── 2a. Safety annotation — enrich recs with organ function / safety context
        try:
            all_steps = await asyncio.to_thread(
                annotate_recommendations,
                all_steps,
                VIVA_DUMMY_CPMRN,
                model,
            )
        except Exception as exc:
            log.warning("Order safety annotation failed (continuing with originals): %s", exc)

        try:
            active_orders_resp = get_active_orders(VIVA_DUMMY_CPMRN)
            active_orders = active_orders_resp.get("active_orders") if not active_orders_resp.get("error") else None
        except Exception as exc:
            log.warning("Could not load active orders for viva turn: %s", exc)
            active_orders = None

        try:
            order_result = await asyncio.to_thread(
                run_order_generation,
                all_steps,
                VIVA_DUMMY_CPMRN,
                "adult",
                model,
                kb,
                active_orders,
                chat_run_id,
            )
            raw_orders = order_result.get("orders", [])
            order_run_id = order_result.get("run_id")
            order_cost = order_result.get("cost_usd", 0.0)
            generated_orders = _dedup_orders(raw_orders)
            log.info(
                "Viva %s turn %d: %d orders generated (%d after dedup)  cost=$%.4f",
                session_id, turn_num, len(raw_orders), len(generated_orders), order_cost,
            )
        except Exception as exc:
            log.warning("Order generation failed for viva turn %d: %s", turn_num, exc)

    turn_cost += order_cost

    # ── 2.5. Time delta + pending lab queue ──────────────────────────────────
    _time_delta_minutes: int = next_scenario.get("time_delta_minutes", 60)
    _session_elapsed: int = session.get("session_elapsed_minutes", 0)
    _turn_end_minutes: int = _session_elapsed + _time_delta_minutes

    _pending_lab_queue: list[dict] = session.get("pending_lab_queue", [])
    _available_labs = [e["name"] for e in _pending_lab_queue if e["available_at_minutes"] <= _turn_end_minutes]
    _still_pending = [e["name"] for e in _pending_lab_queue if e["available_at_minutes"] > _turn_end_minutes]

    _new_queue_entries: list[dict] = []
    for o in generated_orders:
        if o.get("order_type") in ("lab", "procedure"):
            name = o.get("orderable_name", "")
            if name:
                delay = get_result_delay(name)
                _new_queue_entries.append({
                    "name": name,
                    "ordered_at_minutes": _turn_end_minutes,
                    "available_at_minutes": _turn_end_minutes + delay,
                    "ordered_turn": turn_num,
                })
    _updated_lab_queue = (
        [e for e in _pending_lab_queue if e["available_at_minutes"] > _turn_end_minutes]
        + _new_queue_entries
    )
    log.info(
        "Viva %s turn %d: time_delta=%dm  elapsed=%dm  available_labs=%s  pending=%s",
        session_id, turn_num, _time_delta_minutes, _turn_end_minutes,
        _available_labs, _still_pending,
    )

    # ── 2.6. Simulation — write patient responses to MongoDB ─────────────────
    simulation_summary = ""
    vital_timeline: list[dict] = []
    if generated_orders:
        log.info("Viva %s turn %d: running simulator", session_id, turn_num)
        try:
            sim_result = await asyncio.to_thread(
                simulate_and_write,
                session["trajectory"],
                generated_orders,
                VIVA_DUMMY_CPMRN,
                next_scenario.get("clinical_context", ""),
                turn_num,
                session["max_turns"],
                model,
                _time_delta_minutes,
                _available_labs or None,
                _still_pending or None,
            )
            simulation_summary = sim_result.get("summary", "")
            vital_timeline = sim_result.get("vital_timeline", [])
            log.info("Viva %s turn %d: simulation complete — %s", session_id, turn_num, simulation_summary[:120])
        except Exception as exc:
            log.warning("Viva %s turn %d: simulation failed: %s", session_id, turn_num, exc)

    # ── 3. Gap resolution ─────────────────────────────────────────────────────
    log.info("Viva %s turn %d: resolving gaps", session_id, turn_num)
    gaps_before = _count_pending_gaps(kb)
    gap_stats = await asyncio.to_thread(resolve_all_gaps, kb, _VIVA_GAP_MAX_RESULTS)
    gaps_resolved = max(0, gaps_before - _count_pending_gaps(kb))
    turn_cost += gap_stats.get("cost_usd", 0.0)
    log.info(
        "Viva %s turn %d: %d gaps resolved, cost=$%.4f",
        session_id, turn_num, gaps_resolved, gap_stats.get("cost_usd", 0),
    )

    # ── 4. Advance case: replay queue OR teacher ──────────────────────────────
    replay_queue: list = session.get("replay_queue", [])
    if "replay_queue" in session:
        if replay_queue:
            log.info("Viva %s turn %d: replay mode, %d scenarios remaining", session_id, turn_num, len(replay_queue))
            next_queued = replay_queue.pop(0)
            session["replay_queue"] = replay_queue
            teacher_result = {"complete": False, "scenario": next_queued}
        else:
            log.info("Viva %s turn %d: replay mode, last turn", session_id, turn_num)
            teacher_result = {"complete": True, "outcome": "Replay complete."}
    else:
        log.info("Viva %s turn %d: teacher generating next step", session_id, turn_num)
        teacher_result = await asyncio.to_thread(
            generate_next_turn,
            session["trajectory"],
            session["turns"],
            student_answer_text,
            turn_num,
            session["max_turns"],
            simulation_summary,
            model,
        )
        # Write any supplementary notes the teacher added (consultant reviews, etc.)
        if not teacher_result.get("complete"):
            additional_notes = teacher_result["scenario"].pop("additional_notes", [])
            if additional_notes:
                try:
                    await asyncio.to_thread(
                        write_patient_state, {"notes": additional_notes}, VIVA_DUMMY_CPMRN
                    )
                    log.info(
                        "Viva %s turn %d: wrote %d additional notes from teacher",
                        session_id, turn_num, len(additional_notes),
                    )
                except Exception as exc:
                    log.warning("Could not write teacher additional notes: %s", exc)

    # ── Persist ───────────────────────────────────────────────────────────────
    turn_record = {
        "turn_num": turn_num,
        "scenario": next_scenario,
        "chat_run_id": chat_run_id,
        "order_run_id": order_run_id,
        "student_answer_text": student_answer_text,
        "student_snap": {
            "clinical_direction":   student_snap.get("clinical_direction", []),
            "clinical_reasoning":   student_snap.get("clinical_reasoning", []),
            "specific_parameters":  student_snap.get("specific_parameters", []),
            "monitoring_followup":  student_snap.get("monitoring_followup", []),
            "alternative_considerations": student_snap.get("alternative_considerations", []),
            "pages_consulted":      student_snap.get("pages_consulted", []),
            "gap_sections":         student_snap.get("gap_sections", []),
        },
        "orders": generated_orders,
        "simulation_summary": simulation_summary,
        "gaps_resolved": gaps_resolved,
        "cost_usd": turn_cost,
        "conditional_orders": student_snap.get("conditional_orders", []),
        "turn_delta_minutes": _time_delta_minutes,
        "session_elapsed_minutes": _turn_end_minutes,
        "vital_timeline": vital_timeline,
        "available_labs_this_turn": _available_labs,
    }

    session["turns"].append(turn_record)
    session["current_turn"] = turn_num
    session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + turn_cost, 6)
    session["session_elapsed_minutes"] = _turn_end_minutes
    session["pending_lab_queue"] = _updated_lab_queue

    complete = teacher_result["complete"] or turn_num >= session["max_turns"]
    if complete:
        session["status"] = "complete"
        session["outcome"] = teacher_result.get("outcome", "Case concluded.")
        session["next_scenario"] = None
    else:
        session["next_scenario"] = teacher_result["scenario"]

    save_session(session, kb)

    return {
        "turn_num": turn_num,
        "student_answer": student_answer_text,
        "orders": generated_orders,
        "gaps_resolved": gaps_resolved,
        "complete": complete,
        "outcome": session.get("outcome"),
        "next_scenario": session.get("next_scenario"),
        "cost_usd": turn_cost,
        "session": session,
    }


@router.post("/{session_id}/rerun-turn/{turn_num}")
async def rerun_turn(
    session_id: str,
    turn_num: int,
    req: TurnRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Rerun turn `turn_num` using its stored scenario. Replaces the turn record
    and truncates all subsequent turns, then regenerates next_scenario.
    Reactivates a completed session if the last turn is rerun.
    """
    try:
        session = load_session(session_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")

    turns = session.get("turns", [])
    if turn_num < 1 or turn_num > len(turns):
        raise HTTPException(status_code=400, detail=f"Turn {turn_num} not found (session has {len(turns)} turns)")

    # Retrieve the original scenario for this turn
    original_turn = turns[turn_num - 1]
    scenario = original_turn.get("scenario")
    if not scenario:
        raise HTTPException(status_code=400, detail=f"Turn {turn_num} has no stored scenario")

    model = req.model or session.get("model") or None
    log.info("Viva %s RERUN turn %d", session_id, turn_num)

    # ── 1. Student assessment (agentic — queries MongoDB with tools) ──────────
    snap_result = await asyncio.to_thread(
        run_viva_student_turn,
        scenario.get("clinical_context", ""),
        scenario.get("question", ""),
        scenario.get("phase", "MANAGEMENT"),
        scenario.get("difficulty", "MEDIUM"),
        VIVA_DUMMY_CPMRN,
        kb,
        model,
    )
    student_snap = snap_result["snapshots"][0]
    student_answer_text = _build_student_answer_text(student_snap)
    turn_cost = student_snap.get("cost_usd", 0.0)
    chat_run_id = student_snap.get("chat_run_id")

    # ── 2. Order generation ───────────────────────────────────────────────────
    immediate_steps: list[str] = student_snap.get("immediate_next_steps", [])
    monitoring_steps: list[str] = student_snap.get("monitoring_followup", [])
    all_steps = immediate_steps + monitoring_steps
    generated_orders: list[dict] = []
    order_run_id: str | None = None
    order_cost = 0.0

    if all_steps:
        try:
            active_orders_resp = get_active_orders(VIVA_DUMMY_CPMRN)
            active_orders = active_orders_resp.get("active_orders") if not active_orders_resp.get("error") else None
        except Exception as exc:
            log.warning("Could not load active orders for rerun turn: %s", exc)
            active_orders = None

        try:
            order_result = await asyncio.to_thread(
                run_order_generation,
                all_steps,
                VIVA_DUMMY_CPMRN,
                "adult",
                model,
                kb,
                active_orders,
                chat_run_id,
            )
            raw_orders = order_result.get("orders", [])
            order_run_id = order_result.get("run_id")
            order_cost = order_result.get("cost_usd", 0.0)
            generated_orders = _dedup_orders(raw_orders)
        except Exception as exc:
            log.warning("Order generation failed for rerun turn %d: %s", turn_num, exc)

    turn_cost += order_cost

    # ── 3. Gap resolution ─────────────────────────────────────────────────────
    gaps_before = _count_pending_gaps(kb)
    gap_stats = await asyncio.to_thread(resolve_all_gaps, kb, _VIVA_GAP_MAX_RESULTS)
    gaps_resolved = max(0, gaps_before - _count_pending_gaps(kb))
    turn_cost += gap_stats.get("cost_usd", 0.0)

    # ── Persist — replace turn in-place, keep everything else ────────────────
    # We deliberately do NOT truncate subsequent turns or re-run the teacher.
    # Subsequent turns stay visible; session status/next_scenario unchanged.
    new_turn_record = {
        "turn_num": turn_num,
        "scenario": scenario,
        "chat_run_id": chat_run_id,
        "order_run_id": order_run_id,
        "student_answer_text": student_answer_text,
        "student_snap": {
            "clinical_direction":   student_snap.get("clinical_direction", []),
            "clinical_reasoning":   student_snap.get("clinical_reasoning", []),
            "specific_parameters":  student_snap.get("specific_parameters", []),
            "monitoring_followup":  student_snap.get("monitoring_followup", []),
            "alternative_considerations": student_snap.get("alternative_considerations", []),
            "pages_consulted":      student_snap.get("pages_consulted", []),
            "gap_sections":         student_snap.get("gap_sections", []),
        },
        "orders": generated_orders,
        "gaps_resolved": gaps_resolved,
        "cost_usd": turn_cost,
        "rerun": True,
    }

    session["turns"][turn_num - 1] = new_turn_record
    session["total_cost_usd"] = round(
        sum(t.get("cost_usd", 0.0) for t in session["turns"]), 6
    )

    save_session(session, kb)

    return {
        "turn_num": turn_num,
        "student_answer": student_answer_text,
        "orders": generated_orders,
        "gaps_resolved": gaps_resolved,
        "complete": session["status"] == "complete",
        "outcome": session.get("outcome"),
        "next_scenario": session.get("next_scenario"),
        "cost_usd": turn_cost,
        "session": session,
    }


@router.get("/provenance")
def get_provenance(order_run_id: str):
    """
    Return the order-gen + upstream chat provenance traces for a given order_run_id.
    Scans the last 14 days of JSONL trace files.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td
    from ..config import TRACES_DIR

    def _find(prefix: str, run_id: str) -> dict | None:
        for delta in range(14):
            date_str = (_dt.utcnow() - _td(days=delta)).strftime("%Y-%m-%d")
            path = TRACES_DIR / f"{prefix}_{date_str}.jsonl"
            if not path.exists():
                continue
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                        if rec.get("run_id") == run_id:
                            return rec
                    except Exception:
                        continue
        return None

    order_trace = _find("order_gen", order_run_id)
    if not order_trace:
        raise HTTPException(status_code=404, detail=f"No trace found for run_id {order_run_id!r}")

    chat_trace = None
    parent_run_id = order_trace.get("parent_run_id")
    if parent_run_id:
        chat_trace = _find("chat", parent_run_id)

    return {"order_trace": order_trace, "chat_trace": chat_trace}


@router.get("/")
def list_viva_sessions(kb: KBConfig = Depends(resolve_kb)):
    return {"sessions": list_sessions(kb)}


@router.get("/{session_id}")
def get_viva_session(session_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        return {"session": load_session(session_id, kb)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")


@router.delete("/{session_id}")
def delete_viva_session(session_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        delete_session(session_id, kb)
        return {"deleted": session_id}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")


def _build_student_answer_text(snap: dict) -> str:
    """
    Build a readable summary of the student's CDS answer from the structured
    snapshot fields. The 'agent_answer' field is not populated by the clinical
    assessment pipeline, so we reconstruct from what is actually stored.
    """
    parts = []
    if snap.get("clinical_direction"):
        parts.append("Clinical direction:\n" + "\n".join(f"- {x}" for x in snap["clinical_direction"]))
    if snap.get("clinical_reasoning"):
        parts.append("Reasoning:\n" + "\n".join(f"- {x}" for x in snap["clinical_reasoning"]))
    if snap.get("specific_parameters"):
        parts.append("Specific parameters:\n" + "\n".join(f"- {x}" for x in snap["specific_parameters"]))
    if snap.get("monitoring_followup"):
        parts.append("Monitoring/follow-up:\n" + "\n".join(f"- {x}" for x in snap["monitoring_followup"]))
    return "\n\n".join(parts) or snap.get("agent_answer", "")


def _count_pending_gaps(kb: KBConfig) -> int:
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return 0
    return sum(
        1 for f in gaps_dir.glob("*.md")
        if not f.stem.startswith("patient-")
    )
