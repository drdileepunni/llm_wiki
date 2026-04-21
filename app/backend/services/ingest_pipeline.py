import logging
import difflib
from datetime import date
from pathlib import Path
from ..config import MODEL, KBConfig, _default_kb
from ..cancellation import shutdown_event
from .token_tracker import log_call
from .llm_client import get_llm_client

log = logging.getLogger("wiki.ingest_pipeline")

# ── Tool definitions ────────────────────────────────────────────────────────────

# Step 1: planning tool — lightweight, just decides what files to create
PLAN_TOOL = {
    "name": "plan_wiki_files",
    "description": "Plan all wiki file operations for this ingest. Return the list of files to create/update with their key content points. Do NOT write full content here.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary of what was found in the source."
            },
            "files": {
                "type": "array",
                "description": "Plan of all files to write.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op":      {"type": "string", "enum": ["write", "append", "update"]},
                        "path":    {"type": "string", "description": "e.g. wiki/sources/foo.md"},
                        "section": {"type": "string", "description": "For update ops only"},
                        "key_points": {
                            "type": "string",
                            "description": "Newline-separated bullet points of what THIS SOURCE explicitly says about this file's topic. Do not include background knowledge."
                        }
                    },
                    "required": ["op", "path", "key_points"]
                }
            },
            "knowledge_gaps": {
                "type": "array",
                "description": "Standard knowledge areas NOT addressed by this source. List per entity/concept.",
                "items": {
                    "type": "object",
                    "properties": {
                        "page": {
                            "type": "string",
                            "description": "The entity/concept page path (e.g. wiki/entities/furosemide.md)"
                        },
                        "missing_sections": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Standard sections this source does not cover (e.g. ['Epidemiology', 'Dosing', 'Adverse effects'])"
                        }
                    },
                    "required": ["page", "missing_sections"]
                }
            }
        },
        "required": ["summary", "files"]
    }
}

# Step 2: write tool — one file at a time
WRITE_TOOL = {
    "name": "write_wiki_file",
    "description": "Write the full markdown content for a single wiki file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full markdown content of the file including frontmatter."
            }
        },
        "required": ["content"]
    }
}

# ── Prompts ─────────────────────────────────────────────────────────────────────

PHI_RULES = """
PRIVACY — Strip all PHI before writing to wiki:
- Patient name → anonymised ID (e.g. "Patient A")
- Exact dates → relative timing ("Day 3 of admission")
- Exact age → age range ("mid-60s")
- MRN / account numbers → omit
- Provider names → role ("the cardiologist")
- Institution names → "the treating institution"
- Geographic details below state level → omit
Keep: clinical findings, diagnoses, lab values, drug doses, treatment decisions, outcomes.
"""

PLAN_PROMPT = f"""You are ingesting a source into a multi-access knowledge base wiki. Today: {date.today().isoformat()}.

{PHI_RULES}

RULES:
- Always proceed. Never refuse. Ingest regardless of topic.
- Minimum plan: 1 source page + at least 3 entity/concept pages + log.md append + index.md update.
- Flag contradictions with existing wiki content explicitly in your plan.
- Use [[wiki links]] for cross-references.
- For each entity/concept page, key_points MUST contain ONLY information explicitly stated in this source.
  Do NOT add background knowledge, textbook facts, or anything not in the source text.
- For standard sections that apply to an entity/concept (e.g. Epidemiology, Pathophysiology, Treatment,
  Dosing, Prognosis) but are NOT addressed by this source, list them in knowledge_gaps instead.

KNOWLEDGE GAP AWARENESS:
- Existing gap files (wiki/gaps/*.md) will be provided below if any exist.
- Use them to understand what's already known to be missing — so you can prioritise those areas
  when planning entity/concept pages for this source.
- Do NOT include wiki/gaps/ files in your file plan. Gap files are managed automatically.

Call plan_wiki_files with your summary, files, and knowledge_gaps.
"""

WRITE_PROMPT = f"""You are writing a single wiki page. Today: {date.today().isoformat()}.

{PHI_RULES}

PAGE TYPE RULES:

**source pages** (wiki/sources/...):
  - Faithfully represent what the source says. Do not add outside information.
  - Sections: Citation, Abstract, Key Findings, Entities Mentioned, Concepts Mentioned, Open Questions.

**entity and concept pages** (wiki/entities/..., wiki/concepts/...):
  - Write ONLY what this source explicitly states about the entity/concept.
  - DO NOT add background knowledge, textbook content, or anything not in the source text.
  - Use the standard section headings (Definition, Epidemiology, Pathophysiology, Clinical Presentation,
    Diagnosis, Treatment & Management, Complications, Prognosis) but ONLY populate a section if the
    source addresses it. For sections the source does not cover, omit them entirely — they will be
    tracked as knowledge gaps in the plan step.
  - Specific values (doses, lab cut-offs, thresholds) are welcome IF they come from the source.

GENERAL:
- DO NOT summarise. Write only source-derived content. Use ## sections to organise.
- Use [[wiki links]] for all cross-references.
- If flagging a contradiction, include a ## Contradictions section.

Write complete markdown with frontmatter:
---
title: Page Title
type: source | entity | concept | query
tags: [tag1, tag2]
created: {date.today().isoformat()}
updated: {date.today().isoformat()}
sources: [slug]
---

Call write_wiki_file with the complete file content.
"""

# ── Diff helper ─────────────────────────────────────────────────────────────────

def compute_diff(path: str, new_content: str, op: str, kb: "KBConfig") -> dict:
    full_path = kb.wiki_root / path
    is_new = not full_path.exists()
    old_lines = [] if is_new else full_path.read_text(encoding="utf-8").splitlines()

    if op == "append":
        new_lines = old_lines + new_content.splitlines()
    else:
        new_lines = new_content.splitlines()

    added   = sum(1 for l in new_lines if l not in old_lines)
    removed = sum(1 for l in old_lines if l not in new_lines)

    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    return {"path": path, "op": op, "is_new": is_new,
            "added": added, "removed": removed, "diff": diff_lines}

# ── File executor ───────────────────────────────────────────────────────────────

def execute_file_op(path: str, content: str, op: str, kb: "KBConfig", section: str = ""):
    full_path = kb.wiki_root / path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if op == "write":
        full_path.write_text(content, encoding="utf-8")

    elif op == "append":
        with open(full_path, "a", encoding="utf-8") as f:
            f.write("\n" + content)

    elif op == "update":
        # LLM has already merged existing + new content — just write it
        full_path.write_text(content, encoding="utf-8")

# ── Gap file helpers ────────────────────────────────────────────────────────────

def _parse_gap_missing(text: str) -> set[str]:
    """Extract missing section names from an existing gap file."""
    sections: set[str] = set()
    in_block = False
    for line in text.splitlines():
        if line.strip() == "## Missing Sections":
            in_block = True
            continue
        if in_block:
            if line.startswith("##"):
                break
            if line.startswith("- "):
                sections.add(line[2:].strip())
    return sections


def write_gap_files(knowledge_gaps: list, kb: "KBConfig", source_name: str) -> list[str]:
    """Write or merge gap files into wiki/gaps/ from the plan's knowledge_gaps output."""
    gaps_dir = kb.wiki_dir / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written: list[str] = []

    for gap in knowledge_gaps:
        page = gap.get("page", "")
        raw_ms  = gap.get("missing_sections", "")
        missing = set(
            s.strip("- •").strip()
            for s in (raw_ms.splitlines() if isinstance(raw_ms, str) else raw_ms)
            if s.strip()
        )
        if not page or not missing:
            continue

        stem = Path(page).stem
        gap_path = gaps_dir / f"{stem}.md"
        created = today

        existing_missing: set[str] = set()
        if gap_path.exists():
            existing_text = gap_path.read_text(encoding="utf-8")
            existing_missing = _parse_gap_missing(existing_text)
            for line in existing_text.splitlines():
                if line.startswith("created:"):
                    created = line.split(":", 1)[1].strip().strip('"')
                    break

        merged = sorted(existing_missing | missing)
        if not merged:
            continue

        title = stem.replace("-", " ").title()
        rel_gap = f"wiki/gaps/{stem}.md"
        content = (
            f"---\n"
            f'title: "Knowledge Gap \u2014 {title}"\n'
            f"type: gap\n"
            f"referenced_page: {page}\n"
            f"tags: [gap]\n"
            f"created: {created}\n"
            f"updated: {today}\n"
            f"---\n\n"
            f"## Missing Sections\n\n"
            + "\n".join(f"- {s}" for s in merged)
            + f"\n\n## Suggested Sources\n\n"
            f"- Ingest a reference document, guideline, or monograph that covers the above sections for: **{title}**\n"
            f"\n_Last updated by ingest of: {source_name}_\n"
        )
        gap_path.write_text(content, encoding="utf-8")
        written.append(rel_gap)
        log.info("  gap file written: %s  (%d missing sections)", rel_gap, len(merged))

    return written


def resolve_gap_sections(gap_file_rel: str, resolved_sections: list[str], kb: "KBConfig") -> None:
    """Remove resolved sections from a gap file; delete the file if nothing remains."""
    gap_path = kb.wiki_root / gap_file_rel
    if not gap_path.exists():
        return
    existing = _parse_gap_missing(gap_path.read_text(encoding="utf-8"))
    resolved_set = {s.removeprefix("RESOLVED: ").strip() for s in resolved_sections}
    remaining = existing - resolved_set
    if not remaining:
        gap_path.unlink()
        log.info("  gap file fully resolved and deleted: %s", gap_file_rel)
    else:
        # Rewrite with remaining sections only
        text = gap_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        new_lines = []
        in_missing = False
        for line in lines:
            if line.strip() == "## Missing Sections":
                in_missing = True
                new_lines.append(line)
                new_lines.extend(f"- {s}" for s in sorted(remaining))
                continue
            if in_missing:
                if line.startswith("##"):
                    in_missing = False
                    new_lines.append(line)
                # skip old "- " lines inside the block
                elif not line.startswith("- "):
                    new_lines.append(line)
            else:
                new_lines.append(line)
        gap_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log.info("  gap file updated: %s  (%d sections remain)", gap_file_rel, len(remaining))


def _parse_written_sections(content: str) -> set[str]:
    """Return the set of ## section headings present in written markdown content."""
    sections: set[str] = set()
    for line in content.splitlines():
        if line.startswith("## "):
            sections.add(line[3:].strip())
    return sections


def load_existing_gaps_context(kb: "KBConfig", max_files: int = 30) -> str:
    """Read wiki/gaps/ and return a context block for the plan prompt."""
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return ""
    gap_files = sorted(gaps_dir.glob("*.md"))[:max_files]
    if not gap_files:
        return ""
    blocks = []
    for f in gap_files:
        rel = f"wiki/gaps/{f.name}"
        blocks.append(f"--- {rel} ---\n{f.read_text(encoding='utf-8')}")
    return (
        "\n\nEXISTING KNOWLEDGE GAPS (wiki/gaps/):\n"
        + "\n\n".join(blocks)
        + "\n\nIf this source resolves any listed missing sections, include those gap files as "
          "update ops and prefix the resolved section names with 'RESOLVED: ' in key_points.\n"
    )


# ── Main pipeline ───────────────────────────────────────────────────────────────

def run_ingest(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",   # local file path (or GCP URI later)
    kb: "KBConfig | None" = None,
) -> dict:
    if kb is None:
        kb = _default_kb()
    llm           = get_llm_client()
    system_prompt = kb.claude_md.read_text()
    source_chars  = len(source_text)
    log.info("=== INGEST START  source=%r  chars=%d  model=%s ===", source_name, source_chars, MODEL)

    source_ref = source_path or source_name   # what gets stored in frontmatter

    # ── Step 1: Plan ──────────────────────────────────────────────────────────
    existing_gaps = load_existing_gaps_context(kb)
    log.info("Step 1: planning  max_tokens=16000  existing_gap_files=%s",
             "yes" if existing_gaps else "none")
    plan_response = llm.create_message(
        messages=[{
            "role": "user",
            "content": f"""Source: {source_name}
Citation: {citation}
Source file: {source_ref}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---
{existing_gaps}
{PLAN_PROMPT}"""
        }],
        tools=[PLAN_TOOL],
        system=system_prompt,
        max_tokens=16000,
    )

    log.info(
        "Step 1 response: stop_reason=%s  in=%d  out=%d",
        plan_response.stop_reason,
        plan_response.usage.input_tokens,
        plan_response.usage.output_tokens,
    )
    if plan_response.stop_reason == "max_tokens":
        log.warning("Step 1 HIT TOKEN CEILING — plan is truncated, tool_use block will be missing")
    elif plan_response.stop_reason in ("blocked", "malformed"):
        log.error("Step 1 failed: stop_reason=%s", plan_response.stop_reason)
        return {
            "summary": f"Error: model returned stop_reason={plan_response.stop_reason} in planning step.",
            "files_written": [], "diffs": [],
            "errors": [f"Model stop_reason={plan_response.stop_reason} in step 1"],
            "input_tokens": plan_response.usage.input_tokens,
            "output_tokens": plan_response.usage.output_tokens,
            "cost_usd": 0,
        }

    log_call("ingest", f"[plan] {source_name}",
             plan_response.usage.input_tokens, plan_response.usage.output_tokens,
             model=MODEL)

    plan_block = next((b for b in plan_response.content if b.type == "tool_use"), None)
    if not plan_block:
        log.error("Step 1 returned no tool_use block. Content blocks: %s",
                  [b.type for b in plan_response.content])
        return {
            "summary": "Error: planning step did not return a tool call.",
            "files_written": [], "diffs": [],
            "errors": ["No plan_wiki_files tool call in step 1 response"],
            "input_tokens": plan_response.usage.input_tokens,
            "output_tokens": plan_response.usage.output_tokens,
            "cost_usd": 0,
        }

    plan           = plan_block.input
    summary        = plan.get("summary", "")
    file_plan      = plan.get("files", [])
    knowledge_gaps = plan.get("knowledge_gaps", [])
    log.info("Plan received: %d files to write  summary=%r", len(file_plan), summary[:120])
    for i, f in enumerate(file_plan):
        raw_kp = f.get("key_points", "")
        pt_count = len([l for l in (raw_kp.splitlines() if isinstance(raw_kp, str) else raw_kp) if l.strip()])
        log.info("  [%d] %s  op=%s  points=%d", i + 1, f.get("path"), f.get("op"), pt_count)

    # ── Step 2: Write each file individually ──────────────────────────────────
    files_written    = []
    diffs            = []
    errors           = []
    gap_files_written: list[str] = []
    total_in         = plan_response.usage.input_tokens
    total_out        = plan_response.usage.output_tokens

    for idx, file_spec in enumerate(file_plan):
        path     = file_spec.get("path", "")
        op       = file_spec.get("op", "write")
        section  = file_spec.get("section", "")
        raw_pts  = file_spec.get("key_points", "")
        points   = [p.strip("- •").strip() for p in (raw_pts.splitlines() if isinstance(raw_pts, str) else raw_pts) if p.strip()]

        # Gap files are managed purely in Python — never let the LLM write them
        if path.startswith("wiki/gaps/"):
            log.info("Step 2 [%d/%d]: skipping gap file %s (managed by pipeline)", idx + 1, len(file_plan), path)
            continue

        if shutdown_event.is_set():
            log.info("Step 2 [%d/%d]: shutdown requested — stopping write loop", idx + 1, len(file_plan))
            errors.append("Ingest cancelled: server shutting down")
            break

        log.info("Step 2 [%d/%d]: writing %s  op=%s", idx + 1, len(file_plan), path, op)

        try:
            full_path_for_op = kb.wiki_root / path
            existing_content = ""
            if op in ("update", "append") and full_path_for_op.exists():
                existing_content = full_path_for_op.read_text(encoding="utf-8")

            existing_block = (
                f"\n\nEXISTING FILE CONTENT (merge/update this — return the complete updated file):\n"
                f"---BEGIN EXISTING---\n{existing_content}\n---END EXISTING---"
                if existing_content else ""
            )

            write_response = llm.create_message(
                messages=[{
                    "role": "user",
                    "content": f"""{WRITE_PROMPT}

Write the wiki file at: {path}
Operation: {op}{f' (section: {section})' if section else ''}
{existing_block}

---SOURCE TEXT---
{source_text[:40000]}
---END SOURCE TEXT---

Key points the plan identified (use as a minimum checklist, not a ceiling — add more):
{chr(10).join(f'- {p}' for p in points)}

Source: {source_name}
Citation: {citation}
Source file path: {source_ref}

IMPORTANT: Write ONLY what this source explicitly states. Do NOT add background knowledge.
A page that only covers what the source says is correct — gaps are tracked separately.

Include `source_file: "{source_ref}"` in the frontmatter.
Return the COMPLETE file content — never truncate."""
                }],
                tools=[WRITE_TOOL],
                system=system_prompt,
                max_tokens=16000,
            )

            log.info(
                "  write response: stop_reason=%s  in=%d  out=%d",
                write_response.stop_reason,
                write_response.usage.input_tokens,
                write_response.usage.output_tokens,
            )
            if write_response.stop_reason == "max_tokens":
                log.warning("  WRITE HIT TOKEN CEILING for %s — content may be truncated", path)

            total_in  += write_response.usage.input_tokens
            total_out += write_response.usage.output_tokens

            write_block = next(
                (b for b in write_response.content if b.type == "tool_use"), None
            )

            if not write_block:
                log.error("  No tool_use block for %s. Block types: %s",
                          path, [b.type for b in write_response.content])
                errors.append(f"No content returned for {path}")
                continue

            content = write_block.input.get("content", "")
            log.info("  Content length: %d chars → executing op=%s", len(content), op)
            diff    = compute_diff(path, content, op, kb)
            execute_file_op(path, content, op, kb, section)
            files_written.append(path)
            diffs.append(diff)
            log.info("  ✓ Written: %s  (+%d/-%d lines)", path, diff["added"], diff["removed"])

            # Resolve gap file for this entity/concept page in pure Python
            p = Path(path)
            if len(p.parts) >= 2 and p.parts[-2] in ("entities", "concepts"):
                gap_rel = f"wiki/gaps/{p.stem}.md"
                if (kb.wiki_root / gap_rel).exists():
                    written_sections = _parse_written_sections(content)
                    resolve_gap_sections(gap_rel, list(written_sections), kb)
                    log.info("  gap resolution: checked %s against %d written sections", gap_rel, len(written_sections))

        except Exception as e:
            log.exception("  Exception writing %s: %s", path, e)
            errors.append(f"Error writing {path}: {e}")

    # ── Write new gap files for sections not covered by this source ───────────
    gap_files_written = write_gap_files(knowledge_gaps, kb, source_name)

    cost = log_call("ingest", source_name, total_in, total_out, model=MODEL, kb_name=kb.name)

    log.info(
        "=== INGEST END  files_written=%d  gap_files=%d  errors=%d  total_in=%d  total_out=%d  cost=$%.4f ===",
        len(files_written), len(gap_files_written), len(errors), total_in, total_out, cost,
    )
    if errors:
        for err in errors:
            log.warning("  error: %s", err)

    return {
        "summary":          summary,
        "files_written":    files_written,
        "diffs":            diffs,
        "errors":           errors,
        "knowledge_gaps":   knowledge_gaps,
        "gap_files_written": gap_files_written,
        "input_tokens":     total_in,
        "output_tokens":    total_out,
        "cost_usd":         cost,
        "model":            MODEL,
    }


# ── Chunked entry point ──────────────────────────────────────────────────────

CHUNK_SIZE = 30_000   # chars (~2-3 pages); gives ~10 chunks for a typical 20-page doc


def run_ingest_chunked(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",
    kb: "KBConfig | None" = None,
) -> dict:
    """
    Ingest a source, automatically splitting large documents into sequential
    chunks so each fits comfortably within the model's context window.

    Each chunk after the first receives a one-paragraph summary of what prior
    chunks already covered, so the planner knows which pages were already
    created and can issue update ops rather than duplicating work.

    Small documents (≤ CHUNK_SIZE chars) pass straight through to run_ingest().
    """
    if kb is None:
        kb = _default_kb()
    if len(source_text) <= CHUNK_SIZE:
        return run_ingest(source_text, source_name, citation, source_path, kb)

    # Split on whitespace boundaries near each CHUNK_SIZE boundary
    chunks: list[str] = []
    start = 0
    while start < len(source_text):
        end = start + CHUNK_SIZE
        if end < len(source_text):
            # Walk back to the nearest newline so we don't cut mid-sentence
            boundary = source_text.rfind("\n", start, end)
            if boundary > start:
                end = boundary
        chunks.append(source_text[start:end])
        start = end

    total_chunks = len(chunks)
    log.info(
        "=== CHUNKED INGEST  source=%r  total_chars=%d  chunks=%d  chunk_size=%d ===",
        source_name, len(source_text), total_chunks, CHUNK_SIZE,
    )

    combined: dict = {
        "summary": "",
        "files_written": [],
        "diffs": [],
        "errors": [],
        "knowledge_gaps": [],
        "gap_files_written": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "model": MODEL,
    }
    seen_files: set[str] = set()
    seen_gap_files: set[str] = set()
    # Deduplicate knowledge gaps across chunks by page path
    gaps_by_page: dict[str, set[str]] = {}
    running_summary = ""

    for i, chunk in enumerate(chunks, 1):
        if shutdown_event.is_set():
            log.info("--- Chunk %d/%d: shutdown requested — stopping chunked ingest ---", i, total_chunks)
            combined["errors"].append(f"Ingest cancelled at chunk {i}/{total_chunks}: server shutting down")
            break

        log.info("--- Chunk %d/%d  chars=%d ---", i, total_chunks, len(chunk))

        # Prepend prior-chunk context so the planner can issue update ops
        if running_summary:
            context_header = (
                f"[PRIOR CHUNKS ALREADY INGESTED — do not re-create these pages, "
                f"use op=update to add new information from this chunk]\n"
                f"Summary of prior chunks: {running_summary}\n\n"
                f"--- CHUNK {i}/{total_chunks} STARTS HERE ---\n\n"
            )
            chunk_text = context_header + chunk
        else:
            chunk_text = f"--- CHUNK {i}/{total_chunks} STARTS HERE ---\n\n" + chunk

        result = run_ingest(
            source_text=chunk_text,
            source_name=f"{source_name} [part {i}/{total_chunks}]",
            citation=citation,
            source_path=source_path,
            kb=kb,
        )

        # Accumulate totals
        combined["input_tokens"]  += result.get("input_tokens", 0)
        combined["output_tokens"] += result.get("output_tokens", 0)
        combined["cost_usd"]      += result.get("cost_usd", 0.0)
        combined["errors"].extend(result.get("errors", []))

        # Merge diffs — keep only the latest diff per path
        for diff in result.get("diffs", []):
            combined["diffs"].append(diff)

        # Track unique files written
        for f in result.get("files_written", []):
            if f not in seen_files:
                seen_files.add(f)
                combined["files_written"].append(f)

        # Track unique gap files written
        for gf in result.get("gap_files_written", []):
            if gf not in seen_gap_files:
                seen_gap_files.add(gf)
                combined["gap_files_written"].append(gf)

        # Merge knowledge gaps — union missing sections per page
        for gap in result.get("knowledge_gaps", []):
            page = gap.get("page", "")
            if page:
                gaps_by_page.setdefault(page, set()).update(gap.get("missing_sections", []))

        # Set overall summary from first chunk; append subsequent summaries
        chunk_summary = result.get("summary", "")
        if chunk_summary:
            if not combined["summary"]:
                combined["summary"] = chunk_summary
            running_summary = (running_summary + " " + chunk_summary).strip()

        log.info(
            "--- Chunk %d/%d done  files_written=%d  errors=%d ---",
            i, total_chunks, len(result.get("files_written", [])), len(result.get("errors", [])),
        )

    combined["knowledge_gaps"] = [
        {"page": page, "missing_sections": sorted(sections)}
        for page, sections in sorted(gaps_by_page.items())
    ]
    log.info(
        "=== CHUNKED INGEST COMPLETE  total_files=%d  total_errors=%d  total_cost=$%.4f  gaps=%d ===",
        len(combined["files_written"]), len(combined["errors"]), combined["cost_usd"],
        len(combined["knowledge_gaps"]),
    )
    return combined
