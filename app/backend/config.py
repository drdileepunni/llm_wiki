from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY         = os.getenv("GOOGLE_API_KEY", "")
BQ_SERVICE_URL         = os.getenv("BQ_SERVICE_URL", "https://bigquery-service-971880579407.us-central1.run.app")
BROWSERBASE_API_KEY    = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
OLLAMA_API_KEY         = os.getenv("OLLAMA_API_KEY", "")
MEDGEMMA_URL           = os.getenv("MEDGEMMA_URL", "http://localhost:11434")
MEDGEMMA_CPU_URL       = os.getenv("MEDGEMMA_CPU_URL", "")
GCS_BUCKET             = os.getenv("GCS_BUCKET", "cds-pipeline-ops")
WIKI_ROOT = Path(os.getenv("WIKI_ROOT", "/app"))
MODEL = os.getenv("MODEL", "gemini-2.0-flash-lite")
REASONING_MODEL = os.getenv("REASONING_MODEL", "gemini-2.0-flash-lite")

TRACES_DIR = WIKI_ROOT / "app" / "traces"
