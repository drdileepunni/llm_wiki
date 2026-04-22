"""
Standalone test for generate_assessment() using data from the prior ingest.
Run from the repo root:
    python test_assess_generate.py
"""

import sys
import os
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / "app"))
os.environ.setdefault("WIKI_ROOT", str(Path(__file__).parent))

# Must be set before importing config — load from .env if present
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / "app" / ".env")

# ── Reconstruct ingest result from existing wiki files ────────────────────────
from backend.config import get_kb

kb = get_kb("agent_school")

# Summary from the source page abstract
INGEST_SUMMARY = (
    "Patient A was admitted with escalating critical illness over a 10-day period. "
    "The timeline covers lab results, vital signs, clinical notes, and orders including "
    "intubation, vasopressors, renal support, and antibiotics. Key events include "
    "hemodynamic instability, acute kidney injury, and mechanical ventilation."
)

# Reconstruct knowledge_gaps from actual gap files on disk
GAP_DIR = kb.wiki_dir / "gaps"
knowledge_gaps = []
for gap_file in sorted(GAP_DIR.glob("*.md")):
    sections = []
    in_block = False
    for line in gap_file.read_text().splitlines():
        if line.strip() == "## Missing Sections":
            in_block = True
            continue
        if in_block:
            if line.startswith("##"):
                break
            if line.startswith("- "):
                sections.append(line[2:].strip())
    if sections:
        knowledge_gaps.append({
            "page": f"wiki/gaps/{gap_file.name.replace('.md', '')}.md",
            "missing_sections": sections,
        })

# Files written from actual wiki files on disk
files_written = []
for subdir in ("sources", "entities", "concepts"):
    for f in sorted((kb.wiki_dir / subdir).glob("*.md")):
        files_written.append(f"wiki/{subdir}/{f.name}")

print(f"Reconstructed: {len(knowledge_gaps)} gaps, {len(files_written)} files written")
for g in knowledge_gaps:
    print(f"  KG: {g['page']}  ({len(g['missing_sections'])} sections)")

# ── Run generate_assessment ───────────────────────────────────────────────────
from backend.services.assess_pipeline import generate_assessment

SOURCE_SLUG = "intsnlg2851370-timeline-truncated-csv"
SOURCE_NAME = "INTSNLG2851370_timeline_truncated.csv"

print(f"\nGenerating assessment for {SOURCE_SLUG!r} ...")
assessment = generate_assessment(
    source_slug=SOURCE_SLUG,
    source_name=SOURCE_NAME,
    ingest_summary=INGEST_SUMMARY,
    knowledge_gaps=knowledge_gaps,
    files_written=files_written,
    kb=kb,
)

# ── Print result ──────────────────────────────────────────────────────────────
print(f"\nStatus: {assessment['status']}")
print(f"Questions: {len(assessment['questions'])}")
if assessment.get("error"):
    print(f"Error: {assessment['error']}")
else:
    for q in assessment["questions"]:
        kgs = ", ".join(q.get("linked_kgs") or []) or "—"
        print(f"  Q{q['id']}: {q['question'][:80]}")
        print(f"       KGs: {kgs}")

print(f"\nWritten to: {kb.wiki_root / 'assessments' / (SOURCE_SLUG + '.json')}")
