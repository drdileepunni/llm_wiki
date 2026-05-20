"""
Radar CDS Replay endpoints.

GET  /api/radar-replay/patients                    — list patients with snapshots
GET  /api/radar-replay/{cpmrn}/{encounter}/snapshots — list snapshots for a patient
GET  /api/radar-replay/{cpmrn}/{encounter}/results   — get stored replay results
POST /api/radar-replay/{cpmrn}/{encounter}/replay    — start a sequential replay (async)
GET  /api/radar-replay/replay/{run_id}              — poll replay status
"""
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/radar-replay", tags=["radar-replay"])

# ── Ensure repo root on path ───────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── In-memory run registry ─────────────────────────────────────────────────
_runs: dict[str, dict] = {}
# { run_id: { status, cpmrn, encounter, progress, total, error, started_at, finished_at } }


# ── Patients ───────────────────────────────────────────────────────────────
@router.get("/patients")
def get_patients():
    from tools.radar_sync.replay_pipeline import list_patients
    return {"patients": list_patients()}


# ── Snapshots for a patient ────────────────────────────────────────────────
@router.get("/{cpmrn}/{encounter}/snapshots")
def get_snapshots(cpmrn: str, encounter: int):
    from tools.radar_sync.replay_pipeline import list_snapshots
    return {"snapshots": list_snapshots(cpmrn, encounter)}


# ── Results for a patient ──────────────────────────────────────────────────
@router.get("/{cpmrn}/{encounter}/results")
def get_results(cpmrn: str, encounter: int):
    from tools.radar_sync.replay_pipeline import get_replay_results
    return {"results": get_replay_results(cpmrn, encounter)}


class ReplayRequest(BaseModel):
    from_index: int = 0
    force: bool = False   # when True: delete all results and re-run every snapshot from scratch


# ── Start replay ───────────────────────────────────────────────────────────
@router.post("/{cpmrn}/{encounter}/replay")
def start_replay(cpmrn: str, encounter: int, body: ReplayRequest = ReplayRequest()):
    from_index = 0 if body.force else body.from_index
    force      = body.force
    run_id     = str(uuid.uuid4())
    _runs[run_id] = {
        "status":      "running",
        "cpmrn":       cpmrn,
        "encounter":   encounter,
        "from_index":  from_index,
        "force":       force,
        "progress":    0,
        "total":       0,
        "current_ts":  None,
        "error":       None,
        "started_at":  datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
    }

    def _run():
        try:
            import importlib, sys as _sys
            # Ensure both app/ and repo root are on sys.path
            _root = _ROOT
            for _p in [str(_root / "app"), str(_root)]:
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)

            # Force fresh load of tools modules so changes are always picked up
            for _mod in ["tools.radar_sync.summary_updater", "tools.radar_sync.replay_pipeline"]:
                if _mod in _sys.modules:
                    importlib.reload(_sys.modules[_mod])

            from tools.radar_sync.replay_pipeline import replay, list_snapshots

            def progress_cb(i, total, snap_ts):
                _runs[run_id]["progress"]   = i
                _runs[run_id]["total"]      = total
                _runs[run_id]["current_ts"] = snap_ts

            snaps = list_snapshots(cpmrn, encounter)
            _runs[run_id]["total"] = len(snaps)

            replay(cpmrn, encounter, from_index=from_index, force=force, progress_cb=progress_cb)
            _runs[run_id]["status"]      = "done"
            _runs[run_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _runs[run_id]["status"] = "error"
            _runs[run_id]["error"]  = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"run_id": run_id, "status": "running", "from_index": from_index, "force": force}


# ── Poll replay status ─────────────────────────────────────────────────────
@router.get("/replay/{run_id}")
def get_replay_status(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    return _runs[run_id]
