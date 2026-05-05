#!/usr/bin/env python3
"""
verify_gap.py — Quickly check whether a resolved gap is now answerable.

Usage:
  python scripts/verify_gap.py wiki/gaps/some-entity.md
  python scripts/verify_gap.py wiki/gaps/some-entity.md --kb default
  python scripts/verify_gap.py --all                    # check all resolved gaps
  python scripts/verify_gap.py --pending                # check all pending (unresolved) gaps

The script runs a vector search for each missing_value/section in the gap file
and checks whether the referenced page surfaces in the top results with a score
above the threshold.  No LLM call — just one embedding per query (~$0.00).

Exit code: 0 if all queries verified, 1 if any are unverified.
"""

import argparse
import re
import sys
from pathlib import Path

# ── path setup ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.backend.config import _default_kb, KBConfig
from app.backend.services import vector_store as vs_mod
from app.backend.services.fill_sections_pipeline import (
    VERIFY_SCORE_THRESHOLD,
    _verify_gap_closure,
)

ANSI_GREEN  = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED    = "\033[91m"
ANSI_RESET  = "\033[0m"
ANSI_BOLD   = "\033[1m"


# ── Gap file parser ───────────────────────────────────────────────────────────

def _parse_gap_file(path: Path) -> dict:
    """Parse a gap .md file into a structured dict."""
    text = path.read_text(encoding="utf-8", errors="replace")
    referenced_page = ""
    times_opened = 0
    for line in text.splitlines():
        if line.startswith("referenced_page:"):
            referenced_page = line.split(":", 1)[1].strip()
        elif line.startswith("times_opened:"):
            try:
                times_opened = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    missing: list[str] = []
    resolved: list[str] = []
    resolution_question = ""
    missing_values: list[str] = []
    in_missing = in_rq = in_mv = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "## Missing Sections":
            in_missing = True; in_rq = False; in_mv = False
        elif stripped == "## Resolution Question":
            in_rq = True; in_missing = False; in_mv = False
        elif stripped == "## Specific Missing Values":
            in_mv = True; in_missing = False; in_rq = False
        elif stripped.startswith("## "):
            in_missing = in_rq = in_mv = False
        elif in_missing and line.startswith("- "):
            item = line[2:].strip()
            if item.startswith("RESOLVED:"):
                resolved.append(item[len("RESOLVED:"):].strip())
            else:
                missing.append(item)
        elif in_rq and stripped and not resolution_question:
            resolution_question = stripped
        elif in_mv and line.startswith("- "):
            missing_values.append(line[2:].strip())

    return {
        "file": path,
        "title": path.stem.replace("-", " ").title(),
        "referenced_page": referenced_page,
        "missing_sections": missing,
        "resolved_sections": resolved,
        "resolution_question": resolution_question,
        "missing_values": missing_values,
        "times_opened": times_opened,
    }


# ── Core verify logic ─────────────────────────────────────────────────────────

def verify_gap(gap: dict, kb: KBConfig) -> list[dict]:
    """
    Run verification for a single gap dict.
    Uses missing_values as queries when present, else missing_sections.
    Returns list of result dicts from _verify_gap_closure.
    """
    # Resolve target page path
    ref = gap["referenced_page"]
    if ref:
        target_path = ref if ref.startswith("wiki/") else f"wiki/{ref}"
    else:
        slug = gap["file"].stem
        target_path = f"wiki/entities/{slug}.md"

    # We need the current content of the target page to do content-token checks
    full_path = kb.wiki_root / target_path
    new_content = full_path.read_text(encoding="utf-8") if full_path.exists() else ""

    sections = gap["missing_sections"] or gap["resolved_sections"]
    results = _verify_gap_closure(
        target_path,
        new_content,
        sections,
        gap["missing_values"] or None,
        kb.wiki_dir,
    )
    return results


# ── Formatting ────────────────────────────────────────────────────────────────

def _print_results(gap_title: str, gap_file: Path, results: list[dict]) -> int:
    """Print formatted results. Returns number of unverified queries."""
    print(f"\n{ANSI_BOLD}{gap_title}{ANSI_RESET}  ({gap_file.name})")
    if not results:
        print(f"  {ANSI_YELLOW}No queries to check (gap has no missing_values or sections){ANSI_RESET}")
        return 0

    unverified = 0
    for r in results:
        query = r["query"][:72]
        if r.get("verified"):
            score   = r.get("score", 0)
            section = r.get("section", "?")
            tok_ok  = r.get("content_ok", True)
            marker  = f"{ANSI_GREEN}✓{ANSI_RESET}" if tok_ok else f"{ANSI_YELLOW}~{ANSI_RESET}"
            print(f"  {marker} {query}")
            print(f"      → {r.get('best_match', section)}#{section}  score={score:.3f}"
                  + ("" if tok_ok else "  (token overlap uncertain)"))
        else:
            unverified += 1
            score      = r.get("score", 0)
            best       = r.get("best_match", "none")
            err        = r.get("error", "")
            print(f"  {ANSI_RED}⚠{ANSI_RESET} {query}")
            if err:
                print(f"      ERROR: {err}")
            else:
                print(f"      best match: {best}  score={score:.3f}  (threshold={VERIFY_SCORE_THRESHOLD})")
    return unverified


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gap_file", nargs="?", help="Path to a gap .md file")
    parser.add_argument("--kb", default=None, help="KB name (default: first available)")
    parser.add_argument("--all", dest="all_gaps", action="store_true", help="Verify all gap files in the KB")
    parser.add_argument("--pending", action="store_true", help="Only gaps with unresolved sections")
    parser.add_argument("--threshold", type=float, default=None,
                        help=f"Score threshold override (default: {VERIFY_SCORE_THRESHOLD})")
    args = parser.parse_args()

    if args.threshold is not None:
        import app.backend.services.fill_sections_pipeline as _fp
        _fp.VERIFY_SCORE_THRESHOLD = args.threshold

    kb = _default_kb() if args.kb is None else KBConfig(args.kb)

    # Collect gap files to check
    gap_files: list[Path] = []
    if args.gap_file:
        p = Path(args.gap_file)
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if not p.exists():
            print(f"{ANSI_RED}Error: gap file not found: {args.gap_file}{ANSI_RESET}", file=sys.stderr)
            return 2
        gap_files = [p]
    elif args.all_gaps or args.pending:
        gaps_dir = kb.wiki_dir / "gaps"
        if not gaps_dir.exists():
            print(f"{ANSI_YELLOW}No gaps directory found at {gaps_dir}{ANSI_RESET}")
            return 0
        for f in sorted(gaps_dir.glob("*.md")):
            if f.stem.startswith("patient-"):
                continue
            gap_files.append(f)
    else:
        parser.print_help()
        return 2

    if not gap_files:
        print(f"{ANSI_YELLOW}No gap files found.{ANSI_RESET}")
        return 0

    total_unverified = 0
    for gf in gap_files:
        gap = _parse_gap_file(gf)
        if args.pending and not gap["missing_sections"]:
            continue
        results = verify_gap(gap, kb)
        unverified = _print_results(gap["title"], gf, results)
        total_unverified += unverified

    print()
    if total_unverified == 0:
        print(f"{ANSI_GREEN}{ANSI_BOLD}All queries verified.{ANSI_RESET}")
    else:
        print(f"{ANSI_RED}{ANSI_BOLD}{total_unverified} query/queries unverified.{ANSI_RESET}")

    return 0 if total_unverified == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
