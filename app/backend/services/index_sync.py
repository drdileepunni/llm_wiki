"""
Deterministic index.md sync — ensures every wiki page file has an entry.

Called after every ingest or fill_sections run so the chat pipeline can
always find newly written pages. This replaces the LLM's unreliable
index-update behaviour with a simple directory scan.
"""

import re
import logging
from pathlib import Path

log = logging.getLogger("wiki.index_sync")

_SECTIONS = ("sources", "entities", "concepts", "queries")
_SECTION_HEADERS = {
    "sources":  "## Sources",
    "entities": "## Entities",
    "concepts": "## Concepts",
    "queries":  "## Queries",
}


def _to_slug(s: str) -> str:
    """Normalise any title or stem to a comparison slug."""
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def _extract_page_meta(md_path: Path, rel_path: str):
    """
    Return (link_text, description) for an index entry.
    link_text  — page title from frontmatter (Title Case), falls back to stem
    description — "section/slug.md — first sentence of body"
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return md_path.stem, rel_path

    # Extract frontmatter title
    title = md_path.stem
    fm_match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).splitlines():
            if line.lower().startswith("title:"):
                raw = line.split(":", 1)[1].strip().strip('"').strip("'")
                if raw:
                    title = raw
                break

    # Extract first non-empty body line (skip headings) as description
    body = text[fm_match.end():].strip() if fm_match else text
    first_line = ""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("---"):
            first_line = stripped[:150]
            break

    desc = rel_path
    if first_line:
        desc = f"{rel_path} — {first_line}"

    return title, desc


def sync_index(wiki_dir: Path) -> int:
    """
    Scan wiki_dir for .md files in sources/, entities/, concepts/, queries/
    and append any slugs missing from index.md.

    Uses slug-normalised comparison so Title Case entries (written by the LLM)
    and lowercase-hyphenated entries (written by this function) both count as
    present. New entries include the page title and a one-line description so
    the chat pipeline can identify them.

    Returns the number of entries added.
    """
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        log.warning("index.md not found at %s — skipping sync", index_path)
        return 0

    text = index_path.read_text(encoding="utf-8")
    # Build a set of slugified versions of every [[...]] link already in index
    present_slugs = {_to_slug(t) for t in re.findall(r'\[\[([^\]|]+)\]\]', text)}

    additions: dict[str, list[tuple[str, str]]] = {s: [] for s in _SECTIONS}
    for section in _SECTIONS:
        section_dir = wiki_dir / section
        if not section_dir.exists():
            continue
        for f in sorted(section_dir.glob("*.md")):
            if f.name == "index.md":
                continue
            if _to_slug(f.stem) not in present_slugs:
                rel_path = f"{section}/{f.name}"
                link_text, description = _extract_page_meta(f, rel_path)
                additions[section].append((link_text, description))

    total = sum(len(v) for v in additions.values())
    if total == 0:
        return 0

    for section, entries in additions.items():
        if not entries:
            continue
        header = _SECTION_HEADERS[section]
        block = "\n".join(f"- [[{title}]] — {desc}" for title, desc in entries) + "\n"

        if header in text:
            text = text.replace(header + "\n", header + "\n" + block, 1)
        else:
            text = text.rstrip() + f"\n\n{header}\n{block}"

        log.info("index_sync: added %d entries to %s", len(entries), header)

    index_path.write_text(text, encoding="utf-8")
    log.info("index_sync: %d total entries added to %s", total, index_path)
    return total
