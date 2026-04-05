import anthropic
import json
from pathlib import Path
from datetime import date
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

INGEST_INSTRUCTIONS = f"""
You are ingesting a new source into the wiki. Follow the ingest workflow from CLAUDE.md exactly.
Today's date is {date.today().isoformat()}.

After processing the source, output your file operations as a series of JSON blocks, one per line, in this exact format:
{{"op": "write", "path": "wiki/sources/slug.md", "content": "...full markdown content..."}}
{{"op": "write", "path": "wiki/entities/entity-name.md", "content": "..."}}
{{"op": "append", "path": "wiki/log.md", "content": "\\n## [DATE] ingest | Source Title\\n..."}}
{{"op": "update", "path": "wiki/index.md", "section": "Sources", "content": "- [[Source Title]] — one line summary"}}

Rules:
- Output ONLY the JSON blocks after a line that says exactly: ===FILE_OPS===
- Before that line, write a brief summary of what you found in the source (2-3 sentences)
- Every path must be relative to the wiki root
- The write op overwrites the file entirely. The append op adds to the end.
- Always touch: the source page, at least 2 entity/concept pages, log.md, index.md
- Use today's date for log entries
- Each JSON object must be on a single line (no line breaks within a JSON object)
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
            except (json.JSONDecodeError, KeyError):
                continue

    return {
        "summary": summary,
        "files_written": files_written,
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
                text = text.replace(
                    f"## {section}\n(none yet)",
                    f"## {section}\n{new_line}"
                )
                # Otherwise insert after section header
                if new_line not in text:
                    text = text.replace(
                        f"## {section}\n",
                        f"## {section}\n{new_line}\n",
                        1
                    )
            path.write_text(text)
