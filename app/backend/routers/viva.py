"""
Viva (teacher-student loop) endpoints.

POST /api/viva/start              — start new session (trajectory + first scenario)
POST /api/viva/{session_id}/turn  — run one full turn (student → gaps → teacher)
GET  /api/viva/                   — list sessions
GET  /api/viva/{session_id}       — get session state
DELETE /api/viva/{session_id}     — delete session
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

log = logging.getLogger("wiki.viva")

router = APIRouter(prefix="/api/viva", tags=["viva"])

_VIVA_GAP_MAX_RESULTS = 1  # keep turns fast; LLM escalation kicks in from turn 3+


class StartRequest(BaseModel):
    topic: str
    model: str | None = None
    max_turns: int = 8


class TurnRequest(BaseModel):
    model: str | None = None


@router.post("/start")
async def start_viva(req: StartRequest, kb: KBConfig = Depends(resolve_kb)):
    """Generate trajectory + first scenario and persist a new session."""
    session_id = f"viva_{uuid.uuid4().hex[:8]}"
    model = req.model

    trajectory = await asyncio.to_thread(generate_trajectory, req.topic, model)
    first_scenario = await asyncio.to_thread(
        generate_first_scenario, req.topic, trajectory, model
    )

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

    # First scenario goes into next_scenario; the rest queue up in replay_queue
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

    from ..services.viva_session import save_session
    save_session(session, kb)

    # Register in listing
    from ..services.viva_session import list_sessions  # noqa: ensure dir exists
    return {"session": session}


@router.post("/{session_id}/turn")
async def run_turn(
    session_id: str,
    req: TurnRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Run one complete viva turn:
      1. Student assessment (wiki-grounded)
      2. Gap resolution (blocking)
      3. Teacher generates next scenario or signals completion
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

    # ── 2. Gap resolution ─────────────────────────────────────────────────────
    log.info("Viva %s turn %d: resolving gaps", session_id, turn_num)
    gaps_before = _count_pending_gaps(kb)
    gap_stats = await asyncio.to_thread(resolve_all_gaps, kb, _VIVA_GAP_MAX_RESULTS)
    gaps_resolved = max(0, gaps_before - _count_pending_gaps(kb))
    turn_cost += gap_stats.get("cost_usd", 0.0)
    log.info(
        "Viva %s turn %d: %d gaps resolved, cost=$%.4f",
        session_id, turn_num, gaps_resolved, gap_stats.get("cost_usd", 0),
    )

    # ── 3. Advance case: replay queue OR teacher ──────────────────────────────
    replay_queue: list = session.get("replay_queue", [])
    if "replay_queue" in session:
        # Forked/replay session — serve stored scenarios without calling teacher.
        # Empty queue means there is no scenario left to serve after this turn → complete.
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
        "student_answer_text": student_answer_text,
        "student_snap": {
            "immediate_next_steps": student_snap.get("immediate_next_steps", []),
            "clinical_direction":   student_snap.get("clinical_direction", []),
            "specific_parameters":  student_snap.get("specific_parameters", []),
            "monitoring_followup":  student_snap.get("monitoring_followup", []),
            "alternative_considerations": student_snap.get("alternative_considerations", []),
            "pages_consulted":      student_snap.get("pages_consulted", []),
            "gap_sections":         student_snap.get("gap_sections", []),
        },
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
        "gaps_resolved": gaps_resolved,
        "complete": complete,
        "outcome": session.get("outcome"),
        "next_scenario": session.get("next_scenario"),
        "cost_usd": turn_cost,
        "session": session,
    }


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
