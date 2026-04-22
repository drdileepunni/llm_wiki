import logging
import json
import re
from pathlib import Path
from datetime import date
from ..config import MODEL, KBConfig, _default_kb
from .token_tracker import log_call
from .llm_client import get_llm_client
from .ingest_pipeline import write_gap_files
from . import vector_store as vs_mod

log = logging.getLogger("wiki.chat_pipeline")

def run_chat(question: str, kb: "KBConfig | None" = None, images: list | None = None) -> dict:
    if kb is None:
        kb = _default_kb()
    log.info("=== CHAT START  question=%r  model=%s  kb=%s ===", question[:120], MODEL, kb.name)
    llm           = get_llm_client()
    system_prompt = kb.claude_md.read_text()

    total_input = 0
    total_output = 0

    # ── Step 1: page retrieval via vector search ──────────────────────────────
    # Embed the question and rank all wiki pages by cosine similarity.
    # No LLM call needed — instant, scales to any index size.
    vec_hits = vs_mod.search(question, kb.wiki_dir, top_k=3)
    pages = [h["path"] for h in vec_hits]
    log.info("Vector retrieval: %d pages  %s", len(pages),
             [(h["path"].split("/")[-1], round(h["score"], 3)) for h in vec_hits])

    page_contents = []
    for page_path in pages:
        full_path = kb.wiki_dir / page_path
        if not full_path.exists():
            for subdir in ("entities", "concepts", "sources", "queries"):
                candidate = kb.wiki_dir / subdir / Path(page_path).name
                if candidate.exists():
                    full_path = candidate
                    page_path = f"{subdir}/{Path(page_path).name}"
                    break
        if full_path.exists():
            content = full_path.read_text()
            page_contents.append(f"### {page_path}\n\n{content}")
            log.info("  Loaded: %s  (%d chars)", page_path, len(content))
        else:
            log.warning("  Missing page: %s", page_path)

    context = "\n\n---\n\n".join(page_contents)

    # Step 2: answer (plain text — use tool to get structured response)
    log.info("Step 2: generating answer  max_tokens=2000  context_chars=%d  images=%d",
             len(context), len(images) if images else 0)
    prompt_text = f"""Using the following wiki pages, answer this question: {question}

Use [[wiki links]] when referencing entities or concepts that have pages.
Cite your sources inline using the page path in parentheses.
Be precise and direct.
If the wiki doesn't have enough information, say so clearly and indicate what kind of source would fill the gap.
When the wiki lacks the information to answer the question, also populate gap_entity, gap_page_path, and gap_missing_sections so the gap can be tracked automatically.
Answer any question regardless of topic — this is a general knowledge base.

Wiki pages:
{context}"""

    if images:
        message_content = [
            {"type": "image", "source": {"type": "base64", "media_type": img.get("media_type", "image/jpeg"), "data": img["data"]}}
            for img in images
        ] + [{"type": "text", "text": prompt_text}]
    else:
        message_content = prompt_text

    step2 = llm.create_message(
        messages=[{"role": "user", "content": message_content}],
        tools=[{
            "name": "return_answer",
            "description": "Return the answer to the user's question.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "Full markdown answer with [[wiki links]] and inline citations."
                    },
                    "gap_entity": {
                        "type": "string",
                        "description": "If the wiki could not answer the question, the primary entity or concept that is missing (e.g. 'Severe Bradycardia'). Omit if the question was answered."
                    },
                    "gap_page_path": {
                        "type": "string",
                        "description": "Relative wiki path for the gap page, e.g. wiki/entities/severe-bradycardia.md. Omit if the question was answered."
                    },
                    "gap_missing_sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific section headings that would answer the question. Omit if the question was answered."
                    }
                },
                "required": ["answer"]
            }
        }],
        system=system_prompt,
        max_tokens=4000,
        force_tool=False,
    )

    total_input  += step2.usage.input_tokens
    total_output += step2.usage.output_tokens
    log.info("Step 2 response: stop_reason=%s  in=%d  out=%d",
             step2.stop_reason, step2.usage.input_tokens, step2.usage.output_tokens)

    # Extract answer — prefer tool_use, fall back to text
    answer_block = next((b for b in step2.content if b.type == "tool_use"), None)
    if answer_block:
        answer = answer_block.input.get("answer", "")
        gap_entity   = answer_block.input.get("gap_entity", "")
        gap_page     = answer_block.input.get("gap_page_path", "")
        gap_sections = answer_block.input.get("gap_missing_sections") or []
    else:
        answer = next((b.text for b in step2.content if b.type == "text"), "")
        gap_entity = gap_page = ""
        gap_sections = []

    # Auto-register gap if the model flagged one
    gap_written = None
    if gap_entity and gap_page and gap_sections:
        try:
            written = write_gap_files(
                [{"page": gap_page, "missing_sections": gap_sections}],
                kb,
                source_name=f"unanswered query: {question[:80]}",
            )
            gap_written = written[0] if written else None
            log.info("Auto-registered gap: %s  sections=%s", gap_written, gap_sections)
        except Exception as exc:
            log.warning("Failed to write gap from query: %s", exc)

    # Log combined cost
    cost = log_call(
        operation="chat",
        source_name=question[:80],
        input_tokens=total_input,
        output_tokens=total_output,
        model=MODEL,
        kb_name=kb.name,
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
        "gap_registered": gap_written,
        "gap_entity": gap_entity or None,
        "gap_sections": gap_sections or [],
    }

def file_answer(question: str, answer: str, kb: "KBConfig | None" = None) -> str:
    """Write a query answer back to the wiki."""
    if kb is None:
        kb = _default_kb()
    slug = re.sub(r'[^a-z0-9]+', '-', question[:50].lower()).strip('-')
    filename = f"{date.today().isoformat()}-{slug}.md"
    path = kb.wiki_dir / "queries" / filename
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
    index_path = kb.wiki_dir / "index.md"
    if index_path.exists():
        text = index_path.read_text()
        entry = f"- [[{question[:60]}]] — {date.today().isoformat()}"
        text = text.replace("## Queries\n(none yet)", f"## Queries\n{entry}")
        if entry not in text:
            text = text.replace("## Queries\n", f"## Queries\n{entry}\n", 1)
        index_path.write_text(text)

    return filename
