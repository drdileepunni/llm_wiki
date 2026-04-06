from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WIKI_ROOT = Path(os.environ["WIKI_ROOT"])
MODEL = os.getenv("MODEL", "claude-3-haiku-20240307")

RAW_DIR = WIKI_ROOT / "raw"
WIKI_DIR = WIKI_ROOT / "wiki"
CLAUDE_MD = WIKI_ROOT / "CLAUDE.md"
DB_PATH = WIKI_ROOT / "app" / "wiki.db"

# Anthropic pricing (per million tokens)
PRICING = {
    "claude-sonnet-4-5":       {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-6":       {"input": 3.00,  "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-3-haiku-20240307": {"input": 0.25,  "output": 1.25},
}
