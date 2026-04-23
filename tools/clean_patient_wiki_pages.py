#!/usr/bin/env python3
"""
Delete patient-specific pages that were ingested into the general wiki
(wiki/entities/, wiki/sources/, etc.) before the patient_dir isolation was added.

Usage:
    python tools/clean_patient_wiki_pages.py             # dry run — shows what would be deleted
    python tools/clean_patient_wiki_pages.py --execute   # actually deletes

Patient IDs are discovered from the timelines/ directory. Any wiki page whose
filename starts with the lowercase patient base ID (e.g. "intsnlg2851387") is
treated as patient-specific and removed.
"""

import re
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backend.config import WIKI_ROOT
from app.backend.services import vector_store as vs_mod
from app.backend.config import _default_kb


def _patient_bases(timelines_dir: Path) -> set[str]:
    """Return lowercase base IDs from all slug dirs in timelines/."""
    bases: set[str] = set()
    if not timelines_dir.exists():
        return bases
    for d in timelines_dir.iterdir():
        if d.is_dir():
            base = re.sub(r"_\d+$", "", d.name).lower()
            bases.add(base)
    return bases


def clean_patient_pages(dry_run: bool = True) -> list[str]:
    kb = _default_kb()
    timelines_dir = WIKI_ROOT / "timelines"
    bases = _patient_bases(timelines_dir)

    if not bases:
        print("No patient slugs found in timelines/ — nothing to do.")
        return []

    print(f"Patient base IDs detected: {sorted(bases)}")
    print()

    deleted: list[str] = []
    sections = ("entities", "concepts", "sources", "queries", "gaps")

    for section in sections:
        section_dir = kb.wiki_dir / section
        if not section_dir.exists():
            continue
        for f in sorted(section_dir.glob("*.md")):
            stem_lower = f.stem.lower().replace("-", "")  # normalise hyphens
            for base in bases:
                base_norm = base.replace("-", "")
                if stem_lower.startswith(base_norm):
                    rel_path = f"{section}/{f.name}"
                    if dry_run:
                        print(f"  [DRY RUN] would delete: {rel_path}")
                    else:
                        vs_mod.remove(rel_path, kb.wiki_dir)
                        f.unlink()
                        print(f"  Deleted: {rel_path}")
                    deleted.append(rel_path)
                    break

    print()
    print(f"{'Would delete' if dry_run else 'Deleted'} {len(deleted)} file(s).")
    if dry_run and deleted:
        print("Re-run with --execute to apply.")
    return deleted


if __name__ == "__main__":
    dry_run = "--execute" not in sys.argv
    if dry_run:
        print("=== DRY RUN — pass --execute to actually delete ===\n")
    clean_patient_pages(dry_run=dry_run)
