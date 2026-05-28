"""
Local test runner for the CDS pipeline.
Loads .env.local, wires up sys.path, then calls _collect_all() directly.

Usage:
    .venv/bin/python run_local.py
"""
import sys
import logging
from pathlib import Path

# Wire up paths exactly as main.py does in Cloud Run
_ROOT = Path(__file__).resolve().parent
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load .env.local (secrets + env vars for local dev)
from dotenv import load_dotenv
load_dotenv(_ROOT / ".env.local", override=True)

# PRODTECH_BQ_SA_KEY can't be stored in .env.local (multi-line JSON breaks dotenv).
# Load it directly from the key file if not already set.
import os
if not os.environ.get("PRODTECH_BQ_SA_KEY"):
    _key_path = _ROOT / "prodtech_sa_key.json"
    if _key_path.exists():
        os.environ["PRODTECH_BQ_SA_KEY"] = _key_path.read_text()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-patients", type=int, default=None,
                        help="Limit pipeline to first N patients (for local testing)")
    args = parser.parse_args()

    from app.backend.scheduler import _collect_all
    print(f"=== Starting local pipeline run"
          + (f" (max {args.max_patients} patients)" if args.max_patients else "")
          + " ===")
    _collect_all(max_patients=args.max_patients)
    print("=== Done ===")
