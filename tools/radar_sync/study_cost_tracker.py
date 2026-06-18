"""
study_cost_tracker.py — LLM cost calculation per pipeline run.

Called at the end of each hourly scheduler run. Reads pipeline_traces
documents that were created during the run, aggregates token counts by
step, applies per-model pricing (including the cached-token discount when
Gemini context caching is active), and writes one doc to pipeline_run_costs.

Pricing used (USD per 1M tokens):

  gemini-2.5-flash  (problem_tracker, status_classifier, screener_flag_eval)
    Input (non-cached):   $0.30
    Input (cached):       $0.075   ← 75 % cheaper via context cache
    Output (+ thinking):  $2.50

  gemini-3.1-flash-lite  (all other steps)
    Input (non-cached):   $0.25
    Input (cached):       $0.0625  ← 75 % cheaper via context cache
    Output (+ thinking):  $1.50

Collection: pipeline_run_costs
{
  run_started_at,          # datetime — marks which hourly run this belongs to
  computed_at,             # datetime — when this doc was written
  patient_count,           # how many patients had traces in this run
  by_step: {
    problem_tracker:   {input_tokens, cached_tokens, output_tokens, thinking_tokens, cost_usd},
    status_classifier: {input_tokens, cached_tokens, output_tokens, thinking_tokens, cost_usd},
    ...
  },
  totals: {input_tokens, cached_tokens, output_tokens, thinking_tokens, cost_usd},
  pricing: {<step>: {model, input_per_1m, cached_per_1m, output_per_1m}, ...},
}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Per-step pricing table ────────────────────────────────────────────────────
# (model_name, input_$/1M, cached_input_$/1M, output_$/1M)
# "cached_input" is the rate applied to tokens served from a Gemini context
# cache (cached_content_token_count in usage_metadata).  Non-cached input
# tokens within the same request are billed at the regular input rate.
# thinking tokens are billed at the same rate as output tokens.

_FLASH_LITE = ("gemini-3.1-flash-lite", 0.25,  0.0625, 1.50)
_FLASH_25   = ("gemini-2.5-flash",      0.30,  0.075,  2.50)

# Steps that use gemini-2.5-flash (the reasoning / tracker model)
_FLASH_25_STEPS = {"problem_tracker", "status_classifier", "screener_flag_eval"}

def _pricing(step: str) -> tuple[str, float, float, float]:
    return _FLASH_25 if step in _FLASH_25_STEPS else _FLASH_LITE


def _cost(step: str, input_tok: int, cached_tok: int, output_tok: int, thinking_tok: int) -> float:
    """Return total cost in USD, applying the cached-token discount."""
    _, input_rate, cached_rate, output_rate = _pricing(step)
    non_cached_tok = max(0, input_tok - cached_tok)
    return (
        non_cached_tok               / 1_000_000 * input_rate  +
        cached_tok                   / 1_000_000 * cached_rate +
        (output_tok + thinking_tok)  / 1_000_000 * output_rate
    )


def compute_run_cost(db: Any, run_started_at: datetime) -> dict:
    """
    Aggregate token usage for all pipeline_traces created at or after
    run_started_at, compute USD cost, persist to pipeline_run_costs.

    Returns the summary dict (same shape as stored doc, minus _id).
    """
    if isinstance(run_started_at, datetime) and run_started_at.tzinfo is None:
        run_started_at = run_started_at.replace(tzinfo=timezone.utc)

    traces = list(
        db["pipeline_traces"].find(
            {"started_at": {"$gte": run_started_at}},
            {"step": 1, "total_tokens": 1, "CPMRN": 1, "_id": 0},
        )
    )

    by_step: dict[str, dict] = {}
    patients: set[str] = set()

    for t in traces:
        step = t.get("step", "unknown")
        tok  = t.get("total_tokens") or {}
        inp    = tok.get("in",      0) or 0
        out    = tok.get("out",     0) or 0
        thk    = tok.get("thinking", 0) or 0
        cached = tok.get("cached",  0) or 0

        if step not in by_step:
            by_step[step] = {
                "input_tokens": 0, "cached_tokens": 0,
                "output_tokens": 0, "thinking_tokens": 0,
            }

        by_step[step]["input_tokens"]   += inp
        by_step[step]["cached_tokens"]  += cached
        by_step[step]["output_tokens"]  += out
        by_step[step]["thinking_tokens"] += thk

        cpmrn = t.get("CPMRN")
        if cpmrn:
            patients.add(cpmrn)

    # Compute cost per step and totals
    total_in, total_cached, total_out, total_thk = 0, 0, 0, 0
    pricing_snapshot: dict[str, dict] = {}

    for step, agg in by_step.items():
        agg["cost_usd"] = round(
            _cost(step, agg["input_tokens"], agg["cached_tokens"],
                  agg["output_tokens"], agg["thinking_tokens"]),
            6,
        )
        model, in_r, ca_r, out_r = _pricing(step)
        pricing_snapshot[step] = {
            "model": model, "input_per_1m": in_r,
            "cached_per_1m": ca_r, "output_per_1m": out_r,
        }
        total_in     += agg["input_tokens"]
        total_cached += agg["cached_tokens"]
        total_out    += agg["output_tokens"]
        total_thk    += agg["thinking_tokens"]

    total_cost = round(
        sum(agg["cost_usd"] for agg in by_step.values()), 6
    )

    now = datetime.now(timezone.utc)

    doc = {
        "run_started_at": run_started_at,
        "computed_at":    now,
        "patient_count":  len(patients),
        "trace_count":    len(traces),
        "by_step":        by_step,
        "totals": {
            "input_tokens":    total_in,
            "cached_tokens":   total_cached,
            "output_tokens":   total_out,
            "thinking_tokens": total_thk,
            "cost_usd":        total_cost,
        },
        "pricing": pricing_snapshot,
    }

    try:
        import sys
        from pathlib import Path
        _root = Path(__file__).resolve().parents[2]
        for _p in [str(_root / "app"), str(_root)]:
            if _p not in sys.path:
                sys.path.insert(0, _p)
        from backend.services.bq_store import get_bq_store
        get_bq_store().insert_run_cost(doc)
        logger.info(
            "cost_tracker: run %s — %d traces, %d patients, total $%.4f USD "
            "(in=%d cached=%d out=%d thinking=%d)",
            run_started_at.isoformat(), len(traces), len(patients), total_cost,
            total_in, total_cached, total_out, total_thk,
        )
    except Exception:
        logger.exception("cost_tracker: failed to persist run cost doc")

    return doc
