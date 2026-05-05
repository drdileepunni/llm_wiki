"""
Persistent process state. active_medgemma survives server restarts via a small
file in the app directory. Defaults to "gpu" if the file doesn't exist.
"""
import os
from pathlib import Path

_STATE_FILE = Path(__file__).parent.parent / ".medgemma_active"

def _load() -> str:
    try:
        val = _STATE_FILE.read_text().strip()
        return val if val in ("gpu", "cpu") else "gpu"
    except Exception:
        return "gpu"

def _save(val: str) -> None:
    try:
        _STATE_FILE.write_text(val)
    except Exception:
        pass

active_medgemma: str = _load()


def set_active(val: str) -> None:
    global active_medgemma
    active_medgemma = val
    _save(val)
