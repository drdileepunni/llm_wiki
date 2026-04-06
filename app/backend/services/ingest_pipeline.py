import anthropic
import json
from datetime import date
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Tool schema — the API enforces valid JSON and handles all escaping
WIKI_TOOL = {
    "name": "write_wiki_files",
    "description": "Write all wiki file operations resulting from ingesting a source.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary of what was found in the source and what was extracted."
            },
            "files": {
                "type": "array",
                "description": "All file operations to perform.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["write", "append", "update"],
                            "description": "write = overwrite entire file, append = add to end, update = insert after section header"
                        },
                        "path": {
                            "type": "string",
                            "description": "Path relative to wiki root, e.g. wiki/sources/foo.md"
                        },
                        "content": {
                            "type": "string",
                            "description": "Full file content (for write/append) or new line to insert (for update)"
                        },
                        "section": {
                            "type": "string",
                            "description": "Section header name for update ops, e.g. 'Sources'"
                        }
                    },
                    "required": ["op", "path", "content"]
                }
            }
        },
        "required": ["summary", "files"]
    }
}

INGEST_PROMPT = f"""You are ingesting a new source into the wiki. Today's date is {date.today().isoformat()}.

## CRITICAL RULES

0. **Strip all PHI before writing anything to the wiki.**
   This wiki is multi-access. Never write protected health information into any wiki page.
   For case reports or any source containing patient data, de-identify as follows:
   - Patient name → "the patient" or anonymised ID (e.g. "Patient A")
   - Specific dates → relative timing only (e.g. "Day 3 of admission")
   - Age (if exact) → age range (e.g. "mid-60s")
   - MRN, account numbers, insurance IDs → omit entirely
   - Provider names → role only (e.g. "the cardiologist")
   - Institution names → "the treating institution"
   - Geographic identifiers below state level → omit or generalise
   Keep: clinical findings, diagnoses, lab values, drug doses, imaging findings,
   treatment decisions, outcomes, and relative timelines. The knowledge stays; the
   patient identity does not.

1. **Always proceed. Never refuse. Never ask for permission.**
   - Ingest regardless of topic: clinical cases, news, research, notes, anything.
   - Never write "should I proceed", "out of scope", or "I need to confirm".
   - Every source contains knowledge worth capturing. Just ingest it.

2. **Extract ALL knowledge, regardless of domain.**
   - Named entities: diseases, drugs, people, organizations, procedures, devices, trials, biomarkers
   - Concepts and mechanisms: how things work, why things happen
   - Clinical findings: what was observed, measured, concluded
   - Practical takeaways: what changes in practice
   - Open questions: what remains unknown or debated

3. **Minimum file output per ingest:**
   - 1 source page (wiki/sources/[slug].md)
   - At least 3 entity or concept pages (wiki/entities/ or wiki/concepts/)
   - 1 append to wiki/log.md
   - 1 update to wiki/index.md (section: "Sources")

4. **Every wiki page must have frontmatter:**
   ```
   ---
   title: Page Title
   type: source | entity | concept | query
   tags: [tag1, tag2]
   created: {date.today().isoformat()}
   updated: {date.today().isoformat()}
   sources: [slug]
   ---
   ```

5. **Use [[wiki links]] for all cross-references between pages.**

Call the `write_wiki_files` tool with your summary and all file operations.
"""

def run_ingest(source_text: str, source_name: str, citation: str = "") -> dict:
    system_prompt = CLAUDE_MD.read_text()

    user_message = f"""Source to ingest: {source_name}
Citation: {citation}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---

{INGEST_PROMPT}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=system_prompt,
        tools=[WIKI_TOOL],
        tool_choice={"type": "any"},  # force tool use
        messages=[{"role": "user", "content": user_message}]
    )

    cost = log_call(
        operation="ingest",
        source_name=source_name,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    # Extract tool call result — API guarantees valid JSON
    tool_use_block = next(
        (b for b in response.content if b.type == "tool_use"),
        None
    )

    if not tool_use_block:
        return {
            "summary": "Error: Claude did not call the write_wiki_files tool.",
            "files_written": [],
            "errors": ["No tool_use block in response"],
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": cost,
        }

    args = tool_use_block.input  # already a dict — no JSON parsing needed
    summary = args.get("summary", "")
    files = args.get("files", [])

    files_written = []
    errors = []

    for file_op in files:
        try:
            execute_file_op(file_op)
            files_written.append(file_op["path"])
        except Exception as e:
            errors.append(f"Error writing {file_op.get('path', '?')}: {e}")

    return {
        "summary": summary,
        "files_written": files_written,
        "errors": errors,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": cost,
    }

def execute_file_op(op: dict):
    path = WIKI_ROOT / op["path"]
    path.parent.mkdir(parents=True, exist_ok=True)

    if op["op"] == "write":
        path.write_text(op["content"], encoding="utf-8")

    elif op["op"] == "append":
        with open(path, "a", encoding="utf-8") as f:
            f.write(op["content"])

    elif op["op"] == "update":
        if path.exists():
            text = path.read_text()
            section = op.get("section", "")
            new_line = op.get("content", "")
            if f"## {section}" in text:
                if f"## {section}\n(none yet)" in text:
                    text = text.replace(
                        f"## {section}\n(none yet)",
                        f"## {section}\n{new_line}"
                    )
                elif new_line not in text:
                    text = text.replace(
                        f"## {section}\n",
                        f"## {section}\n{new_line}\n",
                        1
                    )
            path.write_text(text)
