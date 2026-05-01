"""
Viva session persistence — read/write JSON files under assessments/viva/.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from ..config import KBConfig

log = logging.getLogger("wiki.viva_session")


def _viva_dir(kb: KBConfig) -> Path:
    return kb.wiki_root / "assessments" / "viva"


def _viva_path(session_id: str, kb: KBConfig) -> Path:
    return _viva_dir(kb) / f"{session_id}.json"


def create_session(
    session_id: str,
    topic: str,
    trajectory: str,
    first_scenario: dict,
    max_turns: int,
    model: str,
    kb_name: str,
    kb: KBConfig,
) -> dict:
    session = {
        "session_id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "topic": topic,
        "trajectory": trajectory,
        "max_turns": max_turns,
        "current_turn": 0,
        "status": "active",
        "model": model,
        "kb_name": kb_name,
        "turns": [],
        "next_scenario": first_scenario,
        "outcome": None,
        "total_cost_usd": 0.0,
    }
    _save(session, kb)
    return session


def load_session(session_id: str, kb: KBConfig) -> dict:
    p = _viva_path(session_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"Viva session not found: {session_id}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_session(session: dict, kb: KBConfig) -> None:
    _save(session, kb)


def _save(session: dict, kb: KBConfig) -> None:
    p = _viva_path(session["session_id"], kb)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")


def delete_session(session_id: str, kb: KBConfig) -> None:
    p = _viva_path(session_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"Viva session not found: {session_id}")
    p.unlink()


def list_sessions(kb: KBConfig) -> list[dict]:
    d = _viva_dir(kb)
    if not d.exists():
        return []
    results = []
    for f in sorted(d.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append({
                "session_id": data["session_id"],
                "topic": data.get("topic", ""),
                "status": data.get("status", ""),
                "current_turn": data.get("current_turn", 0),
                "max_turns": data.get("max_turns", 0),
                "created_at": data.get("created_at", ""),
                "total_cost_usd": data.get("total_cost_usd", 0.0),
                "outcome": data.get("outcome"),
            })
        except Exception as exc:
            log.warning("Skipping corrupt viva session %s: %s", f.name, exc)
    return results
