"""
Migrate existing wiki entity/concept pages to the canonical registry structure.

WHAT THIS SCRIPT DOES:
  Phase 1 — Near-duplicate detection: Finds pages about the same topic
             (score > dup_threshold, default 0.93) and merges the smaller
             into the larger. E.g. ringer-lactate.md + ringers-lactate.md → one page.
  Phase 2 — Self-register survivors: Every surviving distinct page is registered
             as its own canonical entry. This ensures future KG resolutions for
             "noradrenaline dosing" route to noradrenaline.md rather than creating
             a new noradrenaline-dosing.md.

Usage:
    # Embed all pages first (required if many pages show as "no embedding")
    python scripts/migrate_to_canonical.py --kb agent_school --rebuild-embeddings

    # Dry run — shows what would be merged, no changes written
    python scripts/migrate_to_canonical.py --kb agent_school

    # Execute merges + self-register all survivors
    python scripts/migrate_to_canonical.py --kb agent_school --execute

    # Tune near-duplicate threshold (default 0.93)
    python scripts/migrate_to_canonical.py --kb agent_school --dup-threshold 0.92 --execute
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _load_vector_store(wiki_dir: Path) -> list[dict]:
    p = wiki_dir / "vector_store.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _save_vector_store(wiki_dir: Path, records: list[dict]) -> None:
    p = wiki_dir / "vector_store.json"
    p.write_text(json.dumps(records, separators=(",", ":")), encoding="utf-8")


def _load_registry(wiki_dir: Path) -> list[dict]:
    p = wiki_dir / "canonical_registry.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _section_count(content: str) -> int:
    return sum(1 for l in content.splitlines() if l.startswith("## "))


def _char_count(content: str) -> int:
    return len(content)


def _extract_sections(content: str) -> list[tuple[str, str]]:
    sections = []
    current_heading = None
    current_body: list[str] = []
    for line in content.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line[3:].strip()
            current_body = []
        elif current_heading is not None:
            current_body.append(line)
    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_body).strip()))
    return sections


def _merge_sections_into(target_path: Path, donor_path: Path) -> int:
    """Append sections from donor not already in target. Returns number appended."""
    target_content = target_path.read_text(encoding="utf-8")
    donor_content = donor_path.read_text(encoding="utf-8")
    target_headings = {h.lower() for h, _ in _extract_sections(target_content)}
    additions = []
    for heading, body in _extract_sections(donor_content):
        if heading.lower() not in target_headings and body.strip():
            additions.append(f"\n## {heading}\n\n{body}\n")
    if additions:
        target_path.write_text(target_content.rstrip() + "\n" + "".join(additions), encoding="utf-8")
    return len(additions)


def _update_gap_files(wiki_dir: Path, old_path: str, new_path: str) -> list[str]:
    gaps_dir = wiki_dir / "gaps"
    if not gaps_dir.exists():
        return []
    updated = []
    for gap_file in gaps_dir.glob("*.md"):
        text = gap_file.read_text(encoding="utf-8")
        if old_path in text or f"wiki/{old_path}" in text:
            text = text.replace(f"wiki/{old_path}", f"wiki/{new_path}")
            text = text.replace(old_path, new_path)
            gap_file.write_text(text, encoding="utf-8")
            updated.append(gap_file.name)
    return updated


def rebuild_embeddings(kb_name: str) -> None:
    """Embed every entity/concept page that doesn't yet have a vector store entry."""
    from app.backend.config import get_kb
    from app.backend.services import vector_store as vs_mod

    kb = get_kb(kb_name)
    wiki_dir = kb.wiki_dir
    records = _load_vector_store(wiki_dir)
    stored = {r["path"] for r in records}

    page_dirs = ["entities", "concepts"]
    to_embed = []
    for d in page_dirs:
        p = wiki_dir / d
        if p.exists():
            for f in sorted(p.glob("*.md")):
                rel = f"{d}/{f.name}"
                if rel not in stored:
                    to_embed.append(rel)

    print(f"Embedding {len(to_embed)} unembedded pages (this may take a few minutes)...")
    for i, rel in enumerate(to_embed, 1):
        try:
            content = (wiki_dir / rel).read_text(encoding="utf-8")
            ok = vs_mod.upsert(rel, content, wiki_dir)
            status = "OK" if ok else "FAIL"
            print(f"  [{i}/{len(to_embed)}] {status}: {rel}")
        except Exception as exc:
            print(f"  [{i}/{len(to_embed)}] ERROR: {rel}: {exc}")

    print(f"Done embedding. {len(to_embed)} pages processed.")


def run(kb_name: str, dup_threshold: float, execute: bool) -> None:
    from app.backend.config import get_kb
    kb = get_kb(kb_name)
    wiki_dir = kb.wiki_dir

    registry = _load_registry(wiki_dir)
    canonical_paths = {e["path"] for e in registry}
    vector_records = _load_vector_store(wiki_dir)
    emb_by_path = {r["path"]: r["embedding"] for r in vector_records}

    page_dirs = ["entities", "concepts"]
    all_pages: list[str] = []
    for d in page_dirs:
        p = wiki_dir / d
        if p.exists():
            all_pages.extend(f"{d}/{f.name}" for f in sorted(p.glob("*.md")))

    # Separate pages with embeddings from those without
    embedded = [p for p in all_pages if p in emb_by_path]
    not_embedded = [p for p in all_pages if p not in emb_by_path]

    print(f"\nKB: {kb_name}")
    print(f"Canonical pages already registered: {len(canonical_paths)}")
    print(f"Total entity/concept pages: {len(all_pages)}")
    print(f"  With embeddings: {len(embedded)}")
    print(f"  Without embeddings (run --rebuild-embeddings first): {len(not_embedded)}")
    print(f"Near-duplicate threshold: {dup_threshold}")
    print()

    # ── Phase 1: Near-duplicate detection ────────────────────────────────────
    # Compare every embedded page against every other embedded page.
    # If score > dup_threshold, the smaller page is a duplicate of the larger.
    # "Larger" = more characters (richer content wins).
    print("=" * 70)
    print("PHASE 1: NEAR-DUPLICATE DETECTION")
    print("=" * 70)

    # Build pairwise similarity — O(n^2) but n ~ 200 so ~20k comparisons, fast
    merge_into: dict[str, str] = {}  # donor → target (the richer page)
    absorbed: set[str] = set()

    pages_with_emb = [p for p in embedded if p not in canonical_paths]

    for i, p1 in enumerate(pages_with_emb):
        if p1 in absorbed:
            continue
        emb1 = emb_by_path[p1]
        for p2 in pages_with_emb[i + 1:]:
            if p2 in absorbed:
                continue
            score = _cosine(emb1, emb_by_path[p2])
            if score >= dup_threshold:
                # Pick richer page (more chars) as target
                c1 = _char_count((wiki_dir / p1).read_text(encoding="utf-8"))
                c2 = _char_count((wiki_dir / p2).read_text(encoding="utf-8"))
                if c1 >= c2:
                    target, donor = p1, p2
                else:
                    target, donor = p2, p1
                merge_into[donor] = target
                absorbed.add(donor)
                print(f"  DUP  {donor}")
                print(f"    → {target}  (score={score:.3f})")

    if not merge_into:
        print("  No near-duplicates found at threshold", dup_threshold)

    # ── Phase 2: Self-register all survivors ─────────────────────────────────
    print(f"\n{'=' * 70}")
    print("PHASE 2: SELF-REGISTER SURVIVING DISTINCT PAGES")
    print("=" * 70)
    print("(Every distinct page becomes its own canonical entry so future KG")
    print(" resolutions route to it rather than creating new granular stubs.)\n")

    survivors = [p for p in all_pages if p not in absorbed and p not in canonical_paths]
    new_registrations = [p for p in survivors if p not in canonical_paths]

    print(f"  Pages to self-register: {len(new_registrations)}")
    for p in sorted(new_registrations)[:10]:
        print(f"    + {p}")
    if len(new_registrations) > 10:
        print(f"    ... and {len(new_registrations) - 10} more")

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print("=" * 70)
    print(f"  Near-duplicates to merge:         {len(merge_into)}")
    print(f"  Distinct pages to self-register:  {len(new_registrations)}")
    print(f"  Pages with no embeddings (skip):  {len(not_embedded)}")

    if not execute:
        print("\n[DRY RUN] No changes written. Re-run with --execute to apply.")
        return

    # ── Execute ───────────────────────────────────────────────────────────────
    print("\n[EXECUTING]\n")
    from app.backend.services import vector_store as vs_mod
    from app.backend.services.canonical_registry import register as _register

    archive_dir = wiki_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    updated_records = list(vector_records)

    # Merge near-duplicates
    for donor, target in merge_into.items():
        donor_full = wiki_dir / donor
        target_full = wiki_dir / target
        if not donor_full.exists() or not target_full.exists():
            continue
        n = _merge_sections_into(target_full, donor_full)
        # Re-embed target with merged content
        try:
            vs_mod.upsert(target, target_full.read_text(encoding="utf-8"), wiki_dir)
        except Exception as exc:
            print(f"  WARNING: re-embed failed for {target}: {exc}")
        # Update gap files
        gap_updates = _update_gap_files(wiki_dir, donor, target)
        # Archive donor
        archive_path = archive_dir / donor.replace("/", "_")
        shutil.move(str(donor_full), str(archive_path))
        updated_records = [r for r in updated_records if r["path"] != donor]
        print(f"  MERGED  {donor} → {target}  (+{n} sections, {len(gap_updates)} gap files updated)")

    _save_vector_store(wiki_dir, updated_records)

    # Self-register surviving pages
    registered = 0
    for page_path in new_registrations:
        page_full = wiki_dir / page_path
        if not page_full.exists():
            continue
        title = Path(page_path).stem.replace("-", " ").title()
        _register(page_path, title, wiki_dir)
        registered += 1

    print(f"\nDone.")
    print(f"  Merged {len(merge_into)} near-duplicate pairs.")
    print(f"  Registered {registered} pages as canonical entries.")
    print(f"  Archived pages are in: {archive_dir}")
    print(f"\nNext: the canonical registry now has {len(canonical_paths) + registered} entries.")
    print("Future KG resolutions will route to these pages automatically.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate wiki pages to canonical registry structure")
    parser.add_argument("--kb", default="agent_school", help="KB name (default: agent_school)")
    parser.add_argument("--dup-threshold", type=float, default=0.93,
                        help="Cosine similarity threshold for near-duplicate detection (default: 0.93)")
    parser.add_argument("--execute", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--rebuild-embeddings", action="store_true",
                        help="Embed all unembedded pages before migration (run this first)")
    args = parser.parse_args()

    if args.rebuild_embeddings:
        rebuild_embeddings(args.kb)
    else:
        run(args.kb, args.dup_threshold, args.execute)
