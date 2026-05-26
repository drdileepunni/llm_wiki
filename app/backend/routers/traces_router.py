"""
Traces API — read-only access to per-patient CDS/order-gen trace JSONL files.

GET /api/traces/runs                  → list of hourly run buckets (date, hour, mode, count, cost)
GET /api/traces/runs/{date}/{hour}    → patient entries in that bucket (lite — no full question)
GET /api/traces/entry/{run_id}        → full entry for a single run_id
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _infer_mode(path: Path) -> str:
    """Infer trace mode from the filename prefix."""
    name = path.stem  # e.g. "chat_2026-05-20" or "order_gen_2026-05-20"
    if name.startswith("order_gen"):
        return "order_gen"
    return "cds"


def _load_all_entries() -> list[dict]:
    """Read every JSONL trace file and return all entries, newest first."""
    entries: list[dict] = []
    if not TRACES_DIR.exists():
        return entries
    for path in sorted(TRACES_DIR.glob("*.jsonl"), reverse=True):
        inferred_mode = _infer_mode(path)
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # Fill in mode from filename if missing
                        if "mode" not in entry:
                            entry["mode"] = inferred_mode
                        entries.append(entry)
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning("Could not read trace file %s: %s", path, e)
    return entries


def _parse_patient_meta(entry: dict) -> dict:
    """Extract CPMRN, unit, bed, brief — handles both CDS and order_gen schemas."""
    meta = {"cpmrn": "—", "unit": "—", "bed": "—", "brief": "—"}

    # order_gen entries have cpmrn as a direct field
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
    """Split the question into labelled sections for the Inputs tab."""
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
    """
    Estimate per-step cost using the proportional share of total cost_usd.
    Output tokens are 4× more expensive than input tokens (rough heuristic).
    Returns {step1, step2, step3, total} in USD.
    """
    total_usd: float | None = (entry.get("tokens") or {}).get("cost_usd")
    if total_usd is None:
        return {"step1": None, "step2": None, "step3": None, "total": None}

    def weighted(step_key: str) -> float:
        s = entry.get(step_key) or {}
        return s.get("input_tokens", 0) + 4 * s.get("output_tokens", 0)

    w1 = weighted("step1")
    w2 = weighted("step2")
    w3 = weighted("step3")
    total_w = w1 + w2 + w3 or 1

    return {
        "step1": round(total_usd * w1 / total_w, 6),
        "step2": round(total_usd * w2 / total_w, 6),
        "step3": round(total_usd * w3 / total_w, 6),
        "total": round(total_usd, 6),
    }


def _bucket_key(ts_iso: str) -> tuple[str, str]:
    """Return (date, hour) from an ISO timestamp string."""
    try:
        dt = datetime.fromisoformat(ts_iso)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H")
    except Exception:
        return "unknown", "00"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/runs")
def list_runs(mode: str | None = Query(None)):
    """
    Return a list of hourly run buckets, newest first.
    Each bucket: {date, hour, mode, count, cost_usd, label}
    """
    entries = _load_all_entries()

    buckets: dict[tuple, dict] = defaultdict(lambda: {
        "count": 0, "cost_usd": 0.0, "modes": set(),
    })

    for e in entries:
        entry_mode = e.get("mode", "cds")
        if mode and entry_mode != mode:
            continue
        date, hour = _bucket_key(e.get("timestamp", ""))
        key = (date, hour, entry_mode)
        b = buckets[key]
        b["count"] += 1
        b["cost_usd"] += (e.get("tokens") or {}).get("cost_usd", 0.0)
        b["modes"].add(entry_mode)

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
    Return all patient entries for a given date+hour bucket.
    Returns lite entries (no full question text) sorted by timestamp.
    """
    entries = _load_all_entries()
    results = []

    for e in entries:
        entry_mode = e.get("mode", "cds")
        if mode and entry_mode != mode:
            continue
        d, h = _bucket_key(e.get("timestamp", ""))
        if d != date or h != hour:
            continue

        meta = _parse_patient_meta(e)
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

    results.sort(key=lambda r: r["timestamp"])
    return {"patients": results}


@router.get("/entry/{run_id}")
def get_entry(run_id: str):
    """Return the full trace entry for a given run_id."""
    entries = _load_all_entries()
    for e in entries:
        if e.get("run_id") == run_id:
            meta = _parse_patient_meta(e)
            sections = _parse_question_sections(e.get("question", ""))
            costs = _step_cost(e)
            return {
                "run_id":    e.get("run_id"),
                "timestamp": e.get("timestamp"),
                "mode":      e.get("mode"),
                "model":     e.get("model"),
                "reasoning_model": e.get("reasoning_model"),
                "kb":        e.get("kb"),
                "meta":      meta,
                "sections":  sections,
                "step1":     e.get("step1", {}),
                "step2":     e.get("step2", {}),
                "step3":     e.get("step3", {}),
                "step4":     e.get("step4", {}),
                "final":     e.get("final", {}),
                "tokens":    e.get("tokens", {}),
                "costs":     costs,
            }
    raise HTTPException(status_code=404, detail=f"run_id {run_id!r} not found")
