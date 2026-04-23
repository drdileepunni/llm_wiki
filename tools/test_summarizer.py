#!/usr/bin/env python3
"""
Debug script for the clinical timeline summarizer.
Runs _summarize_timeline() on a snapshot and logs token counts, stop reason, and full output.

Usage:
    python tools/test_summarizer.py [snapshot_num]   # default: snapshot_1
    python tools/test_summarizer.py 3
"""

import sys
import logging
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("test_summarizer")

from app.backend.services.clinical_assess_pipeline import (
    TIMELINES_BASE,
    _SUMMARIZER_PROMPT,
    _SUMMARIZER_SYSTEM,
)
from app.backend.services.llm_client import get_llm_client
from app.backend.config import MODEL

PATIENT = "INTSNLG2851387_1"
SNAP_NUM = int(sys.argv[1]) if len(sys.argv) > 1 else 1

snap_dir = TIMELINES_BASE / PATIENT / f"snapshot_{SNAP_NUM}"
csv_path = snap_dir / "snapshot.csv"

if not csv_path.exists():
    print(f"ERROR: {csv_path} not found")
    sys.exit(1)

csv_text = csv_path.read_text(encoding="utf-8")
log.info("CSV: %d lines, %d chars", csv_text.count("\n"), len(csv_text))

prompt = _SUMMARIZER_PROMPT.format(csv_text=csv_text)
log.info("Prompt: %d chars", len(prompt))

llm = get_llm_client()
log.info("Model: %s", MODEL)

response = llm.create_message(
    messages=[{"role": "user", "content": prompt}],
    tools=[],
    system=_SUMMARIZER_SYSTEM,
    max_tokens=4000,
    force_tool=False,
    thinking_budget=1024,
)

log.info("Stop reason : %s", response.stop_reason)
log.info("Input tokens : %d", response.usage.input_tokens)
log.info("Output tokens: %d", response.usage.output_tokens)
log.info("Content blocks: %d", len(response.content))

summary = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
log.info("Summary length: %d chars", len(summary))

print("\n" + "=" * 80)
print("FULL SUMMARY OUTPUT")
print("=" * 80)
print(summary)
print("=" * 80)
