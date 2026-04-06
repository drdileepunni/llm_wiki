import anthropic
import json
from pathlib import Path
from datetime import date
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

INGEST_INSTRUCTIONS = f"""
You are ingesting a new source into the wiki. Today's date is {date.today().isoformat()}.

## CRITICAL RULES — READ FIRST

1. **Always proceed. Never refuse. Never ask for permission.**
   - It does not matter if the source is a clinical case report, a news article, a research paper,
     a personal note, or anything else.
   - It does not matter if the topic is outside any previously stated domain.
   - Your job is to extract knowledge from whatever is given and file it into the wiki.
   - If you find yourself writing "I need to stop", "should I proceed", or "this is out of scope" —
     stop and instead just ingest it.

2. **Extract ALL knowledge, regardless of topic.**
   - Named entities: diseases, drugs, people, organizations, procedures, devices, trials, biomarkers
   - Concepts and mechanisms: how things work, why things happen
   - Clinical findings: what was observed, measured, concluded
   - Practical takeaways: what changes in practice
   - Open questions: what remains unknown or debated

3. **Every source creates multiple pages.**
   - Always write: 1 source page + at least 3 entity or concept pages + log.md update + index.md update
   - A clinical case report creates pages for the patient's conditions, the drugs used, the procedures performed, the clinical concepts at play
   - An opinion article creates concept pages for the ideas it argues
   - A news article creates entity pages for the people/organizations/events mentioned

## Output Format

Write a brief summary (2-3 sentences) of what was ingested, then output file operations.

After the summary, output a line containing exactly: ===FILE_OPS===

Then output file operations, one JSON object per line (no line breaks inside any JSON object):

{{"op": "write", "path": "wiki/sources/slug.md", "content": "...full markdown content..."}}
{{"op": "write", "path": "wiki/entities/entity-name.md", "content": "..."}}
{{"op": "write", "path": "wiki/concepts/concept-name.md", "content": "..."}}
{{"op": "append", "path": "wiki/log.md", "content": "\\n## {date.today().isoformat()} ingest | Source Title\\nBrief description of what was ingested and pages created."}}
{{"op": "update", "path": "wiki/index.md", "section": "Sources", "content": "- [[Source Title]] — one line summary"}}

## File op rules
- "write" op: overwrites entire file (use for new pages or full rewrites)
- "append" op: adds to end of file (use for log.md)
- "update" op: inserts after a section header in an existing file (use for index.md)
- Every path is relative to the wiki root (e.g. "wiki/sources/foo.md")
- Every wiki page must have frontmatter (title, type, tags, created, updated, sources)
- Use [[wiki links]] for all cross-references between pages
"""

def run_ingest(source_text: str, source_name: str, citation: str = "") -> dict:
    system_prompt = CLAUDE_MD.read_text()

    user_message = f"""Source to ingest: {source_name}
Citation: {citation}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---

{INGEST_INSTRUCTIONS}
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    # Log tokens
    cost = log_call(
        operation="ingest",
        source_name=source_name,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )

    full_text = response.content[0].text

    # Split on the file ops marker
    parts = full_text.split("===FILE_OPS===")
    summary = parts[0].strip()
    files_written = []
    errors = []

    if len(parts) > 1:
        ops_text = parts[1].strip()
        for line in ops_text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                op = json.loads(line)
                execute_file_op(op)
                files_written.append(op["path"])
            except json.JSONDecodeError as e:
                errors.append(f"JSON parse error: {e} on line: {line[:80]}")
            except KeyError as e:
                errors.append(f"Missing key {e} in op: {line[:80]}")
            except Exception as e:
                errors.append(f"Error executing op: {e}")

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
                # Replace "(none yet)" placeholder if present
                if f"## {section}\n(none yet)" in text:
                    text = text.replace(
                        f"## {section}\n(none yet)",
                        f"## {section}\n{new_line}"
                    )
                # Otherwise insert after section header if not already present
                elif new_line not in text:
                    text = text.replace(
                        f"## {section}\n",
                        f"## {section}\n{new_line}\n",
                        1
                    )
            path.write_text(text)
