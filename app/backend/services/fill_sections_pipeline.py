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
    _wiki_link_context,
)
from ..services.llm_client import get_llm_client
from ..services.token_tracker import log_call
from ..services.index_sync import sync_index
from ..services import vector_store as vs_mod

log = logging.getLogger("wiki.fill_sections")


def _parse_section_quality(content: str) -> dict:
    """
    Parse section_quality from a page's YAML frontmatter.
    Returns {section_name: {"score": int, "flags": list[str]}}
    """
    import re as _re
    quality: dict = {}
    fm = _re.match(r'^---\r?\n([\s\S]*?)\r?\n---', content)
    if not fm:
        return quality
    block = fm.group(1)
    # Match YAML entries like:
    #   "Section Name":
    #     score: 3
    #     flags: [insufficient_content]
    pattern = _re.compile(
        r'"([^"]+)":\s*\n\s+score:\s*(\d+)(?:\s*\n\s+flags:\s*\[([^\]]*)\])?'
    )
    for m in pattern.finditer(block):
        flags_raw = m.group(3) or ""
        flags = [f.strip() for f in flags_raw.split(",") if f.strip()]
        quality[m.group(1)] = {"score": int(m.group(2)), "flags": flags}
    return quality


def _extract_section_body(content: str, heading: str) -> str:
    """Return the prose body of a named ## section, stripped of the heading line itself."""
    heading_norm = heading.strip().lower()
    lines = content.splitlines()
    in_section = False
    body: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if line[3:].strip().lower() == heading_norm:
                in_section = True
            elif in_section:
                break
        elif in_section:
            body.append(line)
    return "\n".join(body).strip()


def _dedup_sections(content: str) -> str:
    """
    Merge duplicate ## section headings in-place.
    When a heading appears more than once, the body of later occurrences is
    appended to the first occurrence (new lines only), and the duplicate
    heading block is removed.
    """
    lines = content.splitlines()

    # Locate frontmatter end (---...---)
    fm_end = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                fm_end = i + 1
                break

    frontmatter = lines[:fm_end]
    body = lines[fm_end:]

    # Split body into segments: list of [heading_line_or_None, [body_lines]]
    segments: list[tuple[str | None, list[str]]] = []
    cur_heading: str | None = None
    cur_body: list[str] = []

    for line in body:
        if line.startswith("## "):
            segments.append((cur_heading, cur_body))
            cur_heading = line
            cur_body = []
        else:
            cur_body.append(line)
    segments.append((cur_heading, cur_body))

    # Merge duplicates — keep first occurrence, append new content from later ones
    seen: dict[str, int] = {}   # normalised heading → index in merged
    merged: list[tuple[str | None, list[str]]] = []

    for heading, body_lines in segments:
        if heading is None:
            merged.append((heading, body_lines))
            continue
        key = heading.strip().lower()
        if key in seen:
            idx = seen[key]
            existing = set(merged[idx][1])
            additions = [l for l in body_lines if l.strip() and l not in existing]
            if additions:
                merged[idx] = (merged[idx][0], merged[idx][1] + additions)
            log.debug("_dedup_sections: merged duplicate '%s'", heading.strip())
        else:
            seen[key] = len(merged)
            merged.append((heading, body_lines))

    result: list[str] = list(frontmatter)
    for heading, body_lines in merged:
        if heading is not None:
            result.append(heading)
        result.extend(body_lines)

    return "\n".join(result)

FILL_TOOL = {
    "name": "fill_wiki_sections",
    "description": "Return the complete updated wiki page with new knowledge from the source article added to the requested sections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The COMPLETE updated wiki page markdown including frontmatter. "
                    "All existing sections are preserved exactly. "
                    "The requested sections have been enriched: new points added, "
                    "existing points NOT repeated or rewritten."
                ),
            },
            "sections_filled": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of sections that had new content added from the source.",
            },
        },
        "required": ["content", "sections_filled"],
    },
}

_SYSTEM = f"""You are the maintainer of a medical/scientific knowledge base wiki.
You write accurate, de-identified, well-structured markdown wiki pages.

{PHI_RULES}

Rules:
- Write ONLY what the source article explicitly states — no background knowledge, no speculation
- When enriching an existing section: append new bullet points or paragraphs; never rewrite or repeat what is already there
- When creating a new section: write it fully from the source
- Preserve all other page content exactly as-is
- Use [[Page Title]] wiki links for EVERY significant named entity: drugs, conditions, procedures, devices, biomarkers — even ones that don't have a page yet. Stub pages are auto-created from links you produce.
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
    hint_subtype: str = "",  # used when the target page doesn't exist yet
    missing_values: list[str] | None = None,  # specific data points to extract
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
    if missing_values:
        log.info("  missing_values (%d): %s", len(missing_values), missing_values)
    else:
        log.info("  missing_values: none passed — LLM will rely on section headings only")

    # ── Read the current page (may not exist yet) ─────────────────────────────
    page_full_path = kb.wiki_root / target_page_path
    existing_content = ""
    if page_full_path.exists():
        existing_content = page_full_path.read_text(encoding="utf-8")
        log.info("Existing page: %d chars", len(existing_content))
    else:
        log.info("Page does not exist yet — will create from scratch")

    # ── Idea A: Opportunistic low-quality section enrichment ──────────────────
    # Parse section_quality from the page's frontmatter. Any section with
    # score ≤ 2 or the 'insufficient_content' flag is a candidate for enrichment
    # even if it wasn't in the original gap's missing_sections list.
    # We add these as bonus enrichment targets so every article fetch also
    # improves weak neighbouring sections when the content is relevant.
    _LOW_QUALITY_THRESHOLD = 2
    opportunistic_sections: list[str] = []
    if existing_content:
        _sq = _parse_section_quality(existing_content)
        for _sec, _qa in _sq.items():
            if _sec in gap_sections:
                continue  # already in the gap list
            if _qa["score"] <= _LOW_QUALITY_THRESHOLD or "insufficient_content" in _qa.get("flags", []):
                opportunistic_sections.append(_sec)
        if opportunistic_sections:
            log.info(
                "Opportunistic enrichment: %d low-quality section(s) added: %s",
                len(opportunistic_sections), opportunistic_sections,
            )

    # Build per-section context showing current content so the LLM can add
    # only what is genuinely new rather than duplicating existing points.
    section_context_parts: list[str] = []
    if existing_content:
        # Primary sections from the gap file
        for s in gap_sections:
            body = _extract_section_body(existing_content, s)
            if body:
                section_context_parts.append(
                    f"## {s}\nCurrent content (DO NOT repeat this — only add new points):\n{body}"
                )
                log.info("Section '%s': has existing content (%d chars) — will enrich", s, len(body))
            else:
                section_context_parts.append(f"## {s}\n(not yet in page — create it)")
                log.info("Section '%s': missing — will create", s)
        # Opportunistic low-quality sections
        for s in opportunistic_sections:
            body = _extract_section_body(existing_content, s)
            section_context_parts.append(
                f"## {s} [LOW QUALITY — enrich only if source covers this]\n"
                f"Current content (add only what the source explicitly says — skip if not covered):\n{body}"
            )
            log.info("Section '%s' [opportunistic]: score ≤ 2, added as enrichment target", s)

    # Determine page subtype: prefer existing frontmatter, then caller hint, then "default"
    subtype = hint_subtype or "default"
    for line in existing_content.splitlines():
        if line.startswith("subtype:"):
            subtype = line.split(":", 1)[1].strip()
            break

    from .page_templates import template_block as _template_block
    template_note = (
        f"\nPAGE STRUCTURE — use ONLY these ## section headings for any NEW sections you add:\n"
        f"{_template_block(subtype)}\n"
    )

    existing_block = (
        f"\n\nCURRENT PAGE CONTENT (preserve all existing sections, only add the missing ones):\n"
        f"---BEGIN CURRENT PAGE---\n{existing_content}\n---END CURRENT PAGE---"
        if existing_content else
        "\n\nThis page does not exist yet. Create it with proper frontmatter and the requested sections."
    )

    section_context_block = "\n\n".join(section_context_parts) if section_context_parts else ""
    wiki_links = _wiki_link_context(kb.wiki_dir)

    missing_values_block = ""
    if missing_values:
        missing_values_block = (
            "\nSPECIFIC DATA POINTS TO EXTRACT — the gap was registered because these exact "
            "values were missing. Find them in the source article and write them into the "
            "appropriate section. If the article doesn't contain a value, omit it rather than "
            "fabricating it:\n"
            + "".join(f"  - {v}\n" for v in missing_values)
        )
        log.info("  missing_values block injected into prompt: %s", missing_values)
    else:
        log.info("  missing_values block: empty — no specific data points in gap file")

    prompt = f"""You are enriching specific sections of a wiki page with knowledge from a new source article.

Wiki page: {target_page_path}
Topic: {gap_title}
{missing_values_block}
SECTIONS TO ENRICH (only touch these — leave everything else exactly as-is):
{section_context_block}

{template_note}
{wiki_links}
{existing_block}

---SOURCE ARTICLE---
Title: {article_title}
Citation: {article_citation}

{article_text[:50_000]}
---END SOURCE ARTICLE---

Instructions:
- For each section above, read what is already there (if anything) and the source article
- Add ONLY information from the source that is NOT already captured in the current content
- Do NOT repeat, rephrase, or rewrite existing bullet points — append new ones only
- If the source adds nothing new to a section, leave that section exactly as-is
- If a section is marked "not yet in page", create it from scratch using the source
- Preserve ALL other sections in the page exactly — do not rewrite, reorder, or remove them
- Use [[Page Title]] wiki links for EVERY significant named entity you mention: drugs (e.g. [[Ondansetron]], [[Metoclopramide]]), conditions (e.g. [[CINV]], [[PONV]]), procedures, devices, biomarkers — even if that page does not exist yet. Stubs will be auto-created from your links.
- Update the `sources:` frontmatter list to include this article
- Return the COMPLETE page content — never truncate
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
    # Guard against the LLM duplicating sections despite instructions
    new_content = _dedup_sections(new_content)
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
            "existing_content=%d chars  new_content=%d chars  sections_filled=%s",
            net_added, MIN_PROSE_CHARS, len(existing_content), len(new_content), sections_filled,
        )
        # Even though the fill was rejected, check if any gap sections are
        # already present in the EXISTING page and close those now.
        from .ingest_pipeline import _parse_written_sections
        gap_path = kb.wiki_root / gap_file_path
        already_closed = []
        if gap_path.exists():
            existing_sections = _parse_written_sections(existing_content)
            if existing_sections:
                gap_existed = gap_path.exists()
                resolve_gap_sections(gap_file_path, list(existing_sections), kb)
                if gap_existed and not gap_path.exists():
                    already_closed.append(gap_file_path)
                    log.info("  gap fully resolved from existing content: %s", gap_file_path)
                elif gap_existed:
                    log.info("  gap partially resolved from existing content: %s", gap_file_path)
        cost = log_call("fill_sections", gap_title, total_in, total_out, model=MODEL, kb_name=kb.name)
        return {
            "summary":       f"Fill skipped for {gap_title} — insufficient content from source (net +{net_added} chars)",
            "files_written": [],
            "diffs":         [],
            "errors":        [f"Source did not cover topic adequately (net +{net_added} chars added)"],
            "knowledge_gaps": [],
            "gap_files_written": [],
            "gaps_closed":   already_closed,
            "input_tokens":  total_in,
            "output_tokens": total_out,
            "cost_usd":      cost,
            "model":         MODEL,
        }

    # ── Ensure scope is declared in frontmatter ───────────────────────────────
    try:
        from .quality_scorer import (
            parse_scope, update_scope_frontmatter, _default_scope, _parse_subtype,
        )
        if not parse_scope(new_content):
            page_subtype = _parse_subtype(new_content)
            page_title   = Path(target_page_path).stem.replace("-", " ").title()
            default_sc   = _default_scope(page_title, page_subtype)
            new_content  = update_scope_frontmatter(new_content, default_sc)
            log.info("Scope (default) injected: %s", default_sc[:80])
    except Exception as _sce:
        log.warning("Scope injection failed (non-fatal): %s", _sce)

    # ── Score and tag quality before writing ─────────────────────────────────
    try:
        from .quality_scorer import score_page, update_quality_frontmatter
        scores = score_page(new_content)
        if scores:
            new_content = update_quality_frontmatter(new_content, scores)
            log.info("Quality scores: %s", {k: v["score"] for k, v in scores.items()})
    except Exception as _qe:
        log.warning("Quality scoring failed (non-fatal): %s", _qe)
        scores = {}

    # ── Scope contamination check (post-fill) ────────────────────────────────
    try:
        from .quality_scorer import (
            check_scope, parse_scope as _ps, _default_scope as _ds,
            _parse_subtype as _pst, update_contamination_frontmatter,
        )
        page_title_sc   = Path(target_page_path).stem.replace("-", " ").title()
        scope_str       = _ps(new_content) or _ds(page_title_sc, _pst(new_content))
        existing_titles = [
            f.stem.replace("-", " ").title()
            for d in ("entities", "concepts")
            for f in sorted((kb.wiki_dir / d).glob("*.md"))
        ] if kb.wiki_dir.exists() else []
        sc_result = check_scope(page_title_sc, new_content, scope_str, existing_titles)
        if not sc_result["clean"]:
            new_content = update_contamination_frontmatter(new_content, sc_result["violations"])
            log.warning(
                "⚠ Scope contamination detected on %s: %d violation(s) → %s",
                target_page_path, len(sc_result["violations"]),
                [v.get("belongs_on") for v in sc_result["violations"]],
            )
    except Exception as _sce:
        log.warning("Scope contamination check failed (non-fatal): %s", _sce)

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

    # Create stub pages for any [[links]] that don't exist yet
    from ..services.ingest_pipeline import _create_missing_stubs
    _create_missing_stubs(new_content, kb)

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

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation": "gap_resolve",
        "kb": kb.name,
        "source": article_title,
        "files_written": [target_page_path],
        "gaps_opened": [],
        "gaps_closed": [gap_file_path] if sections_filled else [],
        "sections_filled": sections_filled,
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": cost,
        "file_diffs": {diff["path"]: {"diff": diff["diff"], "added": diff["added"], "removed": diff["removed"], "is_new": diff["is_new"]}},
    })

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
        "section_quality":   scores,
    }
