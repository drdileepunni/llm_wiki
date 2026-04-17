import logging
import difflib
from datetime import date
from ..config import MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
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
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Bullet points of what this file should contain"
                        }
                    },
                    "required": ["op", "path", "key_points"]
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
- For each entity/concept page, list key_points that cover the FULL clinical picture:
  definition, epidemiology, pathophysiology, clinical features, diagnosis, treatment/management, prognosis, complications — whatever applies.
  Do not limit key_points to only what the source explicitly mentions; include all clinically important topics for that entity.

Call plan_wiki_files with your summary and the list of files to create.
"""

WRITE_PROMPT = f"""You are writing a single wiki page. Today: {date.today().isoformat()}.

{PHI_RULES}

PAGE TYPE RULES:

**source pages** (wiki/sources/...):
  - Faithfully represent what the source says. Do not add outside information.
  - Sections: Citation, Abstract, Key Findings, Entities Mentioned, Concepts Mentioned, Open Questions.

**entity and concept pages** (wiki/entities/..., wiki/concepts/...):
  - Write a COMPREHENSIVE specialist reference on this topic using your full knowledge.
  - The source file provides context for WHICH entity/concept to write about and may contribute specific findings — but the page must cover the complete clinical/scientific picture regardless of how much the source says.
  - MANDATORY sections for medical entities/concepts (include all that apply):
    Definition | Epidemiology | Pathophysiology | Clinical Presentation | Diagnosis |
    Treatment & Management | Special Populations | Complications | Prognosis | Key References
  - Include specific values: drug names + doses + durations, lab cut-offs, scoring thresholds,
    sensitivity/specificity of tests, resistance rates, guideline recommendations.
  - Longer is better than shorter. A thin page is a failure. Aim for ≥600 words of substantive content.

GENERAL:
- DO NOT summarise. Write at depth. Use ## sections to organise, not to replace content.
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

def compute_diff(path: str, new_content: str, op: str) -> dict:
    full_path = WIKI_ROOT / path
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

def execute_file_op(path: str, content: str, op: str, section: str = ""):
    full_path = WIKI_ROOT / path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if op == "write":
        full_path.write_text(content, encoding="utf-8")

    elif op == "append":
        with open(full_path, "a", encoding="utf-8") as f:
            f.write("\n" + content)

    elif op == "update":
        # LLM has already merged existing + new content — just write it
        full_path.write_text(content, encoding="utf-8")

# ── Main pipeline ───────────────────────────────────────────────────────────────

def run_ingest(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",   # local file path (or GCP URI later)
) -> dict:
    llm           = get_llm_client()
    system_prompt = CLAUDE_MD.read_text()
    source_chars  = len(source_text)
    log.info("=== INGEST START  source=%r  chars=%d  model=%s ===", source_name, source_chars, MODEL)

    source_ref = source_path or source_name   # what gets stored in frontmatter

    # ── Step 1: Plan ──────────────────────────────────────────────────────────
    log.info("Step 1: planning  max_tokens=8000")
    plan_response = llm.create_message(
        messages=[{
            "role": "user",
            "content": f"""Source: {source_name}
Citation: {citation}
Source file: {source_ref}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---

{PLAN_PROMPT}"""
        }],
        tools=[PLAN_TOOL],
        system=system_prompt,
        max_tokens=8000,
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

    plan      = plan_block.input
    summary   = plan.get("summary", "")
    file_plan = plan.get("files", [])
    log.info("Plan received: %d files to write  summary=%r", len(file_plan), summary[:120])
    for i, f in enumerate(file_plan):
        log.info("  [%d] %s  op=%s  points=%d", i + 1, f.get("path"), f.get("op"), len(f.get("key_points", [])))

    # ── Step 2: Write each file individually ──────────────────────────────────
    files_written = []
    diffs         = []
    errors        = []
    total_in      = plan_response.usage.input_tokens
    total_out     = plan_response.usage.output_tokens

    for idx, file_spec in enumerate(file_plan):
        path     = file_spec.get("path", "")
        op       = file_spec.get("op", "write")
        section  = file_spec.get("section", "")
        points   = file_spec.get("key_points", [])

        log.info("Step 2 [%d/%d]: writing %s  op=%s", idx + 1, len(file_plan), path, op)

        try:
            full_path_for_op = WIKI_ROOT / path
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

REMINDER for entity/concept pages: use the source as a signal for what to write about,
but write a full reference-quality page using your complete knowledge of the topic.
The source may be brief — your page must not be.

Include `source_file: "{source_ref}"` in the frontmatter.
Return the COMPLETE file content — never truncate."""
                }],
                tools=[WRITE_TOOL],
                system=system_prompt,
                max_tokens=8000,
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
            diff    = compute_diff(path, content, op)
            execute_file_op(path, content, op, section)
            files_written.append(path)
            diffs.append(diff)
            log.info("  ✓ Written: %s  (+%d/-%d lines)", path, diff["added"], diff["removed"])

        except Exception as e:
            log.exception("  Exception writing %s: %s", path, e)
            errors.append(f"Error writing {path}: {e}")

    cost = log_call("ingest", source_name, total_in, total_out, model=MODEL)

    log.info(
        "=== INGEST END  files_written=%d  errors=%d  total_in=%d  total_out=%d  cost=$%.4f ===",
        len(files_written), len(errors), total_in, total_out, cost,
    )
    if errors:
        for err in errors:
            log.warning("  error: %s", err)

    return {
        "summary":       summary,
        "files_written": files_written,
        "diffs":         diffs,
        "errors":        errors,
        "input_tokens":  total_in,
        "output_tokens": total_out,
        "cost_usd":      cost,
        "model":         MODEL,
    }


# ── Chunked entry point ──────────────────────────────────────────────────────

CHUNK_SIZE = 30_000   # chars (~2-3 pages); gives ~10 chunks for a typical 20-page doc


def run_ingest_chunked(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",
) -> dict:
    """
    Ingest a source, automatically splitting large documents into sequential
    chunks so each fits comfortably within the model's context window.

    Each chunk after the first receives a one-paragraph summary of what prior
    chunks already covered, so the planner knows which pages were already
    created and can issue update ops rather than duplicating work.

    Small documents (≤ CHUNK_SIZE chars) pass straight through to run_ingest().
    """
    if len(source_text) <= CHUNK_SIZE:
        return run_ingest(source_text, source_name, citation, source_path)

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
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "model": MODEL,
    }
    seen_files: set[str] = set()
    running_summary = ""

    for i, chunk in enumerate(chunks, 1):
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

    log.info(
        "=== CHUNKED INGEST COMPLETE  total_files=%d  total_errors=%d  total_cost=$%.4f ===",
        len(combined["files_written"]), len(combined["errors"]), combined["cost_usd"],
    )
    return combined
