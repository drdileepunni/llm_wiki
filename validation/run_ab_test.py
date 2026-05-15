#!/usr/bin/env python3
"""
A/B Test CLI — thin wrapper around app/backend/services/ab_pipeline.py

Usage:
  python validation/run_ab_test.py --n 3 --kb agent_school
  python validation/run_ab_test.py --n 3 --diagnosis sepsis --mode weighted
  python validation/run_ab_test.py --n 3 --diagnosis sepsis,pneumonia --max-turns 4
  python validation/run_ab_test.py --skip-arm-b   # Arm A only (wiki, no MedGemma)
  python validation/run_ab_test.py --list-diagnoses
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT      = Path(__file__).parent.parent
VALIDATION_DIR = Path(__file__).parent
APP_DIR        = REPO_ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from dotenv import load_dotenv
load_dotenv(APP_DIR / ".env")

# ── App imports (after env) ───────────────────────────────────────────────────
from backend.config import get_kb
from backend.services.ab_pipeline import run_ab_batch
from backend.services.scenario_catalog import DIAGNOSIS_GROUPS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ab_test")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run A/B CDS pipeline test")
    ap.add_argument("--n",           type=int, default=3,
                    help="Number of scenarios (default: 3)")
    ap.add_argument("--seed",        type=int, default=42,
                    help="Random seed (default: 42)")
    ap.add_argument("--kb",          default="agent_school",
                    help="Knowledge base name (default: agent_school)")
    ap.add_argument("--diagnosis",   default=None,
                    help="Comma-separated diagnosis IDs, e.g. sepsis,pneumonia. "
                         "Omit for all. Use --list-diagnoses to see options.")
    ap.add_argument("--mode",        default="weighted",
                    choices=["weighted", "random"],
                    help="Scenario sampling mode (default: weighted)")
    ap.add_argument("--max-turns",   type=int, default=4,
                    help="Max turns per scenario (default: 4)")
    ap.add_argument("--skip-arm-b",  action="store_true",
                    help="Skip Arm B (MedGemma) — run Arm A only")
    ap.add_argument("--skip-arm-a",  action="store_true",
                    help="Skip Arm A (wiki) — run Arm B only")
    ap.add_argument("--skip-order-gen", action="store_true",
                    help="Skip order structuring pass (faster runs)")
    ap.add_argument("--list-diagnoses", action="store_true",
                    help="Print available diagnosis IDs and exit")
    args = ap.parse_args()

    if args.list_diagnoses:
        print("Available diagnosis IDs:")
        for d in DIAGNOSIS_GROUPS:
            print(f"  {d['id']:25s}  {d['label']}")
        return

    kb = get_kb(args.kb)
    log.info("KB: %s  (%s)", kb.name, kb.wiki_dir)

    diagnosis_ids = None
    if args.diagnosis:
        diagnosis_ids = [d.strip() for d in args.diagnosis.split(",") if d.strip()]

    # Live progress: print each completed case
    def on_progress(case: dict) -> None:
        sid   = case.get("scenario_id", "?")
        turn  = case.get("turn_num", "?")
        a_ok  = (case.get("arm_a") or {}).get("ok", False)
        b_ok  = (case.get("arm_b") or {}).get("ok", False)
        a_ord = len((case.get("arm_a") or {}).get("structured_orders") or [])
        b_ord = len((case.get("arm_b") or {}).get("structured_orders") or [])
        log.info("  ✓ %s turn %s  ArmA=%s(%d orders)  ArmB=%s(%d orders)",
                 sid, turn,
                 "✓" if a_ok else "✗", a_ord,
                 "✓" if b_ok else "✗", b_ord)

    try:
        result = run_ab_batch(
            n_scenarios  = args.n,
            kb           = kb,
            mode         = args.mode,
            seed         = args.seed,
            max_turns    = args.max_turns,
            diagnosis_ids= diagnosis_ids,
            skip_arm_a   = args.skip_arm_a,
            skip_arm_b   = args.skip_arm_b,
            skip_order_gen = args.skip_order_gen,
            on_progress  = on_progress,
        )
    except RuntimeError as e:
        log.error("%s", e)
        sys.exit(1)

    # ── Write output ──────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = VALIDATION_DIR / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"ab_results_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    n_cases = result.get("n_cases", 0)
    log.info("✓ Wrote %d case(s) → %s", n_cases, out_path)
    print(f"\nResults → {out_path}")
    print(f"Next:    python validation/serve_ab.py --results {out_path.name}")


if __name__ == "__main__":
    main()
