"""
Targeted section-fill pipeline for resolving wiki knowledge gaps.

Unlike run_ingest (which spreads a source across many pages and spawns new gaps),
this pipeline does one surgical thing:
  1. Read the specific entity/concept page that has missing sections
  2. Read the article text
  3. Ask Claude to write ONLY the missing sections into that page
  4. Write the updated page back
  5. Delete the gap file — the gap is closed

No gap detection pass. No new gap files. No spreading to other pages.
"""

import logging
from datetime import date
from pathlib import Path

from ..config import MODEL, KBConfig, _default_kb
from ..services.ingest_pipeline import (
    WRITE_TOOL,
    PHI_RULES,
    compute_diff,
    execute_file_op,
    resolve_gap_sections,
    _normalise_path,
)
from ..services.llm_client import get_llm_client
from ..services.token_tracker import log_call
from ..services.index_sync import sync_index
from ..services import vector_store as vs_mod

log = logging.getLogger("wiki.fill_sections")

FILL_TOOL = {
    "name": "fill_wiki_sections",
    "description": "Return the complete updated wiki page with the missing sections filled in.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The COMPLETE updated wiki page markdown including frontmatter. "
                    "Preserve all existing sections exactly. "
                    "Add each missing section as ## Section Name with content drawn "
                    "from the source article."
                ),
            },
            "sections_filled": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of sections that were successfully filled.",
            },
        },
        "required": ["content", "sections_filled"],
    },
}

_SYSTEM = f"""You are the maintainer of a medical/scientific knowledge base wiki.
You write accurate, de-identified, well-structured markdown wiki pages.

{PHI_RULES}

Rules:
- Write ONLY what the source article explicitly states
- Do NOT add background knowledge or speculate
- Each filled section must be based on evidence in the provided article
- Preserve all existing content in the page — only ADD the missing sections
- Use the wiki's frontmatter format and link style ([[Page Title]])
- Today: {date.today().isoformat()}
"""


def fill_sections(
    article_text:    str,
    article_title:   str,
    article_citation: str,
    gap_title:       str,
    gap_sections:    list[str],
    target_page_path: str,   # relative to wiki_root, e.g. "wiki/entities/saline.md"
    gap_file_path:   str,    # relative to wiki_root, e.g. "wiki/gaps/saline.md"
    kb: "KBConfig | None" = None,
    skip_quality_gate: bool = False,
) -> dict:
    """
    Fill specific missing sections of a wiki page using a source article.
    Closes the gap file on success.
    Returns a result dict compatible with the ingest result shape.
    """
    if kb is None:
        kb = _default_kb()

    # Normalize the path so the existence check and the write use the same location.
    # execute_file_op calls _normalise_path internally, so without this the read
    # would check kbs/kb/entities/foo.md while the write goes to kbs/kb/wiki/entities/foo.md.
    target_page_path = _normalise_path(target_page_path)

    llm = get_llm_client()
    log.info(
        "=== FILL_SECTIONS START  gap=%r  sections=%s  page=%s ===",
        gap_title, gap_sections, target_page_path,
    )

    # ── Read the current page (may not exist yet) ─────────────────────────────
    page_full_path = kb.wiki_root / target_page_path
    existing_content = ""
    if page_full_path.exists():
        existing_content = page_full_path.read_text(encoding="utf-8")
        log.info("Existing page: %d chars", len(existing_content))
    else:
        log.info("Page does not exist yet — will create from scratch")

    existing_block = (
        f"\n\nCURRENT PAGE CONTENT (preserve all existing sections, only add the missing ones):\n"
        f"---BEGIN CURRENT PAGE---\n{existing_content}\n---END CURRENT PAGE---"
        if existing_content else
        "\n\nThis page does not exist yet. Create it with proper frontmatter and the requested sections."
    )

    sections_str = "\n".join(f"- {s}" for s in gap_sections)

    prompt = f"""You are filling specific missing sections in a wiki page.

Wiki page to update: {target_page_path}
Topic: {gap_title}

Missing sections to fill (ONLY these — do not modify anything else):
{sections_str}
{existing_block}

---SOURCE ARTICLE START---
Title: {article_title}
Citation: {article_citation}

{article_text[:50_000]}
---SOURCE ARTICLE END---

Instructions:
- Read the source article and extract information relevant to each missing section
- Write each missing section using ONLY information from the source article
- Preserve the existing page content exactly — do not rewrite or reorder anything
- If the source does not cover a section, omit that section (do not invent content)
- Use ## for section headings
- Include `sources:` frontmatter update to reference this article
- Return the COMPLETE page content
"""

    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[FILL_TOOL],
        system=_SYSTEM,
        max_tokens=16_000,
    )

    log.info(
        "LLM response: stop_reason=%s  in=%d  out=%d",
        resp.stop_reason, resp.usage.input_tokens, resp.usage.output_tokens,
    )

    total_in  = resp.usage.input_tokens
    total_out = resp.usage.output_tokens

    fill_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not fill_block:
        msg = "Model did not return a tool_use block"
        log.error(msg)
        cost = log_call("fill_sections", gap_title, total_in, total_out, model=MODEL, kb_name=kb.name)
        return {
            "summary":       f"Failed to fill sections for {gap_title}",
            "files_written": [],
            "diffs":         [],
            "errors":        [msg],
            "knowledge_gaps": [],
            "gap_files_written": [],
            "input_tokens":  total_in,
            "output_tokens": total_out,
            "cost_usd":      cost,
            "model":         MODEL,
        }

    new_content      = fill_block.input.get("content", "")
    sections_filled  = fill_block.input.get("sections_filled", [])
    log.info("Sections filled: %s  content_length=%d", sections_filled, len(new_content))

    # ── Quality gate: reject near-empty fills ─────────────────────────────────
    # Strip frontmatter to measure actual prose content added.
    # A real section needs at least ~200 chars of prose — anything less means the
    # source article didn't actually cover this topic and the model had nothing to write.
    MIN_PROSE_CHARS = 200
    prose = new_content
    if prose.startswith("---"):
        end = prose.find("---", 3)
        prose = prose[end + 3:] if end != -1 else prose
    net_added = len(prose.strip()) - len(existing_content.strip())

    if net_added < MIN_PROSE_CHARS and not skip_quality_gate:
        log.warning(
            "Fill rejected — net prose added (%d chars) below threshold (%d). "
            "Source article likely did not cover this topic. Gap kept open.",
            net_added, MIN_PROSE_CHARS,
        )
        cost = log_call("fill_sections", gap_title, total_in, total_out, model=MODEL, kb_name=kb.name)
        return {
            "summary":       f"Fill skipped for {gap_title} — insufficient content from source (net +{net_added} chars)",
            "files_written": [],
            "diffs":         [],
            "errors":        [f"Source did not cover topic adequately (net +{net_added} chars added)"],
            "knowledge_gaps": [],
            "gap_files_written": [],
            "input_tokens":  total_in,
            "output_tokens": total_out,
            "cost_usd":      cost,
            "model":         MODEL,
        }

    # ── Write the updated page ────────────────────────────────────────────────
    op   = "update" if existing_content else "write"
    diff = compute_diff(target_page_path, new_content, op, kb)
    execute_file_op(target_page_path, new_content, op, kb)
    sync_index(kb.wiki_dir)
    # Embed the updated page for semantic search
    page_rel = "/".join(Path(target_page_path).parts[-2:])  # e.g. "concepts/aki.md"
    try:
        vs_mod.upsert(page_rel, new_content, kb.wiki_dir)
    except Exception as exc:
        log.warning("Vector upsert skipped for %s: %s", target_page_path, exc)
    log.info("✓ Written: %s  (+%d/-%d)", target_page_path, diff["added"], diff["removed"])

    # ── Close the gap file ────────────────────────────────────────────────────
    gap_path = kb.wiki_root / gap_file_path
    if gap_path.exists():
        if sections_filled:
            resolve_gap_sections(gap_file_path, sections_filled, kb)
            log.info("Gap sections resolved: %s", sections_filled)
        else:
            gap_path.unlink()
            log.info("Gap file deleted: %s", gap_file_path)

    cost = log_call("fill_sections", gap_title, total_in, total_out, model=MODEL, kb_name=kb.name)
    log.info(
        "=== FILL_SECTIONS END  filled=%s  cost=$%.4f ===",
        sections_filled, cost,
    )

    return {
        "summary":           f"Filled {len(sections_filled)} section(s) in {target_page_path}: {', '.join(sections_filled)}",
        "files_written":     [target_page_path],
        "diffs":             [diff],
        "errors":            [],
        "knowledge_gaps":    [],   # intentionally empty — no gap detection
        "gap_files_written": [],
        "sections_filled":   sections_filled,
        "input_tokens":      total_in,
        "output_tokens":     total_out,
        "cost_usd":          cost,
        "model":             MODEL,
    }
