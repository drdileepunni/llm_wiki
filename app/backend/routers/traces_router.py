"""
Traces API — read-only access to CDS/order-gen trace data from two sources:

  1. JSONL files  — app/traces/*.jsonl  (historical, up to May 20)
  2. MongoDB      — pipeline_traces collection (live scheduler runs)

GET /api/traces/runs                  → list of hourly run buckets across both sources
GET /api/traces/runs/{date}/{hour}    → patient entries in that bucket
GET /api/traces/entry/{run_id}        → full trace entry (JSONL or MongoDB)

MongoDB run IDs are prefixed "sched_" to distinguish from JSONL UUIDs.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/traces", tags=["traces"])
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
TRACES_DIR = _ROOT / "app" / "traces"

# MongoDB step order for display
_SCHED_STEPS = ["pass1_screener", "status_classifier", "problem_tracker"]


# ── Mongo access ───────────────────────────────────────────────────────────────

def _db():
    from backend.services.emr.db import get_db
    return get_db()


def _ser(obj):
    """Recursively make a MongoDB doc JSON-serialisable."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(i) for i in obj]
    return obj


# ── JSONL helpers ──────────────────────────────────────────────────────────────

def _infer_mode(path: Path) -> str:
    return "order_gen" if path.stem.startswith("order_gen") else "cds"


def _load_jsonl_entries() -> list[dict]:
    entries: list[dict] = []
    if not TRACES_DIR.exists():
        return entries
    for path in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        mode = _infer_mode(path)
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if "mode" not in e:
                            e["mode"] = mode
                        entries.append(e)
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
    return entries


def _parse_patient_meta(entry: dict) -> dict:
    meta = {"cpmrn": "—", "unit": "—", "bed": "—", "brief": "—"}
    if entry.get("cpmrn"):
        meta["cpmrn"] = entry["cpmrn"]
        meta["brief"] = entry.get("patient_type", "—")
        return meta
    question = entry.get("question", "")
    for line in question.split("\n")[:6]:
        line = line.strip()
        if line.startswith("Patient:"):
            meta["brief"] = line.replace("Patient:", "").strip()
        m = re.search(r"CPMRN:\s*(\S+)", line)
        if m:
            meta["cpmrn"] = m.group(1)
        m = re.search(r"Unit:\s*(\S+)", line)
        if m:
            meta["unit"] = m.group(1)
        m = re.search(r"Bed:\s*(\S+)", line)
        if m:
            meta["bed"] = m.group(1)
    return meta


def _parse_question_sections(question: str) -> dict:
    sections: dict[str, str] = {}
    current_key = "header"
    current_lines: list[str] = []
    section_map = {
        "--- LONGITUDINAL CONTEXT ---": "longitudinal",
        "--- CURRENT VITALS ---": "vitals",
        "--- RECENT LABS ---": "labs",
        "--- ACTIVE ORDERS ---": "orders",
        "--- FLUID BALANCE": "fluid",
    }
    for line in question.split("\n"):
        matched = None
        for marker, key in section_map.items():
            if marker in line:
                matched = key
                break
        if matched:
            sections[current_key] = "\n".join(current_lines).strip()
            current_key = matched
            current_lines = []
        else:
            current_lines.append(line)
    sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _step_cost(entry: dict) -> dict[str, float | None]:
    total_usd: float | None = (entry.get("tokens") or {}).get("cost_usd")
    if total_usd is None:
        return {"step1": None, "step2": None, "step3": None, "total": None}

    def weighted(step_key: str) -> float:
        s = entry.get(step_key) or {}
        return s.get("input_tokens", 0) + 4 * s.get("output_tokens", 0)

    w1, w2, w3 = weighted("step1"), weighted("step2"), weighted("step3")
    total_w = w1 + w2 + w3 or 1
    return {
        "step1": round(total_usd * w1 / total_w, 6),
        "step2": round(total_usd * w2 / total_w, 6),
        "step3": round(total_usd * w3 / total_w, 6),
        "total": round(total_usd, 6),
    }


def _bucket_key(ts) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(str(ts)) if isinstance(ts, str) else ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H")
    except Exception:
        return "unknown", "00"


# ── MongoDB (scheduler) helpers ────────────────────────────────────────────────

def _sched_run_id(cpmrn: str, encounter: int, date: str, hour: str) -> str:
    return f"sched_{cpmrn}_{encounter}_{date}_{hour}"


def _load_mongo_buckets() -> dict[tuple, dict]:
    """
    Group pipeline_traces by (CPMRN, encounter, date, hour) into buckets.
    Returns {(date, hour, 'scheduler'): {count, cost_usd}}.
    """
    buckets: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "cost_usd": 0.0})
    try:
        db = _db()
        coll = db["pipeline_traces"]
        # Only need CPMRN + started_at for bucketing
        for doc in coll.find({}, {"CPMRN": 1, "encounter": 1, "started_at": 1, "total_tokens": 1}):
            date, hour = _bucket_key(doc["started_at"])
            key = (date, hour, "scheduler")
            b = buckets[key]
            b["count"] += 1
            tt = doc.get("total_tokens") or {}
            # rough cost: input 0.075/1M, output 0.30/1M, thinking 3.50/1M
            b["cost_usd"] += (
                tt.get("in", 0) * 0.075 / 1e6
                + tt.get("out", 0) * 0.30 / 1e6
                + tt.get("thinking", 0) * 3.50 / 1e6
            )
    except Exception as exc:
        logger.warning("Could not load MongoDB pipeline_traces: %s", exc)
    return buckets


def _load_mongo_patients(date: str, hour: str) -> list[dict]:
    """
    Return one row per unique (CPMRN, encounter) in the given hour bucket,
    aggregating all their pipeline_traces step docs into one patient entry.
    """
    patients: dict[tuple, dict] = {}
    try:
        db = _db()
        coll = db["pipeline_traces"]
        for doc in coll.find({}, {
            "CPMRN": 1, "encounter": 1, "started_at": 1,
            "step": 1, "total_tokens": 1, "duration_ms": 1,
        }):
            d, h = _bucket_key(doc["started_at"])
            if d != date or h != hour:
                continue
            cpmrn = doc["CPMRN"]
            enc   = doc.get("encounter", 0)
            key   = (cpmrn, enc)
            if key not in patients:
                patients[key] = {
                    "run_id":    _sched_run_id(cpmrn, enc, date, hour),
                    "timestamp": doc["started_at"].isoformat()
                                 if isinstance(doc["started_at"], datetime)
                                 else str(doc["started_at"]),
                    "mode":      "scheduler",
                    "model":     "—",
                    "cpmrn":     cpmrn,
                    "unit":      "—",
                    "bed":       "—",
                    "brief":     "",
                    "cost_usd":  0.0,
                    "tokens":    {"in": 0, "out": 0, "thinking": 0},
                    "steps":     [],
                }
            p = patients[key]
            tt = doc.get("total_tokens") or {}
            p["cost_usd"] += (
                tt.get("in", 0) * 0.075 / 1e6
                + tt.get("out", 0) * 0.30 / 1e6
                + tt.get("thinking", 0) * 3.50 / 1e6
            )
            p["tokens"]["in"]       += tt.get("in", 0)
            p["tokens"]["out"]      += tt.get("out", 0)
            p["tokens"]["thinking"] += tt.get("thinking", 0)
            p["steps"].append(doc.get("step", ""))

        # Fetch patient context (brief) for each patient
        db2 = _db()
        pc_coll = db2["patient_contexts"]
        for (cpmrn, enc), p in patients.items():
            try:
                ctx = pc_coll.find_one(
                    {"CPMRN": cpmrn, "encounter": enc},
                    {"structured_summary": 1, "_id": 0},
                )
                if ctx:
                    ss = ctx.get("structured_summary") or {}
                    narrative = ss.get("admission_narrative", "")
                    p["brief"] = narrative[:80] if narrative else "—"
            except Exception:
                pass
            p["cost_usd"] = round(p["cost_usd"], 6)

    except Exception as exc:
        logger.warning("Could not load MongoDB patients for %s %s: %s", date, hour, exc)

    return sorted(patients.values(), key=lambda r: r["timestamp"])


def _get_mongo_entry(cpmrn: str, encounter: int, date: str, hour: str) -> dict:
    """Fetch all pipeline_traces steps for a patient run, plus patient context."""
    steps: dict[str, dict] = {}
    total_tokens = {"in": 0, "out": 0, "thinking": 0}
    total_cost   = 0.0
    first_ts     = None

    try:
        db = _db()
        coll = db["pipeline_traces"]
        for doc in coll.find({"CPMRN": cpmrn, "encounter": encounter}):
            d, h = _bucket_key(doc["started_at"])
            if d != date or h != hour:
                continue
            step = doc.get("step", "unknown")
            clean = _ser({k: v for k, v in doc.items() if k != "_id"})
            steps[step] = clean
            tt = doc.get("total_tokens") or {}
            total_tokens["in"]       += tt.get("in", 0)
            total_tokens["out"]      += tt.get("out", 0)
            total_tokens["thinking"] += tt.get("thinking", 0)
            total_cost += (
                tt.get("in", 0) * 0.075 / 1e6
                + tt.get("out", 0) * 0.30 / 1e6
                + tt.get("thinking", 0) * 3.50 / 1e6
            )
            if first_ts is None:
                ts = doc["started_at"]
                first_ts = ts.isoformat() if isinstance(ts, datetime) else str(ts)

        # Fetch patient context for inputs
        pc = db["patient_contexts"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"_id": 0},
        )
        context = _ser(pc) if pc else {}

    except Exception as exc:
        logger.warning("Could not fetch MongoDB entry %s %s: %s", cpmrn, encounter, exc)
        steps   = {}
        context = {}

    return {
        "run_id":    _sched_run_id(cpmrn, encounter, date, hour),
        "source":    "mongodb",
        "timestamp": first_ts,
        "mode":      "scheduler",
        "model":     "—",
        "meta":      {"cpmrn": cpmrn, "unit": "—", "bed": "—", "brief": ""},
        "steps":     steps,
        "context":   context,
        "tokens":    total_tokens,
        "costs":     {"total": round(total_cost, 6)},
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(mode: str | None = Query(None)):
    """
    Return hourly run buckets from JSONL files + MongoDB, newest first.
    """
    # JSONL buckets
    entries = _load_jsonl_entries()
    buckets: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "cost_usd": 0.0})
    for e in entries:
        entry_mode = e.get("mode", "cds")
        if mode and entry_mode != mode:
            continue
        date, hour = _bucket_key(e.get("timestamp", ""))
        key = (date, hour, entry_mode)
        buckets[key]["count"]    += 1
        buckets[key]["cost_usd"] += (e.get("tokens") or {}).get("cost_usd", 0.0)

    # MongoDB buckets (skip if mode filter excludes scheduler)
    if not mode or mode == "scheduler":
        for key, b in _load_mongo_buckets().items():
            buckets[key]["count"]    += b["count"]
            buckets[key]["cost_usd"] += b["cost_usd"]

    result = []
    for key in sorted(buckets.keys(), reverse=True, key=lambda k: (k[0], k[1])):
        date, hour, entry_mode = key
        b = buckets[key]
        result.append({
            "date":     date,
            "hour":     hour,
            "mode":     entry_mode,
            "count":    b["count"],
            "cost_usd": round(b["cost_usd"], 6),
            "label":    f"{date} {hour}:00",
        })

    return {"runs": result}


@router.get("/runs/{date}/{hour}")
def get_run_patients(date: str, hour: str, mode: str | None = Query(None)):
    """
    Return all patient entries in a given hour bucket, from both sources.
    """
    results = []

    # JSONL patients
    if not mode or mode != "scheduler":
        entries = _load_jsonl_entries()
        for e in entries:
            entry_mode = e.get("mode", "cds")
            if mode and entry_mode != mode:
                continue
            d, h = _bucket_key(e.get("timestamp", ""))
            if d != date or h != hour:
                continue
            meta  = _parse_patient_meta(e)
            costs = _step_cost(e)
            results.append({
                "run_id":    e.get("run_id", ""),
                "timestamp": e.get("timestamp", ""),
                "mode":      entry_mode,
                "model":     e.get("model", ""),
                "cpmrn":     meta["cpmrn"],
                "unit":      meta["unit"],
                "bed":       meta["bed"],
                "brief":     meta["brief"],
                "cost_usd":  costs["total"],
                "tokens":    e.get("tokens", {}),
            })

    # MongoDB (scheduler) patients
    if not mode or mode == "scheduler":
        results.extend(_load_mongo_patients(date, hour))

    results.sort(key=lambda r: r["timestamp"])
    return {"patients": results}


@router.get("/entry/{run_id:path}")
def get_entry(run_id: str):
    """
    Return the full trace entry for a given run_id.
    Handles both JSONL UUIDs and MongoDB 'sched_*' IDs.
    """
    # MongoDB entry: sched_{CPMRN}_{encounter}_{date}_{hour}
    if run_id.startswith("sched_"):
        parts = run_id[len("sched_"):].split("_")
        # parts = [CPMRN, encounter, date, hour]
        # CPMRN itself may contain underscores, so we take last 3 as enc/date/hour
        if len(parts) >= 4:
            hour = parts[-1]
            date = parts[-2]
            enc  = parts[-3]
            cpmrn = "_".join(parts[:-3])
            try:
                encounter = int(enc)
            except ValueError:
                raise HTTPException(status_code=400, detail="Malformed sched run_id")
            return _get_mongo_entry(cpmrn, encounter, date, hour)
        raise HTTPException(status_code=400, detail="Malformed sched run_id")

    # JSONL entry
    for e in _load_jsonl_entries():
        if e.get("run_id") == run_id:
            meta     = _parse_patient_meta(e)
            sections = _parse_question_sections(e.get("question", ""))
            costs    = _step_cost(e)
            return {
                "run_id":          e.get("run_id"),
                "source":          "jsonl",
                "timestamp":       e.get("timestamp"),
                "mode":            e.get("mode"),
                "model":           e.get("model"),
                "reasoning_model": e.get("reasoning_model"),
                "kb":              e.get("kb"),
                "meta":            meta,
                "sections":        sections,
                "step1":           e.get("step1", {}),
                "step2":           e.get("step2", {}),
                "step3":           e.get("step3", {}),
                "step4":           e.get("step4", {}),
                "final":           e.get("final", {}),
                "tokens":          e.get("tokens", {}),
                "costs":           costs,
            }

    raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
