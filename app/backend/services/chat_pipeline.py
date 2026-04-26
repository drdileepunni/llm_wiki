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

_CDS_REASON_TOOL = {
    "name": "clinical_reasoning",
    "description": "Reason through the clinical question and identify specific parameters to verify in the wiki.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clinical_direction": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "High-level clinical actions from your medical training. ALWAYS populate — "
                    "no wiki needed. State what to do and why at the level of clinical logic, "
                    "e.g. 'Reduce noradrenaline — MAP 144 well above target range.' "
                    "Use [[wiki links]] for drugs and conditions."
                ),
            },
            "clinical_reasoning": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key findings, physiological rationale, and priority explanation.",
            },
            "monitoring_followup": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parameters to monitor and timeframes.",
            },
            "alternative_considerations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Edge cases or alternatives.",
            },
            "specific_queries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {
                            "type": "string",
                            "description": "Name of the clinical parameter, e.g. 'MAP target in vasopressor-dependent shock'.",
                        },
                        "search_query": {
                            "type": "string",
                            "description": (
                                "Short, focused query to search the medical wiki. "
                                "Strip all patient-specific details — just the clinical concept. "
                                "E.g. 'MAP target vasopressor septic shock', 'noradrenaline titration step ICU'."
                            ),
                        },
                    },
                    "required": ["parameter", "search_query"],
                },
                "description": (
                    "Specific clinical parameters where precise values matter: drug doses, "
                    "target ranges, titration protocols. Each gets its own targeted wiki search."
                ),
            },
        },
        "required": ["clinical_direction", "clinical_reasoning", "monitoring_followup", "alternative_considerations", "specific_queries"],
    },
}

_CDS_GROUND_TOOL = {
    "name": "ground_parameters",
    "description": "For each parameter, state whether the provided wiki context confirms the value.",
    "input_schema": {
        "type": "object",
        "properties": {
            "groundings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {"type": "string"},
                        "value": {
                            "type": "string",
                            "description": "The confirmed value/dose/range if found in the wiki. Empty string if not found.",
                        },
                        "grounded": {
                            "type": "boolean",
                            "description": "true only if the wiki context explicitly supports this value.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Wiki page path that confirms the value. Empty if not grounded.",
                        },
                    },
                    "required": ["parameter", "value", "grounded"],
                },
                "description": "One entry per parameter submitted.",
            },
        },
        "required": ["groundings"],
    },
}

_CDS_SYNTHESIZE_TOOL = {
    "name": "synthesize_next_steps",
    "description": "Synthesise clinical reasoning and wiki-grounded parameters into a prioritised action list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "immediate_next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ordered, specific, actionable steps for the ICU team right now. "
                    "Each step must be concrete: include drug name, dose/rate, target value, route where relevant. "
                    "Prefer grounded values from the wiki over generic statements. "
                    "Order by clinical priority."
                ),
            },
        },
        "required": ["immediate_next_steps"],
    },
}

_CDS_TOOL = {
    "name": "return_clinical_decision",
    "description": "Return a structured clinical decision support response.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clinical_direction": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "High-level clinical actions derived from your medical training and the clinical picture. "
                    "ALWAYS populate this — it does not require wiki grounding. "
                    "State what to do and why, at the level of 'reduce vasopressor dose given MAP 144 exceeds target', "
                    "not specific numeric titration steps. Use [[wiki links]] for drugs and conditions."
                ),
            },
            "specific_parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {
                            "type": "string",
                            "description": "The specific clinical detail needed, e.g. 'Noradrenaline titration step', 'MAP target in vasopressor-dependent shock'.",
                        },
                        "value": {
                            "type": "string",
                            "description": "The recommended value/dose/threshold. Leave empty string if not found in wiki.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Wiki page path supporting this value. Leave empty if not wiki-grounded.",
                        },
                        "grounded": {
                            "type": "boolean",
                            "description": "true if a retrieved wiki page confirms this value; false if uncertain or absent from the wiki.",
                        },
                    },
                    "required": ["parameter", "value", "grounded"],
                },
                "description": (
                    "Specific clinical parameters where precision matters: drug doses, titration steps, "
                    "target thresholds, drug interactions. Only list parameters relevant to this case. "
                    "If a value is not supported by a retrieved wiki page, set grounded=false — "
                    "a knowledge gap will be registered for that parameter automatically. "
                    "Do NOT fabricate values; set grounded=false instead."
                ),
            },
            "clinical_reasoning": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key timeline findings, physiological rationale, and priority explanation.",
            },
            "monitoring_followup": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Parameters to monitor and timeframes.",
            },
            "alternative_considerations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Edge cases or alternatives where the answer might differ.",
            },
            "gap_entity": {
                "type": "string",
                "description": (
                    "Name the primary missing clinical entity only when one or more specific_parameters "
                    "have grounded=false. Omit if all parameters are grounded."
                ),
            },
            "gap_page_path": {
                "type": "string",
                "description": "Relative wiki path for the gap page, e.g. 'entities/noradrenaline-dosing.md'. Omit if all grounded.",
            },
            "gap_missing_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Section headings that would fill the gap for the ungrounded parameters.",
            },
        },
        "required": ["clinical_direction", "clinical_reasoning", "monitoring_followup", "alternative_considerations"],
    },
}


def run_chat(
    question: str,
    kb: "KBConfig | None" = None,
    images: list | None = None,
    model: str | None = None,
    exclude_pattern: str | None = None,
    mode: str = "qna",
    include_patient_context: bool = False,
    reasoning_model: str | None = None,
) -> dict:
    """
    mode="qna"  — prose markdown answer with wiki citations (default)
    mode="cds"  — structured clinical decision: immediate_actions, clinical_reasoning,
                  monitoring_followup, alternative_considerations
    """
    if kb is None:
        kb = _default_kb()
    resolved_model          = model or MODEL
    resolved_reasoning_model = reasoning_model or resolved_model
    log.info("=== CHAT START  question=%r  mode=%s  model=%s  reasoning_model=%s  kb=%s ===",
             question[:120], mode, resolved_model, resolved_reasoning_model, kb.name)
    llm           = get_llm_client(model=model)
    reasoning_llm = get_llm_client(model=reasoning_model) if reasoning_model and reasoning_model != resolved_model else llm
    system_prompt = kb.claude_md.read_text()

    total_input = 0
    total_output = 0

    # ── CDS mode: reason → targeted retrieval → ground ───────────────────────
    if mode == "cds":
        # Call 1: reason from clinical training, no wiki — identify direction + what to look up
        log.info("CDS Step 1: reasoning (no wiki)  model=%s", resolved_reasoning_model)
        reason_prompt = f"""You are a senior ICU clinician. Reason through this clinical question using your medical training.

{question}

Provide your clinical direction, reasoning, and monitoring plan.
Also list any specific clinical parameters where precise values matter (drug doses, titration steps, target ranges, protocol thresholds).
These will be looked up in the medical wiki separately — keep each search_query short and focused on the clinical concept only, with no patient-specific details."""

        reason_msg = reason_prompt
        if images:
            reason_msg = [
                {"type": "image", "source": {"type": "base64", "media_type": img.get("media_type", "image/jpeg"), "data": img["data"]}}
                for img in images
            ] + [{"type": "text", "text": reason_prompt}]

        step1 = reasoning_llm.create_message(
            messages=[{"role": "user", "content": reason_msg}],
            tools=[_CDS_REASON_TOOL],
            system=system_prompt,
            max_tokens=4000,
            force_tool=True,
            thinking_budget=4000,
        )
        total_input  += step1.usage.input_tokens
        total_output += step1.usage.output_tokens
        log.info("CDS Step 1 done: stop=%s  in=%d  out=%d", step1.stop_reason, step1.usage.input_tokens, step1.usage.output_tokens)

        reason_block = next((b for b in step1.content if b.type == "tool_use"), None)
        inp1 = reason_block.input if reason_block else {}
        clinical_direction         = inp1.get("clinical_direction", [])
        clinical_reasoning         = inp1.get("clinical_reasoning", [])
        monitoring_followup        = inp1.get("monitoring_followup", [])
        alternative_considerations = inp1.get("alternative_considerations", [])
        specific_queries           = inp1.get("specific_queries", [])
        log.info("CDS Step 1: direction=%d  reasoning=%d  queries=%d", len(clinical_direction), len(clinical_reasoning), len(specific_queries))

        # Call 2: targeted vector search per parameter, then ground
        pages_consulted: set = set()
        specific_parameters: list = []

        if specific_queries:
            param_contexts = []
            for sq in specific_queries:
                hits = vs_mod.search(sq["search_query"], kb.wiki_dir, top_k=5, include_patients=include_patient_context)
                if exclude_pattern:
                    hits = [h for h in hits if exclude_pattern.lower() not in h["path"].lower()]
                hits = hits[:3]
                log.info("  Targeted search %r → %s", sq["search_query"],
                         [(h["path"].split("/")[-1], round(h["score"], 3)) for h in hits])

                param_page_texts = []
                for hit in hits:
                    pages_consulted.add(hit["path"])
                    full_path = kb.wiki_dir / hit["path"]
                    if not full_path.exists():
                        for subdir in ("entities", "concepts", "sources", "queries"):
                            candidate = kb.wiki_dir / subdir / Path(hit["path"]).name
                            if candidate.exists():
                                full_path = candidate
                                break
                    if full_path.exists():
                        param_page_texts.append(f"### {hit['path']}\n\n{full_path.read_text()}")

                param_contexts.append({
                    "parameter": sq["parameter"],
                    "context": "\n\n---\n\n".join(param_page_texts) if param_page_texts else "(no relevant wiki pages found)",
                })

            # Build grounding prompt with per-parameter wiki context
            grounding_parts = [
                "For each parameter below, check the provided wiki context.\n"
                "Set grounded=true only if the wiki explicitly confirms the value. "
                "Do NOT fabricate; set grounded=false if uncertain.\n"
            ]
            for pc in param_contexts:
                grounding_parts.append(f"PARAMETER: {pc['parameter']}\nWIKI CONTEXT:\n{pc['context']}\n")

            log.info("CDS Step 2: grounding %d parameters", len(param_contexts))
            step2 = llm.create_message(
                messages=[{"role": "user", "content": "\n\n".join(grounding_parts)}],
                tools=[_CDS_GROUND_TOOL],
                system=system_prompt,
                max_tokens=2000,
                force_tool=True,
            )
            total_input  += step2.usage.input_tokens
            total_output += step2.usage.output_tokens
            log.info("CDS Step 2 done: stop=%s  in=%d  out=%d", step2.stop_reason, step2.usage.input_tokens, step2.usage.output_tokens)

            ground_block = next((b for b in step2.content if b.type == "tool_use"), None)
            if ground_block:
                specific_parameters = ground_block.input.get("groundings", [])

        pages = list(pages_consulted)

        # Step 3: synthesise direction + grounded params into immediate_next_steps
        immediate_next_steps: list = []
        if clinical_direction or specific_parameters:
            direction_text = "\n".join(f"- {d}" for d in clinical_direction)
            params_text = "\n".join(
                f"- {p['parameter']}: {p['value']} ({'wiki-grounded' if p.get('grounded') else 'not in wiki'})"
                for p in specific_parameters if p.get("value")
            ) or "(none)"
            synth_prompt = (
                "You are a senior ICU consultant. Translate the clinical reasoning and parameters below "
                "into a prioritised list of IMMEDIATE NEXT STEPS for the bedside team.\n\n"
                "Rules:\n"
                "- Each step must be specific and actionable (e.g. 'Reduce noradrenaline by 0.02 mcg/kg/min targeting MAP 65–70 mmHg')\n"
                "- Incorporate the exact doses and targets from the wiki-grounded parameters\n"
                "- Order by clinical urgency\n"
                "- 5–8 steps maximum\n\n"
                f"CLINICAL DIRECTION:\n{direction_text}\n\n"
                f"SPECIFIC PARAMETERS (wiki-grounded):\n{params_text}"
            )
            log.info("CDS Step 3: synthesising immediate next steps")
            step3 = llm.create_message(
                messages=[{"role": "user", "content": synth_prompt}],
                tools=[_CDS_SYNTHESIZE_TOOL],
                system=system_prompt,
                max_tokens=1500,
                force_tool=True,
            )
            total_input  += step3.usage.input_tokens
            total_output += step3.usage.output_tokens
            log.info("CDS Step 3 done: in=%d out=%d", step3.usage.input_tokens, step3.usage.output_tokens)
            synth_block = next((b for b in step3.content if b.type == "tool_use"), None)
            if synth_block:
                immediate_next_steps = synth_block.input.get("immediate_next_steps", [])

        # Register one gap file per ungrounded parameter
        gap_entity = gap_sections = ""
        gap_written = None
        ungrounded = [p["parameter"] for p in specific_parameters if not p.get("grounded", True)]
        if ungrounded:
            gap_entity   = ungrounded[0]
            gap_sections = ungrounded
            gap_entries  = []
            for param in ungrounded:
                slug = re.sub(r"[^a-z0-9]+", "-", param.lower()).strip("-")
                gap_entries.append({"page": f"entities/{slug}.md", "missing_sections": [param]})
            try:
                written = write_gap_files(gap_entries, kb, source_name=f"ungrounded cds params: {question[:80]}")
                gap_written = written[0] if written else None
                log.info("CDS gaps registered (%d): %s", len(gap_entries), written)
            except Exception as exc:
                log.warning("Failed to write CDS gaps: %s", exc)

        all_text = " ".join(
            clinical_direction + clinical_reasoning + monitoring_followup + alternative_considerations +
            [p.get("value", "") for p in specific_parameters]
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
        log.info(
            "=== CHAT END (cds)  direction=%d  params=%d(ungrounded=%d)  gap=%s  in=%d  out=%d  cost=$%.4f ===",
            len(clinical_direction), len(specific_parameters), len(ungrounded),
            gap_written, total_input, total_output, cost,
        )
        return {
            "mode": "cds",
            "answer": "",
            "immediate_next_steps": immediate_next_steps,
            "clinical_direction": clinical_direction,
            "immediate_actions": clinical_direction,  # backward compat alias
            "specific_parameters": specific_parameters,
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

    # ── QnA mode: broad retrieval then answer ────────────────────────────────
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
    log.info("QnA retrieval done  context_chars=%d", len(context))

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
        tools=[_QNA_TOOL],
        system=system_prompt,
        max_tokens=4000,
        force_tool=True,
    )

    total_input  += step2.usage.input_tokens
    total_output += step2.usage.output_tokens
    log.info("QnA response: stop_reason=%s  in=%d  out=%d",
             step2.stop_reason, step2.usage.input_tokens, step2.usage.output_tokens)

    answer_block = next((b for b in step2.content if b.type == "tool_use"), None)

    # ── QnA mode extraction ───────────────────────────────────────────────────
    gap_entity = gap_page = ""
    gap_sections: list = []
    gap_written = None

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
