"""
Defrag pipeline — resolves scope_contamination flags written by the ingest and
fill_sections pipelines.

For each flagged page:
  1. Read the page and its violation list from frontmatter
  2. Ask the LLM to extract the violating content and produce a cleaned version
     of the source page
  3. Write / update the target page with the extracted content
  4. Replace the source page with the cleaned version
  5. Clear the scope_contamination flag from the source page

No gap detection. No new gap files. This is purely a curation / cleanup pass.
"""

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

from ..config import MODEL, KBConfig, _default_kb
from ..services.ingest_pipeline import (
    WRITE_TOOL,
    PHI_RULES,
    compute_diff,
    execute_file_op,
    _normalise_path,
    _wiki_link_context,
)
from ..services.llm_client import get_llm_client
from ..services.token_tracker import log_call
from ..services.index_sync import sync_index
from ..services import vector_store as vs_mod
from ..services.quality_scorer import (
    parse_scope_contamination,
    clear_contamination_frontmatter,
    update_quality_frontmatter,
    score_page,
    _default_scope,
    _parse_subtype,
    parse_scope,
    update_scope_frontmatter,
)
from ..services.ingest_pipeline import (
    _parse_written_sections,
    resolve_gap_sections,
)

log = logging.getLogger("wiki.defrag")

EXTRACT_TOOL = {
    "name": "defrag_page",
    "description": (
        "Extract out-of-scope content from the source page and produce "
        "the content for the target page."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_cleaned": {
                "type": "string",
                "description": (
                    "The COMPLETE source page with the out-of-scope content removed. "
                    "All in-scope sections are preserved exactly. "
                    "Replace removed content with a one-line cross-reference: "
                    "'See [[Target Page]] for [topic].'"
                ),
            },
            "target_content": {
                "type": "string",
                "description": (
                    "The COMPLETE target page markdown — either enriched (if the page "
                    "already exists) or newly created. Must include proper frontmatter."
                ),
            },
            "sections_moved": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of sections that had content moved to the target page.",
            },
        },
        "required": ["source_cleaned", "target_content", "sections_moved"],
    },
}

_SYSTEM = f"""You are curating a medical/scientific knowledge base wiki.
You move misplaced content from one page to the correct page.

{PHI_RULES}

Rules:
- Preserve all in-scope content in the source page exactly
- Remove only the specified out-of-scope content
- Replace removed content with a brief cross-reference line
- For the target page: if it exists, APPEND the extracted content (no duplication);
  if it doesn't exist, CREATE it with proper frontmatter
- Use [[Page Title]] wiki links for cross-references
- Today: {date.today().isoformat()}
"""


def _find_or_create_target_path(belongs_on: str, is_new_page: bool, kb: KBConfig) -> str:
    """Return wiki-relative path for the target page (create dir if needed)."""
    slug = re.sub(r"[^a-z0-9]+", "-", belongs_on.lower()).strip("-")
    # Check entities and concepts for an existing file
    for section in ("entities", "concepts"):
        candidate = kb.wiki_dir / section / f"{slug}.md"
        if candidate.exists():
            return f"wiki/{section}/{slug}.md"
    # Default to concepts for new condition pages, entities otherwise
    section = "concepts"
    return f"wiki/{section}/{slug}.md"


def defrag_page(
    source_page_path: str,
    kb: Optional[KBConfig] = None,
) -> dict:
    """
    Defrag a single page that has scope_contamination flags.
    Returns a result dict describing what was moved.
    """
    if kb is None:
        kb = _default_kb()

    source_page_path = _normalise_path(source_page_path)
    full_source = kb.wiki_root / source_page_path

    if not full_source.exists():
        return {
            "ok": False,
            "error": f"Source page not found: {source_page_path}",
            "moves": [],
        }

    content = full_source.read_text(encoding="utf-8")
    violations = parse_scope_contamination(content)

    if not violations:
        return {"ok": True, "message": "No scope_contamination flags found.", "moves": []}

    llm = get_llm_client()
    wiki_links = _wiki_link_context(kb.wiki_dir)
    page_title = Path(source_page_path).stem.replace("-", " ").title()

    total_in = total_out = 0
    all_moves = []
    all_errors = []
    gaps_closed: list[str] = []

    # Group violations by target page so we defrag to each target in one call
    from collections import defaultdict
    by_target: dict = defaultdict(list)
    for v in violations:
        by_target[v.get("belongs_on", "Unknown")].append(v)

    current_source_content = content

    for belongs_on, target_violations in by_target.items():
        is_new_page = any(v.get("is_new_page", True) for v in target_violations)
        target_path = _find_or_create_target_path(belongs_on, is_new_page, kb)
        full_target = kb.wiki_root / target_path
        target_exists = full_target.exists()
        existing_target_content = full_target.read_text(encoding="utf-8") if target_exists else ""

        violation_block = "\n".join(
            f"  Section: {v.get('section', '?')}\n"
            f"  Content to move: {v.get('excerpt', v.get('content', ''))}"
            for v in target_violations
        )

        target_block = (
            f"\n\nEXISTING TARGET PAGE (append extracted content, no duplication):\n"
            f"---BEGIN TARGET---\n{existing_target_content}\n---END TARGET---"
            if target_exists
            else f"\n\nTarget page '{belongs_on}' does not exist yet — create it."
        )

        subtype = _parse_subtype(current_source_content)
        target_subtype = _parse_subtype(existing_target_content) if target_exists else subtype
        target_scope = (
            parse_scope(existing_target_content)
            if target_exists
            else _default_scope(belongs_on, target_subtype)
        )

        prompt = f"""You are defraging a wiki page by moving misplaced content to its correct page.

SOURCE PAGE: {source_page_path}  (title: {page_title})
TARGET PAGE: {target_path}  (title: {belongs_on})

CONTENT TO MOVE (these items are out of scope for the source page):
{violation_block}

TARGET SCOPE: {target_scope}

{wiki_links}

CURRENT SOURCE PAGE:
---BEGIN SOURCE---
{current_source_content}
---END SOURCE---
{target_block}

Instructions:
1. Remove the out-of-scope content from the source page
2. Replace it with: "See [[{belongs_on}]] for details."
3. Add the content to the target page (append if it exists, create if it doesn't)
4. Preserve everything else in both pages exactly
5. Return COMPLETE content for both pages — do not truncate
"""

        try:
            resp = llm.create_message(
                messages=[{"role": "user", "content": prompt}],
                tools=[EXTRACT_TOOL],
                system=_SYSTEM,
                max_tokens=16000,
            )
            total_in  += resp.usage.input_tokens
            total_out += resp.usage.output_tokens

            block = next((b for b in resp.content if b.type == "tool_use"), None)
            if not block:
                msg = f"No tool_use block for defrag of {source_page_path} → {belongs_on}"
                log.error(msg)
                all_errors.append(msg)
                continue

            source_cleaned  = block.input.get("source_cleaned", "")
            target_content  = block.input.get("target_content", "")
            sections_moved  = block.input.get("sections_moved", [])

            if not source_cleaned or not target_content:
                all_errors.append(f"Empty content returned for {belongs_on}")
                continue

            # Ensure scope on target
            if not parse_scope(target_content):
                target_content = update_scope_frontmatter(
                    target_content, _default_scope(belongs_on, _parse_subtype(target_content))
                )

            # Score both pages
            for pg_content, pg_path in [(source_cleaned, source_page_path), (target_content, target_path)]:
                try:
                    sc = score_page(pg_content)
                    if sc:
                        pg_content_scored = update_quality_frontmatter(pg_content, sc)
                        if pg_path == source_page_path:
                            source_cleaned = pg_content_scored
                        else:
                            target_content = pg_content_scored
                except Exception:
                    pass

            # Write target
            target_op = "update" if target_exists else "write"
            target_diff = compute_diff(target_path, target_content, target_op, kb)
            execute_file_op(target_path, target_content, target_op, kb)
            try:
                vs_mod.upsert("/".join(Path(target_path).parts[-2:]), target_content, kb.wiki_dir)
            except Exception:
                pass
            log.info("✓ Target written: %s  (+%d/-%d)", target_path, target_diff["added"], target_diff["removed"])

            # Close any open gap on the target page for sections now filled by defrag
            try:
                target_stem = Path(target_path).stem
                gap_rel = f"wiki/gaps/{target_stem}.md"
                if (kb.wiki_root / gap_rel).exists():
                    written_sections = _parse_written_sections(target_content)
                    gap_existed = (kb.wiki_root / gap_rel).exists()
                    resolve_gap_sections(gap_rel, list(written_sections), kb)
                    if gap_existed and not (kb.wiki_root / gap_rel).exists():
                        gaps_closed.append(gap_rel)
                        log.info("  gap fully resolved on target: %s", gap_rel)
                    elif gap_existed:
                        log.info("  gap partially resolved on target: %s", gap_rel)
            except Exception as _ge:
                log.warning("  Gap resolution on target failed (non-fatal): %s", _ge)

            current_source_content = source_cleaned

            all_moves.append({
                "from_page":     source_page_path,
                "to_page":       target_path,
                "sections_moved": sections_moved,
                "target_is_new": not target_exists,
                "target_diff":   target_diff,
            })

        except Exception as exc:
            log.exception("Defrag error for %s → %s: %s", source_page_path, belongs_on, exc)
            all_errors.append(str(exc))

    # Write the cleaned source page (violations cleared)
    if all_moves:
        cleaned_final = clear_contamination_frontmatter(current_source_content)
        try:
            sc = score_page(cleaned_final)
            if sc:
                cleaned_final = update_quality_frontmatter(cleaned_final, sc)
        except Exception:
            pass
        source_diff = compute_diff(source_page_path, cleaned_final, "update", kb)
        execute_file_op(source_page_path, cleaned_final, "update", kb)
        try:
            vs_mod.upsert("/".join(Path(source_page_path).parts[-2:]), cleaned_final, kb.wiki_dir)
        except Exception:
            pass
        sync_index(kb.wiki_dir)
        log.info("✓ Source cleaned: %s  (+%d/-%d)", source_page_path, source_diff["added"], source_diff["removed"])

    cost = log_call("defrag", page_title, total_in, total_out, model=MODEL, kb_name=kb.name)

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation": "defrag",
        "kb": kb.name,
        "source": page_title,
        "files_written": [source_page_path] + [m["to_page"] for m in all_moves],
        "gaps_opened": [],
        "gaps_closed": gaps_closed,
        "sections_filled": [],
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": cost,
        "moves": all_moves,
    })

    return {
        "ok": len(all_errors) == 0,
        "source_page": source_page_path,
        "moves": all_moves,
        "errors": all_errors,
        "input_tokens":  total_in,
        "output_tokens": total_out,
        "cost_usd": cost,
        "model": MODEL,
    }


def defrag_all(
    kb: Optional[KBConfig] = None,
    dry_run: bool = False,
) -> dict:
    """
    Find all pages with scope_contamination flags and defrag them.
    Returns a summary of all moves performed.
    """
    if kb is None:
        kb = _default_kb()

    flagged = []
    for section in ("entities", "concepts"):
        d = kb.wiki_dir / section
        if not d.exists():
            continue
        for md_file in sorted(d.glob("*.md")):
            content = md_file.read_text(encoding="utf-8", errors="replace")
            violations = parse_scope_contamination(content)
            if violations:
                rel = f"wiki/{section}/{md_file.name}"
                flagged.append({
                    "path": rel,
                    "title": md_file.stem.replace("-", " ").title(),
                    "violations": violations,
                })

    log.info("defrag_all: found %d flagged page(s)", len(flagged))

    if dry_run:
        return {"dry_run": True, "flagged_pages": flagged, "total": len(flagged)}

    results = []
    for page in flagged:
        log.info("Defraging: %s", page["path"])
        result = defrag_page(page["path"], kb)
        results.append(result)

    total_moves = sum(len(r.get("moves", [])) for r in results)
    total_errors = sum(len(r.get("errors", [])) for r in results)

    return {
        "pages_processed": len(flagged),
        "total_moves": total_moves,
        "total_errors": total_errors,
        "results": results,
    }
