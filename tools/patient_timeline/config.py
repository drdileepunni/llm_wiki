"""
Configuration for the patient_timeline tool.

All values fall back to environment variables. GCP defaults match the
production data-pipelines VM used by the ARDS project.

Add these to llm_wiki/app/.env as needed:
  GOOGLE_API_KEY=...          (already present – used for Gemini)
  GCP_PROJECT=...             (optional override)
  GCP_INSTANCE=...            (optional override)
  GCP_ZONE=...                (optional override)
  GCP_REMOTE_USER=...         (optional override)
"""

from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Walk up to the llm_wiki repo root (.env lives next to app/)
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    load_dotenv(_REPO_ROOT / "app" / ".env")
except ImportError:
    pass

# ── Gemini ─────────────────────────────────────────────────────────────────────
# llm_wiki uses GOOGLE_API_KEY; data-pipeline-v2 uses GEMINI_API_KEY.
# Accept either; prefer GOOGLE_API_KEY since that's what's in llm_wiki's .env.
GEMINI_API_KEY: str = (
    os.environ.get("GOOGLE_API_KEY")
    or os.environ.get("GEMINI_API_KEY")
    or ""
)

# ── GCP / VM ───────────────────────────────────────────────────────────────────
GCP_PROJECT:   str = os.environ.get("GCP_PROJECT",   "prod-tech-project1-bv479-zo027")
GCP_INSTANCE:  str = os.environ.get("GCP_INSTANCE",  "data-pipelines")
GCP_ZONE:      str = os.environ.get("GCP_ZONE",      "asia-south1-b")
GCP_REMOTE_USER: str = os.environ.get(
    "GCP_REMOTE_USER", "dileep_unni_cloudphysician_net"
)

DOCKER_IMAGE: str = os.environ.get(
    "DOCKER_IMAGE",
    f"asia-south1-docker.pkg.dev/{GCP_PROJECT}/data-pipeline/datapipeline:latest",
)

REMOTE_HOME:     str = f"/home/{GCP_REMOTE_USER}"
REMOTE_ENV_FILE: str = f"{REMOTE_HOME}/.env"

# ── MongoDB (local) ────────────────────────────────────────────────────────────
MONGO_URI: str = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME:   str = os.environ.get("DB_NAME",   "emr-local")

# ── Local paths ────────────────────────────────────────────────────────────────
REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT_DIR: Path = REPO_ROOT / "timelines"
DEFAULT_CACHE_DIR:  Path = REPO_ROOT / ".timeline_cache"  # kept for API compat
