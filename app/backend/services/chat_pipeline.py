import anthropic
import json
import re
from pathlib import Path
from datetime import date
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def run_chat(question: str) -> dict:
    system_prompt = CLAUDE_MD.read_text()
    index_content = (WIKI_DIR / "index.md").read_text()

    total_input = 0
    total_output = 0

    # Step 1: identify relevant pages
    step1 = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""Here is the wiki index:\n\n{index_content}\n\nQuestion: {question}\n\nList the 3-5 most relevant wiki page paths to answer this question. Output only a JSON array of paths, e.g. ["wiki/entities/foo.md", "wiki/concepts/bar.md"]. Nothing else."""
        }]
    )

    total_input += step1.usage.input_tokens
    total_output += step1.usage.output_tokens

    # Parse page list
    raw = step1.content[0].text.strip()
    match = re.search(r'\[.*?\]', raw, re.DOTALL)
    pages = json.loads(match.group()) if match else []

    # Read pages
    page_contents = []
    for page_path in pages:
        full_path = WIKI_ROOT / page_path
        if full_path.exists():
            content = full_path.read_text()
            page_contents.append(f"### {page_path}\n\n{content}")

    context = "\n\n---\n\n".join(page_contents)

    # Step 2: answer
    step2 = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""Using the following wiki pages, answer this question: {question}

Use [[wiki links]] when referencing entities or concepts that have pages.
Cite your sources inline using the page path in parentheses.
Be precise and direct. If the wiki doesn't have enough information to answer, say so clearly.

Wiki pages:
{context}"""
        }]
    )

    total_input += step2.usage.input_tokens
    total_output += step2.usage.output_tokens

    # Log combined cost
    cost = log_call(
        operation="chat",
        source_name=question[:80],
        input_tokens=total_input,
        output_tokens=total_output,
    )

    answer = step2.content[0].text

    # Extract wiki links for Obsidian deep link rendering
    wiki_links = re.findall(r'\[\[(.+?)\]\]', answer)

    return {
        "answer": answer,
        "pages_consulted": pages,
        "wiki_links": list(set(wiki_links)),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
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
