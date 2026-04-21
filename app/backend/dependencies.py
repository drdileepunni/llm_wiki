from fastapi import Header, HTTPException
from .config import KBConfig, get_kb


def resolve_kb(x_kb_name: str = Header(default="agent_school")) -> KBConfig:
    try:
        return get_kb(x_kb_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
