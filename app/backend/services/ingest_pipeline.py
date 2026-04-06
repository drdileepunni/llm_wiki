import anthropic
import difflib
from datetime import date
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

Call plan_wiki_files with your summary and the list of files to create.
For each file include the key points to cover — full content will be written separately.
"""

WRITE_PROMPT = f"""You are writing a single wiki page. Today: {date.today().isoformat()}.

{PHI_RULES}

Write complete, well-structured markdown with frontmatter:
---
title: Page Title
type: source | entity | concept | query
tags: [tag1, tag2]
created: {date.today().isoformat()}
updated: {date.today().isoformat()}
sources: [slug]
---

Use [[wiki links]] for all cross-references.
If flagging a contradiction, include a ## Contradictions section.
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
            f.write(content)

    elif op == "update":
        if full_path.exists():
            text = full_path.read_text()
            if f"## {section}" in text:
                if f"## {section}\n(none yet)" in text:
                    text = text.replace(f"## {section}\n(none yet)", f"## {section}\n{content}")
                elif content not in text:
                    text = text.replace(f"## {section}\n", f"## {section}\n{content}\n", 1)
            full_path.write_text(text)

# ── Main pipeline ───────────────────────────────────────────────────────────────

def run_ingest(source_text: str, source_name: str, citation: str = "") -> dict:
    system_prompt = CLAUDE_MD.read_text()

    # ── Step 1: Plan ──────────────────────────────────────────────────────────
    plan_response = client.messages.create(
        model=MODEL,
        max_tokens=2000,   # plan is lightweight — just paths + bullet points
        system=system_prompt,
        tools=[PLAN_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": f"""Source: {source_name}
Citation: {citation}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---

{PLAN_PROMPT}"""
        }]
    )

    log_call("ingest", f"[plan] {source_name}",
             plan_response.usage.input_tokens, plan_response.usage.output_tokens)

    plan_block = next((b for b in plan_response.content if b.type == "tool_use"), None)
    if not plan_block:
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

    # ── Step 2: Write each file individually ──────────────────────────────────
    files_written = []
    diffs         = []
    errors        = []
    total_in      = plan_response.usage.input_tokens
    total_out     = plan_response.usage.output_tokens

    for file_spec in file_plan:
        path     = file_spec.get("path", "")
        op       = file_spec.get("op", "write")
        section  = file_spec.get("section", "")
        points   = file_spec.get("key_points", [])

        try:
            write_response = client.messages.create(
                model=MODEL,
                max_tokens=4000,   # ample for a single wiki page
                system=system_prompt,
                tools=[WRITE_TOOL],
                tool_choice={"type": "any"},
                messages=[{
                    "role": "user",
                    "content": f"""{WRITE_PROMPT}

Write the wiki file at: {path}
Operation: {op}{f' (section: {section})' if section else ''}

Key points to cover:
{chr(10).join(f'- {p}' for p in points)}

Source this came from: {source_name}
Citation: {citation}"""
                }]
            )

            total_in  += write_response.usage.input_tokens
            total_out += write_response.usage.output_tokens

            write_block = next(
                (b for b in write_response.content if b.type == "tool_use"), None
            )

            if not write_block:
                errors.append(f"No content returned for {path}")
                continue

            content = write_block.input.get("content", "")
            diff    = compute_diff(path, content, op)
            execute_file_op(path, content, op, section)
            files_written.append(path)
            diffs.append(diff)

        except Exception as e:
            errors.append(f"Error writing {path}: {e}")

    cost = log_call("ingest", source_name, total_in, total_out)

    return {
        "summary":       summary,
        "files_written": files_written,
        "diffs":         diffs,
        "errors":        errors,
        "input_tokens":  total_in,
        "output_tokens": total_out,
        "cost_usd":      cost,
    }
