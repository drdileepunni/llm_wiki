from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY         = os.getenv("GOOGLE_API_KEY", "")
# PRODTECH_BQ_SA_KEY: JSON string of the prod-tech SA key, loaded from Secret Manager on Cloud Run.
# For local dev: export PRODTECH_BQ_SA_KEY="$(cat /path/to/prodtech_sa_key.json)"
PRODTECH_BQ_SA_KEY     = os.getenv("PRODTECH_BQ_SA_KEY", "")
BROWSERBASE_API_KEY    = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
OLLAMA_API_KEY         = os.getenv("OLLAMA_API_KEY", "")
MEDGEMMA_URL           = os.getenv("MEDGEMMA_URL", "http://localhost:11434")
MEDGEMMA_CPU_URL       = os.getenv("MEDGEMMA_CPU_URL", "")
GCS_BUCKET             = os.getenv("GCS_BUCKET", "patientview-cds-pipeline-ops")
WIKI_ROOT = Path(os.getenv("WIKI_ROOT", "/app"))
MODEL = os.getenv("MODEL", "gemini-2.0-flash-lite")
REASONING_MODEL = os.getenv("REASONING_MODEL", "gemini-2.0-flash-lite")

TRACES_DIR = WIKI_ROOT / "app" / "traces"
