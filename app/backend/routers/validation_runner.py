"""
Validation runner endpoints — launch/monitor A/B test runs from the UI.
"""
import json, os, re, signal, subprocess, sys, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/validation", tags=["validation"])

REPO_ROOT      = Path(__file__).parent.parent.parent.parent   # repo root
VALIDATION_DIR = REPO_ROOT / "validation"
DATA_DIR       = VALIDATION_DIR / "data"
VENV_PYTHON    = REPO_ROOT / "app" / ".venv" / "bin" / "python3"

# In-memory run registry
_runs: dict[str, dict] = {}
# { run_id: { status, log, output_file, started_at, n_cases_done, n_cases_total } }
_serve_proc      = None
_ngrok_url       = None
_serve_local_url = None

# ── Catalog — diagnosis groups from scenario_catalog ──────────────────────
@router.get("/catalog")
def get_catalog():
    from ..services.scenario_catalog import get_catalog_stats
    return get_catalog_stats()

# ── List completed result files ────────────────────────────────────────────
@router.get("/results")
def list_results():
    files = sorted(DATA_DIR.glob("ab_results_*.json"), reverse=True)
    out = []
    for f in files:
        try:
            d = json.loads(f.read_text())
            cases = d.get("cases", [])
            n_scenarios = len(set(c.get("scenario_id","") for c in cases))
            n_turns     = len(cases)
            label       = d.get("label", "")   # optional user-set label
            out.append({
                "filename":     f.name,
                "label":        label,
                "generated_at": d.get("generated_at", ""),
                "n_scenarios":  n_scenarios,
                "n_turns":      n_turns,
                "kb":           d.get("kb", ""),
                "diagnosis":    d.get("diagnosis_filter", ""),
                "mode":         d.get("mode", ""),
                "arm_a":        d.get("arm_a_config", {}),
                "arm_b":        d.get("arm_b_config", {}),
            })
        except Exception:
            out.append({"filename": f.name, "n_scenarios": "?", "n_turns": "?"})
    return {"results": out}


# ── Delete a result file ───────────────────────────────────────────────────
@router.delete("/results/{filename}")
def delete_result(filename: str):
    if not filename.startswith("ab_results_") or not filename.endswith(".json"):
        raise HTTPException(400, "Invalid filename")
    p = DATA_DIR / filename
    if not p.exists():
        raise HTTPException(404, "File not found")
    p.unlink()
    return {"ok": True, "deleted": filename}

# ── Rename / label a result file ───────────────────────────────────────────
class RenameRequest(BaseModel):
    filename: str
    label: str          # human-readable label stored inside the JSON

@router.post("/results/label")
def label_result(req: RenameRequest):
    p = DATA_DIR / req.filename
    if not p.exists():
        raise HTTPException(404, "File not found")
    if not req.filename.startswith("ab_results_") or not req.filename.endswith(".json"):
        raise HTTPException(400, "Invalid filename")
    d = json.loads(p.read_text())
    d["label"] = req.label.strip()
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    return {"ok": True, "label": d["label"]}

# ── Analysis of reviewer files ────────────────────────────────────────────
RESULTS_DIR = VALIDATION_DIR / "results"

@router.get("/analysis")
def get_analysis():
    """
    Read all ab_reviewer_*.json files and compute per-arm star rating statistics.
    The new reviewer format shows ONE arm per scenario (arm_shown: arm_a | arm_b)
    and collects 1-5 star ratings per turn.
    """
    files = sorted(RESULTS_DIR.glob("ab_reviewer_*.json"), reverse=True)
    if not files:
        return {"n_reviewers": 0, "arm_a": None, "arm_b": None, "scenarios": [], "reviewers": []}

    arm_scores: dict[str, list[float]] = {"arm_a": [], "arm_b": []}
    scenario_rows = []
    reviewer_summaries = []

    for f in files:
        try:
            rev = json.loads(f.read_text())
        except Exception:
            continue

        reviewer_name = rev.get("reviewer_name", f.stem)
        rev_arm_scores: dict[str, list[float]] = {"arm_a": [], "arm_b": []}

        for sc in rev.get("scenarios", []):
            arm = sc.get("arm_shown", "")    # "arm_a" or "arm_b"
            turns = sc.get("turns", [])
            ratings = [t.get("overall") for t in turns if isinstance(t.get("overall"), (int, float))]
            notes   = [t.get("notes", "") for t in turns if t.get("notes", "").strip()]

            if ratings and arm in arm_scores:
                avg = sum(ratings) / len(ratings)
                arm_scores[arm].append(avg)
                rev_arm_scores[arm].append(avg)

                scenario_rows.append({
                    "reviewer":    reviewer_name,
                    "scenario_id": sc.get("scenario_id", ""),
                    "arm_shown":   arm,
                    "ratings":     ratings,
                    "avg_rating":  round(avg, 2),
                    "notes":       notes,
                    "n_turns":     len(turns),
                })

        reviewer_summaries.append({
            "name":      reviewer_name,
            "role":      rev.get("reviewer_role", ""),
            "submitted": rev.get("submitted_at", ""),
            "n_scenarios": rev.get("n_scenarios", len(rev.get("scenarios", []))),
            "arm_a_avg": round(sum(rev_arm_scores["arm_a"]) / len(rev_arm_scores["arm_a"]), 2)
                         if rev_arm_scores["arm_a"] else None,
            "arm_b_avg": round(sum(rev_arm_scores["arm_b"]) / len(rev_arm_scores["arm_b"]), 2)
                         if rev_arm_scores["arm_b"] else None,
        })

    def _stats(scores: list[float]) -> dict | None:
        if not scores:
            return None
        avg = sum(scores) / len(scores)
        return {
            "avg":   round(avg, 2),
            "n":     len(scores),
            "min":   round(min(scores), 2),
            "max":   round(max(scores), 2),
        }

    return {
        "n_reviewers": len(reviewer_summaries),
        "arm_a":       _stats(arm_scores["arm_a"]),
        "arm_b":       _stats(arm_scores["arm_b"]),
        "scenarios":   sorted(scenario_rows, key=lambda r: r["scenario_id"]),
        "reviewers":   reviewer_summaries,
    }


# ── Start a run ────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    n_scenarios:    int       = 3
    seed:           int       = 42
    skip_arm_b:     bool      = False
    skip_arm_a:     bool      = False
    skip_order_gen: bool      = False
    kb:             str       = "agent_school"
    diagnosis_ids:  list[str] = []        # empty = all diagnoses
    mode:           str       = "weighted"  # weighted | random
    max_turns:      int       = 4

@router.post("/run")
def start_run(req: RunRequest):
    run_id = str(uuid.uuid4())[:8]
    _runs[run_id] = {
        "status":         "starting",
        "log":            [],
        "output_file":    None,
        "started_at":     time.time(),
        "n_cases_done":   0,
        "n_cases_total":  req.n_scenarios * req.max_turns,  # upper bound
    }

    def _run():
        _runs[run_id]["status"] = "running"
        try:
            from ..services.ab_pipeline import run_ab_batch
            from ..config import get_kb

            kb = get_kb(req.kb)
            _runs[run_id]["log"].append(f"KB: {kb.name}")

            def on_progress(case: dict) -> None:
                _runs[run_id]["n_cases_done"] += 1
                sid  = case.get("scenario_id", "?")
                turn = case.get("turn_num", "?")
                a_ok = (case.get("arm_a") or {}).get("ok", False)
                b_ok = (case.get("arm_b") or {}).get("ok", False)
                _runs[run_id]["log"].append(
                    f"✓ {sid}  turn {turn}  "
                    f"ArmA={'✓' if a_ok else '✗'}  ArmB={'✓' if b_ok else '✗'}"
                )

            result = run_ab_batch(
                n_scenarios   = req.n_scenarios,
                kb            = kb,
                mode          = req.mode,
                seed          = req.seed,
                max_turns     = req.max_turns,
                diagnosis_ids = req.diagnosis_ids or None,
                skip_arm_a    = req.skip_arm_a,
                skip_arm_b    = req.skip_arm_b,
                skip_order_gen= req.skip_order_gen,
                on_progress   = on_progress,
            )

            # Write output file
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            DATA_DIR.mkdir(exist_ok=True)
            out_path = DATA_DIR / f"ab_results_{ts}.json"
            out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
            _runs[run_id]["output_file"] = out_path.name
            _runs[run_id]["log"].append(f"✓ Saved → {out_path.name}")
            _runs[run_id]["status"] = "complete"

        except Exception as e:
            _runs[run_id]["log"].append(f"ERROR: {e}")
            _runs[run_id]["status"] = "failed"

    threading.Thread(target=_run, daemon=True).start()
    return {"run_id": run_id}

# ── Poll run status ────────────────────────────────────────────────────────
@router.get("/run/{run_id}")
def get_run_status(run_id: str):
    if run_id not in _runs:
        raise HTTPException(404, "Run not found")
    r = _runs[run_id]
    return {
        "run_id":        run_id,
        "status":        r["status"],
        "log_tail":      r["log"][-40:],
        "output_file":   r.get("output_file"),
        "elapsed_s":     round(time.time() - r["started_at"], 0),
        "n_cases_done":  r.get("n_cases_done", 0),
        "n_cases_total": r.get("n_cases_total", 0),
    }

# ── Serve a result file via serve_ab.py + ngrok ───────────────────────────
class ServeRequest(BaseModel):
    filename: str
    port: int = 8081

def _kill_port(port: int):
    """Kill any process listening on the given port (macOS/Linux)."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True,
        )
        for pid_str in result.stdout.strip().split():
            try:
                os.kill(int(pid_str), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(0.5)  # brief pause for port to free
    except Exception:
        pass


@router.post("/serve")
def start_serve(req: ServeRequest):
    global _serve_proc, _ngrok_url

    # Kill existing tracked server
    if _serve_proc and _serve_proc.poll() is None:
        _serve_proc.terminate()
        _serve_proc = None
    _ngrok_url = None

    # Kill anything else holding the port
    _kill_port(req.port)

    cmd = [
        str(VENV_PYTHON), str(VALIDATION_DIR / "serve_ab.py"),
        "--results", req.filename,
        "--port", str(req.port),
        "--ngrok",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "app")

    _serve_proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(VALIDATION_DIR), env=env,
    )

    # Wait up to 3s to confirm the process is alive
    deadline = time.time() + 3
    while time.time() < deadline:
        if _serve_proc.poll() is not None:
            # Process died — read remaining output for diagnostics
            break
        time.sleep(0.2)

    local_url = f"http://localhost:{req.port}/ab_reviewer.html"
    serving = _serve_proc.poll() is None

    global _serve_local_url
    _serve_local_url = local_url if serving else None

    # Read ngrok URL in background (doesn't block the response)
    def _watch_for_ngrok():
        global _ngrok_url
        try:
            for line in _serve_proc.stdout:
                if "https://" in line:
                    m = re.search(r'https://[^\s]+', line)
                    if m:
                        _ngrok_url = m.group(0)
                        break
        except Exception:
            pass

    if serving:
        threading.Thread(target=_watch_for_ngrok, daemon=True).start()

    return {
        "local_url": local_url,
        "ngrok_url": _ngrok_url,
        "serving": serving,
    }

@router.get("/serve/status")
def serve_status():
    global _serve_proc, _ngrok_url, _serve_local_url
    running = _serve_proc is not None and _serve_proc.poll() is None
    return {"running": running, "ngrok_url": _ngrok_url, "local_url": _serve_local_url}

@router.post("/serve/stop")
def stop_serve():
    global _serve_proc, _ngrok_url, _serve_local_url
    if _serve_proc and _serve_proc.poll() is None:
        _serve_proc.terminate()
    _serve_proc = None
    _ngrok_url = None
    _serve_local_url = None
    return {"stopped": True}
