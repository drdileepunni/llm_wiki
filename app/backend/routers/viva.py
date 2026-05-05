"""
Viva (teacher-student loop) endpoints.

POST /api/viva/patient              — create / upsert dummy patient
GET  /api/viva/patient              — get dummy patient
POST /api/viva/patient/place-order  — place / edit / stop one order on dummy patient
POST /api/viva/start                — start new session (trajectory + first scenario)
POST /api/viva/{session_id}/turn    — run one full turn (student → order-gen → gaps → teacher)
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
from ..services.clinical_assess_pipeline import run_custom_snapshot
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
    reset_dummy_patient_orders,
    upsert_dummy_patient,
    VIVA_DUMMY_CPMRN,
)
from ..services.order_gen_pipeline import run_order_generation

log = logging.getLogger("wiki.viva")

router = APIRouter(prefix="/api/viva", tags=["viva"])

_VIVA_GAP_MAX_RESULTS = 1  # keep turns fast; LLM escalation kicks in from turn 3+


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


# ── Session endpoints ──────────────────────────────────────────────────────────

@router.post("/start")
async def start_viva(req: StartRequest, kb: KBConfig = Depends(resolve_kb)):
    """Generate trajectory + first scenario and persist a new session."""
    session_id = f"viva_{uuid.uuid4().hex[:8]}"
    model = req.model

    trajectory = await asyncio.to_thread(generate_trajectory, req.topic, model)
    first_scenario = await asyncio.to_thread(
        generate_first_scenario, req.topic, trajectory, model
    )

    # Reset dummy patient orders at the start of every new session
    try:
        reset_dummy_patient_orders()
    except Exception as exc:
        log.warning("Could not reset dummy patient orders: %s", exc)

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


@router.post("/{session_id}/turn")
async def run_turn(
    session_id: str,
    req: TurnRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Run one complete viva turn:
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

    # ── 1. Student assessment ─────────────────────────────────────────────────
    log.info("Viva %s turn %d: student assessment", session_id, turn_num)
    snap_result = await asyncio.to_thread(
        run_custom_snapshot,
        {
            "clinical_context": next_scenario.get("clinical_context", ""),
            "csv_content": next_scenario.get("csv_content", ""),
            "question": next_scenario.get("question", ""),
            "phase": next_scenario.get("phase", "MANAGEMENT"),
            "difficulty": next_scenario.get("difficulty", "MEDIUM"),
        },
        kb,
        model,
        None,  # reasoning_model
    )
    student_snap = snap_result["snapshots"][0]
    student_answer_text = student_snap.get("agent_answer", "")
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

            # Enforce: no catalog match → must be low confidence (goes to instructions card)
            for order in raw_orders:
                name = (order.get("orderable_name") or "").strip()
                if not name or name == "—":
                    order["orderable_name"] = None
                    order["confidence"] = "low"

            # Dedup by (order_type, orderable_name) — keep first, merge notes/instructions
            seen: dict[tuple, int] = {}
            deduped: list[dict] = []
            for order in raw_orders:
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
            generated_orders = deduped

            log.info(
                "Viva %s turn %d: %d orders generated (%d after dedup)  cost=$%.4f",
                session_id, turn_num, len(raw_orders), len(generated_orders), order_cost,
            )
        except Exception as exc:
            log.warning("Order generation failed for viva turn %d: %s", turn_num, exc)

    turn_cost += order_cost

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
            model,
        )

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
        "gaps_resolved": gaps_resolved,
        "cost_usd": turn_cost,
    }

    session["turns"].append(turn_record)
    session["current_turn"] = turn_num
    session["total_cost_usd"] = round(session.get("total_cost_usd", 0.0) + turn_cost, 6)

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


def _count_pending_gaps(kb: KBConfig) -> int:
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return 0
    return sum(
        1 for f in gaps_dir.glob("*.md")
        if not f.stem.startswith("patient-")
    )
