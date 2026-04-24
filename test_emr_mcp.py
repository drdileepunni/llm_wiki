#!/usr/bin/env python3
"""
Smoke test: pull patient data via the emr module.
Run from the llm_wiki root:
    python test_emr_mcp.py
"""

import json
import sys
import os
from pathlib import Path

# Load the app's .env (MONGO_URI, MONGO_DB_NAME, etc.)
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "app" / ".env")

# Add app to path so relative imports inside the package resolve
sys.path.insert(0, str(Path(__file__).parent / "app"))

import traceback
from backend.services.emr import (
    get_patient_demographics,
    get_latest_vitals,
    get_latest_labs,
    get_active_orders,
    search_orderables,
)

CPMRN = "INTSNLG2851387"

TESTS = [
    ("get_patient_demographics", lambda: get_patient_demographics(CPMRN)),
    ("get_latest_vitals",        lambda: get_latest_vitals(CPMRN)),
    ("get_latest_labs",          lambda: get_latest_labs(CPMRN)),
    ("get_active_orders",        lambda: get_active_orders(CPMRN)),
    ("search_orderables",        lambda: search_orderables("paracetamol")),
]

passed = failed = 0

for name, fn in TESTS:
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    try:
        result = fn()
        if isinstance(result, dict) and "error" in result:
            print(f"[WARN] {result['error']}")
        else:
            print(json.dumps(result, indent=2, default=str))
            print("[PASS]")
        passed += 1
    except Exception as e:
        print(f"[FAIL] {e}")
        traceback.print_exc()
        failed += 1

print(f"\n{'='*60}")
print(f"Result: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
