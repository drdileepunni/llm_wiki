"""
Migrate existing gap files: broaden overly specific section names.

Run from the repo root:
    python tools/clean_gap_sections.py [--kb agent_school] [--dry-run]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ── Same logic as ingest_pipeline._broaden_section ───────────────────────────

_CONTEXT_STRIP_PATTERNS = [
    re.compile(
        r'\s+(?:in|for|during|after|following|with|associated with|related to|'
        r'due to|secondary to)\s+(?:severe\s+|acute\s+|chronic\s+|mild\s+|'
        r'moderate\s+|critical\s+)?[\w\s\-]+$',
        re.IGNORECASE,
    ),
]


def broaden_section(section: str) -> str:
    s = section.strip()
    for pat in _CONTEXT_STRIP_PATTERNS:
        broadened = pat.sub("", s).strip()
        if len(broadened) >= 3:
            s = broadened
            break
    return s or section


def deduplicate_sections(sections):
    seen = {}
    for s in sorted(sections, key=len):  # shorter (broader) first
        b = broaden_section(s)
        b_lower = b.lower()
        if b_lower not in seen:
            seen[b_lower] = b
    return sorted(seen.values())


def process_gap_file(path: Path, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    in_missing = False
    original_sections: list[str] = []
    other_lines: list[str] = []
    missing_start = missing_end = -1

    for i, line in enumerate(lines):
        if line.strip() == "## Missing Sections":
            missing_start = i
            in_missing = True
        elif in_missing:
            if line.startswith("##"):
                missing_end = i
                in_missing = False
            elif line.startswith("- "):
                original_sections.append(line[2:].strip())

    if not original_sections:
        return False

    cleaned = deduplicate_sections([broaden_section(s) for s in original_sections])
    if cleaned == sorted(original_sections):
        return False  # nothing to change

    print(f"\n{path.name}")
    removed = sorted(set(original_sections) - set(cleaned))
    added   = sorted(set(cleaned) - set(original_sections))
    if removed:
        print(f"  removed: {removed}")
    if added:
        print(f"  added:   {added}")

    if dry_run:
        return True

    # Rebuild the file with updated sections
    new_lines: list[str] = []
    i = 0
    while i < len(lines):
        if i == missing_start:
            new_lines.append("## Missing Sections")
            new_lines.append("")
            for s in cleaned:
                new_lines.append(f"- {s}")
            # Skip old section lines up to missing_end (or end of missing block)
            i += 1
            while i < len(lines):
                if lines[i].startswith("##") or (missing_end > 0 and i >= missing_end):
                    break
                i += 1
            continue
        new_lines.append(lines[i])
        i += 1

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Broaden gap file section names")
    parser.add_argument("--kb", default="agent_school")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    gaps_dir = repo_root / "kbs" / args.kb / "wiki" / "gaps"

    if not gaps_dir.exists():
        print(f"No gaps directory found at {gaps_dir}")
        sys.exit(0)

    gap_files = [f for f in gaps_dir.glob("*.md") if not f.stem.startswith("patient-")]
    print(f"Scanning {len(gap_files)} gap files in {gaps_dir}")
    changed = sum(1 for f in gap_files if process_gap_file(f, args.dry_run))
    print(f"\n{'Would update' if args.dry_run else 'Updated'} {changed} gap files.")


if __name__ == "__main__":
    main()
