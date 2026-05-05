"""
Write order-generation provenance traces to date-partitioned JSONL files.

Each call to write_trace() appends one JSON line to:
  <TRACES_DIR>/order_gen_YYYY-MM-DD.jsonl

Trace structure (keys written by order_gen_pipeline):
  run_id           — uuid4 assigned at pipeline entry
  timestamp        — ISO-8601 UTC
  cpmrn            — patient identifier (or null)
  patient_type     — adult | pediatric | neonatal
  model            — LLM model name used
  kb               — knowledge-base name
  recommendations  — raw input list
  phase0           — {intents: [[{index, catalog_query, order_type, parameters}]]}
  phase1           — {iterations: [{iteration, tool_calls, stop_reason, input_tokens, output_tokens}]}
                     tool_calls: [{name, args, result_summary, result_count}]
  weight_resolution — {source, weight_kg, note}  (null if no demographics fetched)
  phase2           — {input_tokens, output_tokens}
  final_orders     — list of order objects from submit_orders
  tokens           — {input, output, cost_usd}
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("wiki.provenance")


def write_trace(trace: dict, traces_dir: Path, prefix: str = "order_gen") -> Path:
    """Append one trace record to the daily JSONL file. Never raises."""
    traces_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = traces_dir / f"{prefix}_{date_str}.jsonl"
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace, default=str) + "\n")
        log.info("Provenance trace written  run_id=%s  file=%s", trace.get("run_id"), path.name)
    except Exception as exc:
        log.warning("Failed to write provenance trace: %s", exc)
    return path
