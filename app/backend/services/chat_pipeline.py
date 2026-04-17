import logging
import json
import re
from pathlib import Path
from datetime import date
from ..config import MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call
from .llm_client import get_llm_client

log = logging.getLogger("wiki.chat_pipeline")

def run_chat(question: str) -> dict:
    log.info("=== CHAT START  question=%r  model=%s ===", question[:120], MODEL)
    llm           = get_llm_client()
    system_prompt = CLAUDE_MD.read_text()
    index_content = (WIKI_DIR / "index.md").read_text()

    total_input = 0
    total_output = 0

    # Step 1: identify relevant pages (plain text response — no tool needed)
    # We use a lightweight direct call here via the raw underlying client
    # since this step returns plain JSON text, not a tool call.
    log.info("Step 1: identifying relevant pages  max_tokens=500")
    step1 = llm.create_message(
        messages=[{
            "role": "user",
            "content": f"""Here is the wiki index:\n\n{index_content}\n\nQuestion: {question}\n\nList the 3-5 most relevant wiki page paths to answer this question. Output only a JSON array of paths, e.g. ["wiki/entities/foo.md", "wiki/concepts/bar.md"]. Nothing else."""
        }],
        tools=[{
            "name": "return_page_list",
            "description": "Return the list of relevant wiki page paths.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths of relevant wiki pages"
                    }
                },
                "required": ["paths"]
            }
        }],
        system=system_prompt,
        max_tokens=500,
    )

    total_input  += step1.usage.input_tokens
    total_output += step1.usage.output_tokens
    log.info("Step 1 response: stop_reason=%s  in=%d  out=%d",
             step1.stop_reason, step1.usage.input_tokens, step1.usage.output_tokens)

    # Extract page list — prefer tool_use block, fall back to JSON in text
    tool_block = next((b for b in step1.content if b.type == "tool_use"), None)
    if tool_block:
        pages = tool_block.input.get("paths", [])
    else:
        raw_text = next((b.text for b in step1.content if b.type == "text"), "")
        match = re.search(r'\[.*?\]', raw_text, re.DOTALL)
        pages = json.loads(match.group()) if match else []
    log.info("Pages selected (%d): %s", len(pages), pages)

    # Read pages
    page_contents = []
    for page_path in pages:
        full_path = WIKI_ROOT / page_path
        if full_path.exists():
            content = full_path.read_text()
            page_contents.append(f"### {page_path}\n\n{content}")
            log.info("  Loaded: %s  (%d chars)", page_path, len(content))
        else:
            log.warning("  Missing page: %s", page_path)

    context = "\n\n---\n\n".join(page_contents)

    # Step 2: answer (plain text — use tool to get structured response)
    log.info("Step 2: generating answer  max_tokens=2000  context_chars=%d", len(context))
    step2 = llm.create_message(
        messages=[{
            "role": "user",
            "content": f"""Using the following wiki pages, answer this question: {question}

Use [[wiki links]] when referencing entities or concepts that have pages.
Cite your sources inline using the page path in parentheses.
Be precise and direct.
If the wiki doesn't have enough information, say so clearly and indicate what kind of source would fill the gap.
Answer any question regardless of topic — this is a general knowledge base.

Wiki pages:
{context}"""
        }],
        tools=[{
            "name": "return_answer",
            "description": "Return the answer to the user's question.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "Full markdown answer with [[wiki links]] and inline citations."
                    }
                },
                "required": ["answer"]
            }
        }],
        system=system_prompt,
        max_tokens=2000,
    )

    total_input  += step2.usage.input_tokens
    total_output += step2.usage.output_tokens
    log.info("Step 2 response: stop_reason=%s  in=%d  out=%d",
             step2.stop_reason, step2.usage.input_tokens, step2.usage.output_tokens)

    # Extract answer — prefer tool_use, fall back to text
    answer_block = next((b for b in step2.content if b.type == "tool_use"), None)
    if answer_block:
        answer = answer_block.input.get("answer", "")
    else:
        answer = next((b.text for b in step2.content if b.type == "text"), "")

    # Log combined cost
    cost = log_call(
        operation="chat",
        source_name=question[:80],
        input_tokens=total_input,
        output_tokens=total_output,
        model=MODEL,
    )

    # Extract wiki links for Obsidian deep link rendering
    wiki_links = re.findall(r'\[\[(.+?)\]\]', answer)

    log.info("=== CHAT END  wiki_links=%d  total_in=%d  total_out=%d  cost=$%.4f ===",
             len(wiki_links), total_input, total_output, cost)

    return {
        "answer": answer,
        "pages_consulted": pages,
        "wiki_links": list(set(wiki_links)),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
        "model": MODEL,
    }

def file_answer(question: str, answer: str) -> str:
    """Write a query answer back to the wiki."""
    slug = re.sub(r'[^a-z0-9]+', '-', question[:50].lower()).strip('-')
    filename = f"{date.today().isoformat()}-{slug}.md"
    path = WIKI_DIR / "queries" / filename
    path.parent.mkdir(exist_ok=True)

    content = f"""---
title: "{question}"
type: query
created: {date.today().isoformat()}
---

# {question}

{answer}
"""
    path.write_text(content)

    # Update index
    index_path = WIKI_DIR / "index.md"
    if index_path.exists():
        text = index_path.read_text()
        entry = f"- [[{question[:60]}]] — {date.today().isoformat()}"
        text = text.replace("## Queries\n(none yet)", f"## Queries\n{entry}")
        if entry not in text:
            text = text.replace("## Queries\n", f"## Queries\n{entry}\n", 1)
        index_path.write_text(text)

    return filename
