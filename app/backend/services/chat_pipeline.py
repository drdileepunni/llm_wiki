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

# ── Tool schemas ──────────────────────────────────────────────────────────────

_QNA_TOOL = {
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
                "description": "Primary missing entity if the wiki couldn't answer. Omit if answered."
            },
            "gap_page_path": {
                "type": "string",
                "description": "Relative wiki path for the gap page. Omit if answered."
            },
            "gap_missing_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Section headings that would fill the gap. Omit if answered."
            }
        },
        "required": ["answer"]
    }
}

_CDS_TOOL = {
    "name": "return_clinical_decision",
    "description": "Return a structured clinical decision support response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "immediate_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Immediate actions with specific drug/dose/route. Use [[wiki links]] for drugs and conditions. Leave empty if the wiki lacks sufficient information."
            },
            "clinical_reasoning": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key timeline findings, physiological rationale, and priority explanation. Cite page paths inline."
            },
            "monitoring_followup": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parameters to monitor and timeframes."
            },
            "alternative_considerations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Edge cases or alternatives where the answer might differ."
            },
            "gap_entity": {
                "type": "string",
                "description": "If the wiki lacks sufficient information to answer, name the primary missing clinical entity or topic. Omit if the question was answered."
            },
            "gap_page_path": {
                "type": "string",
                "description": "Relative wiki path for the gap page (e.g. 'entities/noradrenaline-dosing.md'). Omit if answered."
            },
            "gap_missing_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Section headings that would fill the gap (e.g. ['Dosing', 'Titration', 'Weaning']). Omit if answered."
            }
        },
        "required": ["immediate_actions", "clinical_reasoning", "monitoring_followup", "alternative_considerations"]
    }
}


def run_chat(
    question: str,
    kb: "KBConfig | None" = None,
    images: list | None = None,
    model: str | None = None,
    exclude_pattern: str | None = None,
    mode: str = "qna",
    include_patient_context: bool = False,
) -> dict:
    """
    mode="qna"  — prose markdown answer with wiki citations (default)
    mode="cds"  — structured clinical decision: immediate_actions, clinical_reasoning,
                  monitoring_followup, alternative_considerations
    """
    if kb is None:
        kb = _default_kb()
    resolved_model = model or MODEL
    log.info("=== CHAT START  question=%r  mode=%s  model=%s  kb=%s ===",
             question[:120], mode, resolved_model, kb.name)
    llm           = get_llm_client(model=model)
    system_prompt = kb.claude_md.read_text()

    total_input = 0
    total_output = 0

    # ── Step 1: page retrieval via vector search ──────────────────────────────
    vec_hits = vs_mod.search(question, kb.wiki_dir, top_k=8, include_patients=include_patient_context)
    if exclude_pattern:
        pat = exclude_pattern.lower()
        vec_hits = [h for h in vec_hits if pat not in h["path"].lower()]
    vec_hits = vec_hits[:3]
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

    # ── Step 2: generate answer ───────────────────────────────────────────────
    log.info("Step 2: generating answer  mode=%s  max_tokens=4000  context_chars=%d",
             mode, len(context))

    if mode == "cds":
        prompt_text = f"""Using the following wiki pages as reference, answer this clinical question with a structured decision support response.

{question}

Use [[wiki links]] for any drugs, conditions, or procedures that have wiki pages.
Cite source page paths inline (e.g. "entities/sepsis.md") where relevant.
Write at senior-resident level. Be specific with drug names and doses.

IMPORTANT: If the wiki pages do not contain sufficient information to answer this clinical question, leave immediate_actions empty and populate gap_entity, gap_page_path, and gap_missing_sections so the knowledge gap can be tracked and filled. Do not fabricate clinical guidance not supported by the wiki.

Wiki pages:
{context}"""
        tool = _CDS_TOOL
    else:
        prompt_text = f"""Using the following wiki pages, answer this question: {question}

Use [[wiki links]] when referencing entities or concepts that have pages.
Cite your sources inline using the page path in parentheses.
Be precise and direct.
If the wiki doesn't have enough information, say so clearly and indicate what kind of source would fill the gap.
When the wiki lacks the information to answer the question, also populate gap_entity, gap_page_path, and gap_missing_sections so the gap can be tracked automatically.
Answer any question regardless of topic — this is a general knowledge base.

Wiki pages:
{context}"""
        tool = _QNA_TOOL

    if images:
        message_content = [
            {"type": "image", "source": {"type": "base64", "media_type": img.get("media_type", "image/jpeg"), "data": img["data"]}}
            for img in images
        ] + [{"type": "text", "text": prompt_text}]
    else:
        message_content = prompt_text

    step2 = llm.create_message(
        messages=[{"role": "user", "content": message_content}],
        tools=[tool],
        system=system_prompt,
        max_tokens=4000,
        force_tool=True,
    )

    total_input  += step2.usage.input_tokens
    total_output += step2.usage.output_tokens
    log.info("Step 2 response: stop_reason=%s  in=%d  out=%d",
             step2.stop_reason, step2.usage.input_tokens, step2.usage.output_tokens)

    answer_block = next((b for b in step2.content if b.type == "tool_use"), None)

    # ── Extract by mode ───────────────────────────────────────────────────────
    gap_entity = gap_page = ""
    gap_sections: list = []
    gap_written = None

    if mode == "cds":
        if answer_block is None:
            log.warning("CDS mode: no tool block returned after all retries — returning empty response")
        inp = answer_block.input if answer_block else {}
        immediate_actions          = inp.get("immediate_actions", [])
        clinical_reasoning         = inp.get("clinical_reasoning", [])
        monitoring_followup        = inp.get("monitoring_followup", [])
        alternative_considerations = inp.get("alternative_considerations", [])
        gap_entity   = inp.get("gap_entity", "")
        gap_page     = inp.get("gap_page_path", "")
        gap_sections = inp.get("gap_missing_sections") or []

        # Register knowledge gap when the wiki couldn't answer
        gap_written = None
        if gap_entity and gap_page and gap_sections:
            try:
                written = write_gap_files(
                    [{"page": gap_page, "missing_sections": gap_sections}],
                    kb,
                    source_name=f"unanswered cds query: {question[:80]}",
                )
                gap_written = written[0] if written else None
                log.info("CDS auto-registered gap: %s  sections=%s", gap_written, gap_sections)
            except Exception as exc:
                log.warning("Failed to write CDS gap from query: %s", exc)

        # Collect wiki links from all string fields
        all_text = " ".join(
            immediate_actions + clinical_reasoning +
            monitoring_followup + alternative_considerations
        )
        wiki_links = re.findall(r'\[\[(.+?)\]\]', all_text)

        cost = log_call(
            operation="chat_cds",
            source_name=question[:80],
            input_tokens=total_input,
            output_tokens=total_output,
            model=resolved_model,
            kb_name=kb.name,
        )
        log.info("=== CHAT END (cds)  gap=%s  total_in=%d  total_out=%d  cost=$%.4f ===",
                 gap_written, total_input, total_output, cost)
        return {
            "mode": "cds",
            "answer": "",
            "immediate_actions": immediate_actions,
            "clinical_reasoning": clinical_reasoning,
            "monitoring_followup": monitoring_followup,
            "alternative_considerations": alternative_considerations,
            "pages_consulted": pages,
            "wiki_links": list(set(wiki_links)),
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_usd": cost,
            "model": resolved_model,
            "gap_registered": gap_written,
            "gap_entity": gap_entity or None,
            "gap_sections": gap_sections or [],
        }

    # ── QnA mode extraction ───────────────────────────────────────────────────
    if answer_block:
        answer       = answer_block.input.get("answer", "")
        gap_entity   = answer_block.input.get("gap_entity", "")
        gap_page     = answer_block.input.get("gap_page_path", "")
        gap_sections = answer_block.input.get("gap_missing_sections") or []
    else:
        answer = next((b.text for b in step2.content if b.type == "text"), "")

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

    cost = log_call(
        operation="chat",
        source_name=question[:80],
        input_tokens=total_input,
        output_tokens=total_output,
        model=resolved_model,
        kb_name=kb.name,
    )

    wiki_links = re.findall(r'\[\[(.+?)\]\]', answer)
    log.info("=== CHAT END (qna)  wiki_links=%d  total_in=%d  total_out=%d  cost=$%.4f ===",
             len(wiki_links), total_input, total_output, cost)

    return {
        "mode": "qna",
        "answer": answer,
        "pages_consulted": pages,
        "wiki_links": list(set(wiki_links)),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
        "model": resolved_model,
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
