from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent.parent / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY", "")
WIKI_ROOT = Path(os.environ["WIKI_ROOT"])
MODEL = os.getenv("MODEL", "claude-haiku-4-5")

RAW_DIR   = WIKI_ROOT / "raw"
WIKI_DIR  = WIKI_ROOT / "wiki"
CLAUDE_MD = WIKI_ROOT / "CLAUDE.md"
DB_PATH   = WIKI_ROOT / "app" / "wiki.db"
CACHE_DIR = WIKI_ROOT / "app" / ".cache"

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
