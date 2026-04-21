from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass
import os

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY         = os.getenv("GOOGLE_API_KEY", "")
BROWSERBASE_API_KEY    = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
WIKI_ROOT = Path(os.environ["WIKI_ROOT"])
MODEL = os.getenv("MODEL", "claude-haiku-4-5")

RAW_DIR   = WIKI_ROOT / "raw"
WIKI_DIR  = WIKI_ROOT / "wiki"
CLAUDE_MD = WIKI_ROOT / "CLAUDE.md"
DB_PATH   = WIKI_ROOT / "app" / "wiki.db"
CACHE_DIR = WIKI_ROOT / "app" / ".cache"

KB_DIR = WIKI_ROOT / "kbs"  # Every KB (including "default") lives here


@dataclass
class KBConfig:
    name: str
    wiki_root: Path   # prefix for all relative paths (e.g. "wiki/sources/foo.md")
    wiki_dir: Path
    raw_dir: Path
    claude_md: Path
    cache_dir: Path


def _default_kb() -> "KBConfig":
    """Convenience alias used by service defaults before a request context exists."""
    return get_kb("agent_school")


def get_kb(name: str) -> "KBConfig":
    if not name:
        name = "default"
    kb_root = KB_DIR / name
    if not kb_root.exists():
        raise ValueError(f"Knowledge base '{name}' does not exist")
    return KBConfig(
        name=name,
        wiki_root=kb_root,
        wiki_dir=kb_root / "wiki",
        raw_dir=kb_root / "raw",
        claude_md=kb_root / "CLAUDE.md",
        cache_dir=kb_root / ".cache",
    )


def list_kbs() -> list[str]:
    if not KB_DIR.exists():
        return []
    return sorted(d.name for d in KB_DIR.iterdir() if d.is_dir())

# ── Flat pricing (per million tokens) ─────────────────────────────────────────
# Used for models with no context-length tier.
PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-5":       {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-6":       {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":        {"input": 0.80,  "output":  4.00},
    # Google Gemini — flat rate models
    "gemini-2.0-flash":        {"input": 0.10,  "output":  0.40},
    "gemini-2.0-flash-lite":   {"input": 0.075, "output":  0.30},
    "gemini-1.5-flash":        {"input": 0.075, "output":  0.30},
}

# ── Tiered pricing for Gemini models with context-length breakpoints ───────────
# Each entry: list of (threshold_tokens, input_$/M, output_$/M)
# Tiers are checked in order; the first threshold >= input_tokens wins.
# Source: https://ai.google.dev/pricing (as of 2026-04)
GEMINI_TIERED_PRICING: dict[str, list[tuple[int, float, float]]] = {
    # gemini-1.5-pro: ≤128k → $1.25/$5.00 | >128k → $2.50/$10.00
    "gemini-1.5-pro": [
        (128_000,  1.25,  5.00),
        (float("inf"), 2.50, 10.00),
    ],
    # gemini-2.5-pro: ≤200k → $1.25/$10.00 | >200k → $2.50/$15.00
    "gemini-2.5-pro-preview-03-25": [
        (200_000,  1.25, 10.00),
        (float("inf"), 2.50, 15.00),
    ],
    "gemini-2.5-pro": [
        (200_000,  1.25, 10.00),
        (float("inf"), 2.50, 15.00),
    ],
}


def get_pricing(model: str, input_tokens: int = 0) -> dict[str, float]:
    """
    Return {input, output} pricing ($/M tokens) for a model.

    For tiered Gemini models the correct tier is selected based on
    `input_tokens`.  For everything else a flat rate is returned.
    Falls back gracefully for unknown models.
    """
    # Tiered Gemini models
    if model in GEMINI_TIERED_PRICING:
        for threshold, inp, out in GEMINI_TIERED_PRICING[model]:
            if input_tokens <= threshold:
                return {"input": inp, "output": out}

    # Flat-rate known models
    if model in PRICING:
        return PRICING[model]

    # Best-effort family fallback
    if model.startswith("gemini"):
        return {"input": 0.10, "output": 0.40}
    return {"input": 3.00, "output": 15.00}  # Anthropic sonnet default
