"""
Local CDS Pipeline Dashboard — entry point.

Usage:
    python run_dashboard.py

Requires:
  - .env.local in the repo root (GCS_BUCKET, GOOGLE_API_KEY, etc.)
  - gcloud ADC with access to:
      * gs://patientview-cds-pipeline-ops  (GCS config)
      * patientview-9uxml.cds_study        (BigQuery metrics)
    Run: gcloud auth application-default login
"""
import sys
from pathlib import Path

# Ensure both `app/` and repo root are on PYTHONPATH
_ROOT = Path(__file__).resolve().parent
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env.local")

import os
os.environ.setdefault("GCS_BUCKET", "patientview-cds-pipeline-ops")

from dashboard.server import create_app

app = create_app()

if __name__ == "__main__":
    print("CDS Dashboard → http://127.0.0.1:8050")
    app.run(host="127.0.0.1", port=8050, debug=True, use_reloader=False)
