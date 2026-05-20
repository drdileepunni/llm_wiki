"""
Sequential CDS replay over stored chart snapshots.

For each snapshot (in chronological order):
  1. Extract delta vs previous snapshot's timestamp
  2. Update rolling summary (LLM) — threads the summary forward
  3. Run CDS
  4. Store result in db.snapshot_results

Results can be retrieved later for evaluation / comparison.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

logger = logging.getLogger(__name__)


def _utc_iso(dt) -> str:
    """Serialize a datetime to ISO 8601 with explicit UTC offset so browsers parse it correctly."""
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        # MongoDB returns naive datetimes that are always UTC — attach tzinfo before serialising
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)

from tools.radar_sync.delta_extractor import extract_delta
from tools.radar_sync.cds_runner import run_cds
from tools.radar_sync.notes_module.admission_loader import load_admission
from tools.radar_sync.notes_module.mongo_cache import save_index
from backend.services.emr.db import get_db


def list_snapshots(cpmrn: str, encounter: int) -> list[dict]:
    """Return all snapshots for a patient, oldest first."""
    db = get_db()
    docs = list(db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"_id": 1, "snapshot_at": 1, "CPMRN": 1, "encounter": 1}
    ).sort("snapshot_at", 1))
    return [
        {
            "id":          str(d["_id"]),
            "snapshot_at": _utc_iso(d["snapshot_at"]),
        }
        for d in docs
    ]


def list_patients() -> list[dict]:
    """Return distinct CPMRN/encounter pairs that have snapshots."""
    db = get_db()
    pipeline = [
        {"$group": {
            "_id": {"CPMRN": "$CPMRN", "encounter": "$encounter"},
            "count": {"$sum": 1},
            "first_snapshot": {"$min": "$snapshot_at"},
            "last_snapshot":  {"$max": "$snapshot_at"},
        }},
        {"$sort": {"_id.CPMRN": 1}},
    ]
    results = list(db.snapshots.aggregate(pipeline))
    return [
        {
            "cpmrn":          r["_id"]["CPMRN"],
            "encounter":      r["_id"]["encounter"],
            "snapshot_count": r["count"],
            "first_snapshot": _utc_iso(r["first_snapshot"]),
            "last_snapshot":  _utc_iso(r["last_snapshot"]),
        }
        for r in results
    ]


def get_replay_results(cpmrn: str, encounter: int) -> list[dict]:
    """Return stored replay results for a patient, oldest first."""
    db = get_db()
    docs = list(db.snapshot_results.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart": 0}
    ).sort("snapshot_at", 1))
    out = []
    for d in docs:
        d["_id"] = str(d["_id"])
        d["snapshot_at"] = _utc_iso(d.get("snapshot_at"))
        d["replayed_at"] = _utc_iso(d.get("replayed_at"))
        out.append(d)
    return out


def _do_update_summary(existing_summary: "str | dict", delta: dict, cpmrn: str, chart: "dict | None" = None) -> dict:
    """
    Thin wrapper that always imports summary_updater fresh so the server never
    uses a stale cached version of that module.
    """
    import importlib, sys as _sys
    # Ensure repo root is on sys.path so tools.* is importable
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    import tools.radar_sync.summary_updater as _su
    importlib.reload(_su)
    return _su.update_summary(existing_summary, delta, cpmrn, chart=chart)


def replay(
    cpmrn: str,
    encounter: int,
    from_index: int = 0,
    force: bool = False,
    progress_cb=None,
) -> list[dict]:
    """
    Replay CDS sequentially across stored snapshots for this patient.

    from_index — start replay from this snapshot index (0-based). If > 0,
                 seeds running_summary from the stored result at from_index-1
                 and only re-runs snapshots from from_index onwards.
    force      — when True, ignore from_index, delete ALL existing results, and
                 re-run every snapshot completely from scratch (no seeding).

    progress_cb(i, total, snapshot_at) — called after each snapshot is processed.
    Returns list of result dicts for the replayed range.
    """
    db = get_db()

    # Load snapshots oldest-first
    snapshots = list(db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter}
    ).sort("snapshot_at", 1))

    if not snapshots:
        logger.warning("replay: no snapshots found for %s enc=%d", cpmrn, encounter)
        return []

    total = len(snapshots)

    if force:
        from_index = 0
        logger.info("replay: HARD REPLAY — deleting all results for %s enc=%d", cpmrn, encounter)
        db.snapshot_results.delete_many({"CPMRN": cpmrn, "encounter": encounter})
    else:
        from_index = max(0, min(from_index, total - 1))
        # Clear only results at or after from_index
        db.snapshot_results.delete_many(
            {"CPMRN": cpmrn, "encounter": encounter, "snapshot_idx": {"$gte": from_index}}
        )

    logger.info("replay: %d snapshots for %s enc=%d, starting from index %d (force=%s)",
                total, cpmrn, encounter, from_index, force)

    # Seed running_summary, pending_conditionals, and prior_snapshot_at from stored result at from_index-1
    running_summary: str | dict = ""
    pending_conditionals: list[dict] = []
    prior_snapshot_at = None

    if from_index > 0 and not force:
        seed = db.snapshot_results.find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "snapshot_idx": from_index - 1}
        )
        if seed:
            # Prefer the rich structured summary; fall back to plain text
            running_summary      = seed.get("structured_summary") or seed.get("running_summary", "")
            pending_conditionals = seed.get("suggestions", {}).get("conditional_orders", []) or []
            prior_snapshot_at    = seed.get("snapshot_at")
            logger.info("replay: seeded summary from snapshot_idx=%d", from_index - 1)
        else:
            logger.warning("replay: no stored result for snapshot_idx=%d, starting cold", from_index - 1)
            from_index = 0  # fall back to full replay

    results = []

    for i, snap in enumerate(snapshots[from_index:], start=from_index):
        snapshot_at = snap["snapshot_at"]
        chart = snap["chart"]

        snap_ts_str = _utc_iso(snapshot_at)
        logger.info("replay: snapshot %d/%d  at=%s", i + 1, total, snap_ts_str)

        # Step 1: rebuild FAISS note index for this snapshot
        try:
            store = load_admission(chart)
            admission_id = f"{cpmrn}_{encounter}"
            save_index(store, admission_id)
        except Exception:
            logger.exception("replay: note indexing failed at snapshot %d", i + 1)

        # Step 2: extract delta vs prior snapshot
        prior_dt = None
        if prior_snapshot_at is not None:
            if isinstance(prior_snapshot_at, str):
                try:
                    prior_dt = datetime.fromisoformat(prior_snapshot_at.replace("Z", "+00:00"))
                except Exception:
                    prior_dt = None
            elif isinstance(prior_snapshot_at, datetime):
                prior_dt = prior_snapshot_at
                if prior_dt.tzinfo is None:
                    prior_dt = prior_dt.replace(tzinfo=timezone.utc)
        delta = extract_delta(chart, prior_dt)

        # Step 3: update rolling summary (structured dict)
        try:
            running_summary = _do_update_summary(running_summary, delta, cpmrn,
                                                  chart=chart if i == from_index and not running_summary else None)
            if not isinstance(running_summary, dict) or not running_summary.get("problems"):
                logger.warning("replay: structured summary empty at snapshot %d — got: %r", i + 1, running_summary)
        except Exception as exc:
            logger.exception("replay: summary update failed at snapshot %d: %s", i + 1, exc)

        # Step 3.5: order generation (shadow trial)
        agent_suggestions = []
        try:
            from tools.radar_sync.replay_order_gen import run_replay_order_gen
            agent_suggestions = run_replay_order_gen(
                db, cpmrn, encounter, i, snapshot_at, running_summary, chart
            )
        except Exception:
            logger.exception("replay: order gen failed at snapshot %d", i + 1)

        # Step 4: run CDS
        cds_result = {}
        try:
            cds_result = run_cds(
                cpmrn, encounter, running_summary, chart,
                delta.get("io_last_24h", {}),
                pending_conditionals=pending_conditionals or None,
            )
        except Exception:
            logger.exception("replay: CDS failed at snapshot %d", i + 1)

        # Step 5: store result
        # running_summary may be a structured dict (new) or plain string (legacy seed)
        structured = running_summary if isinstance(running_summary, dict) else {}
        narrative  = structured.get("narrative", running_summary if isinstance(running_summary, str) else "")

        result_doc = {
            "CPMRN":       cpmrn,
            "encounter":   encounter,
            "snapshot_at": snapshot_at,
            "snapshot_idx": i,
            "replayed_at": datetime.now(timezone.utc),
            "delta_summary": {
                "new_vitals_count": len(delta.get("new_vitals", [])),
                "new_labs_count":   len(delta.get("new_labs", [])),
                "new_notes_count":  len(delta.get("new_notes", [])),
            },
            "delta_content": {
                "new_labs": [
                    {
                        "name": lab.get("name", ""),
                        "reported_at": str(lab.get("reportedAt", "")),
                        "values": ", ".join(
                            f"{k}={v.get('value')} {v.get('unit','') or ''}".strip()
                            for k, v in [
                                (k, v) for k, v in (lab.get("attributes") or {}).items()
                                if isinstance(v, dict) and v.get("value") not in (None, "")
                            ][:8]
                        ),
                    }
                    for lab in (delta.get("new_labs") or [])
                ],
                "new_notes": [
                    {
                        "note_type": n.get("note_type", ""),
                        "author":    n.get("author", ""),
                        "text":      (n.get("text") or "")[:300],
                    }
                    for n in (delta.get("new_notes") or [])
                ],
            },
            "io_last_24h":       delta.get("io_last_24h", {}),
            "structured_summary": structured,
            "running_summary":    narrative,   # plain text for backward compat
            "agent_suggestions":  agent_suggestions,
            "suggestions": {
                "immediate_actions":          cds_result.get("immediate_actions", []),
                "immediate_next_steps":       cds_result.get("immediate_next_steps", []),
                "clinical_reasoning":         cds_result.get("clinical_reasoning", []),
                "monitoring_followup":        cds_result.get("monitoring_followup", []),
                "alternative_considerations": cds_result.get("alternative_considerations", []),
                "conditional_orders":         cds_result.get("conditional_orders", []),
                "cost_usd":                   cds_result.get("cost_usd"),
            },
        }
        db.snapshot_results.insert_one(result_doc)

        out = dict(result_doc)
        out["_id"] = str(result_doc.get("_id", ""))
        out["snapshot_at"] = snap_ts_str
        out["replayed_at"] = _utc_iso(result_doc["replayed_at"])
        results.append(out)

        # Accumulate conditional orders for the next snapshot (deduplicated by condition+action)
        for c in cds_result.get("conditional_orders", []):
            if not any(
                p.get("condition") == c.get("condition") and p.get("action") == c.get("action")
                for p in pending_conditionals
            ):
                pending_conditionals.append(c)

        prior_snapshot_at = snapshot_at
        if progress_cb:
            progress_cb(i + 1, total, snap_ts_str)

    logger.info("replay: complete — %d results stored", len(results))
    return results
