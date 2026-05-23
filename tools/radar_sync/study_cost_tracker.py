"""
study_cost_tracker.py — LLM cost calculation per pipeline run.

Called at the end of each hourly scheduler run. Reads pipeline_traces
documents that were created during the run, aggregates token counts by
step/model, applies Gemini 2.5 Flash pricing, and writes one doc to
pipeline_run_costs.

Gemini 2.5 Flash pricing (as of May 2025, USD per 1M tokens):
  Input (prompts ≤200K ctx):   $0.075
  Output — non-thinking:       $0.30
  Output — thinking:           $3.50   ← thinking_tokens split tracked separately

Collection: pipeline_run_costs
{
  run_started_at,          # datetime — marks which hourly run this belongs to
  computed_at,             # datetime — when this doc was written
  patient_count,           # how many patients had traces in this run
  by_step: {
    problem_tracker:   {input_tokens, output_tokens, thinking_tokens, cost_usd},
    status_classifier: {input_tokens, output_tokens, thinking_tokens, cost_usd},
    study_matcher:     {input_tokens, output_tokens, thinking_tokens, cost_usd},
    ...
  },
  totals: {input_tokens, output_tokens, thinking_tokens, cost_usd},
  model: "gemini-2.5-flash",
  pricing: {input_per_1m, output_per_1m, thinking_per_1m},
}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Gemini 2.5 Flash pricing (USD per 1M tokens) ─────────────────────────────
_MODEL               = "gemini-2.5-flash"
_PRICE_INPUT_PER_1M  = 0.075   # prompt tokens
_PRICE_OUTPUT_PER_1M = 0.30    # non-thinking output tokens
_PRICE_THINK_PER_1M  = 3.50    # thinking tokens


def _cost(input_tok: int, output_tok: int, thinking_tok: int) -> float:
    """Return total cost in USD for a token count triple."""
    non_think_out = max(0, output_tok - thinking_tok)
    return (
        input_tok    / 1_000_000 * _PRICE_INPUT_PER_1M  +
        non_think_out / 1_000_000 * _PRICE_OUTPUT_PER_1M +
        thinking_tok  / 1_000_000 * _PRICE_THINK_PER_1M
    )


def compute_run_cost(db: Any, run_started_at: datetime) -> dict:
    """
    Aggregate token usage for all pipeline_traces created at or after
    run_started_at, compute USD cost, persist to pipeline_run_costs.

    Returns the summary dict (same shape as stored doc, minus _id).
    """
    # Normalise timezone
    if isinstance(run_started_at, datetime) and run_started_at.tzinfo is None:
        run_started_at = run_started_at.replace(tzinfo=timezone.utc)

    traces = list(
        db["pipeline_traces"].find(
            {"started_at": {"$gte": run_started_at}},
            {"step": 1, "total_tokens": 1, "CPMRN": 1, "_id": 0},
        )
    )

    # Aggregate by step
    by_step: dict[str, dict] = {}
    patients: set[str] = set()

    for t in traces:
        step = t.get("step", "unknown")
        tok  = t.get("total_tokens") or {}
        inp  = tok.get("in", 0)  or 0
        out  = tok.get("out", 0) or 0
        thk  = tok.get("thinking", 0) or 0

        if step not in by_step:
            by_step[step] = {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}

        by_step[step]["input_tokens"]   += inp
        by_step[step]["output_tokens"]  += out
        by_step[step]["thinking_tokens"] += thk

        cpmrn = t.get("CPMRN")
        if cpmrn:
            patients.add(cpmrn)

    # Compute cost per step and total
    total_in, total_out, total_thk = 0, 0, 0
    for step, agg in by_step.items():
        agg["cost_usd"] = round(_cost(agg["input_tokens"], agg["output_tokens"], agg["thinking_tokens"]), 6)
        total_in  += agg["input_tokens"]
        total_out += agg["output_tokens"]
        total_thk += agg["thinking_tokens"]

    total_cost = round(_cost(total_in, total_out, total_thk), 6)

    now = datetime.now(timezone.utc)

    doc = {
        "run_started_at": run_started_at,
        "computed_at":    now,
        "patient_count":  len(patients),
        "trace_count":    len(traces),
        "by_step":        by_step,
        "totals": {
            "input_tokens":    total_in,
            "output_tokens":   total_out,
            "thinking_tokens": total_thk,
            "cost_usd":        total_cost,
        },
        "model":   _MODEL,
        "pricing": {
            "input_per_1m":    _PRICE_INPUT_PER_1M,
            "output_per_1m":   _PRICE_OUTPUT_PER_1M,
            "thinking_per_1m": _PRICE_THINK_PER_1M,
        },
    }

    try:
        db["pipeline_run_costs"].insert_one(doc.copy())
        logger.info(
            "cost_tracker: run %s — %d traces, %d patients, total $%.4f USD "
            "(in=%d, out=%d, thinking=%d)",
            run_started_at.isoformat(), len(traces), len(patients), total_cost,
            total_in, total_out, total_thk,
        )
    except Exception:
        logger.exception("cost_tracker: failed to persist run cost doc")

    return doc
