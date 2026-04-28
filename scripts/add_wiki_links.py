"""
Retroactively add [[wiki links]] to existing entity/concept pages.

For each page, asks the LLM to insert [[Page Title]] links wherever known
wiki pages are mentioned — without changing any other content.

Usage:
    python scripts/add_wiki_links.py [--kb kbs/agent_school] [--dry-run]
"""

import argparse
import logging
import sys
from pathlib import Path

# Repo root on sys.path so we can import app modules
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.backend.services.llm_client import get_llm_client
from app.backend.services.ingest_pipeline import _wiki_link_context
from app.backend.services import vector_store as vs_mod

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("add_wiki_links")

LINK_TOOL = {
    "name": "updated_page",
    "description": "Return the page content with [[wiki links]] added.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The COMPLETE page markdown. Identical to the input except that "
                    "plain-text mentions of known wiki pages are now wrapped in [[Page Title]] syntax. "
                    "Do NOT change wording, headings, frontmatter values, or any other content."
                ),
            },
            "links_added": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of [[Page Title]] links that were inserted.",
            },
        },
        "required": ["content", "links_added"],
    },
}

SYSTEM = (
    "You are a wiki editor. Your only job is to add [[Page Title]] cross-reference links "
    "to an existing wiki page. You must NOT change any wording, restructure content, "
    "add new information, or modify frontmatter values. Only wrap plain-text mentions "
    "of known wiki pages in [[double brackets]]."
)


def add_links_to_page(content: str, wiki_links_block: str, llm) -> tuple[str, list[str]]:
    prompt = f"""Add [[wiki links]] to the page below.

{wiki_links_block}

Rules:
- ONLY wrap mentions of the pages listed above in [[Page Title]] syntax
- Do NOT link the page to itself
- Do NOT change any wording, headings, or frontmatter
- Do NOT add links to things not in the known pages list
- First mention of each page in the body = link; subsequent mentions can stay plain
- Frontmatter values (title:, sources:, etc.) must remain unchanged

---PAGE START---
{content}
---PAGE END---

Return the complete page with links added."""

    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[LINK_TOOL],
        system=SYSTEM,
        max_tokens=8000,
    )
    block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not block:
        return content, []
    return block.input.get("content", content), block.input.get("links_added", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", default="kbs/agent_school", help="Path to KB root (relative to repo root)")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    kb_root = ROOT / args.kb
    wiki_dir = kb_root / "wiki"

    pages = []
    for prefix in ("entities", "concepts"):
        d = wiki_dir / prefix
        if d.exists():
            pages.extend(sorted(d.glob("*.md")))

    if not pages:
        log.error("No entity/concept pages found in %s", wiki_dir)
        sys.exit(1)

    log.info("Found %d pages to process", len(pages))
    wiki_links_block = _wiki_link_context(wiki_dir)
    if not wiki_links_block.strip():
        log.error("No known wiki pages found — nothing to link to")
        sys.exit(1)

    llm = get_llm_client()
    total_links = 0

    for page_path in pages:
        rel = page_path.relative_to(wiki_dir)
        content = page_path.read_text(encoding="utf-8")

        log.info("Processing %s ...", rel)
        new_content, links_added = add_links_to_page(content, wiki_links_block, llm)

        if not links_added:
            log.info("  → no new links to add")
            continue

        log.info("  → added %d link(s): %s", len(links_added), links_added)
        total_links += len(links_added)

        if args.dry_run:
            log.info("  [DRY RUN] would write %d chars", len(new_content))
            continue

        page_path.write_text(new_content, encoding="utf-8")

        # Re-embed so vector store reflects the updated content
        try:
            vs_mod.upsert(str(rel), new_content, wiki_dir)
        except Exception as exc:
            log.warning("  vector upsert failed: %s", exc)

    log.info("Done. Total links added: %d", total_links)


if __name__ == "__main__":
    main()
