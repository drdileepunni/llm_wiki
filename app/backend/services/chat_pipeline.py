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


def _traverse_links(seed_paths: set[str], wiki_dir: Path, max_additional: int = 3) -> list[str]:
    """Follow [[wiki links]] one hop from seed pages. Returns additional page paths."""
    additional: list[str] = []
    seen = set(seed_paths)
    for page_path in list(seed_paths):
        full = wiki_dir / page_path
        if not full.exists():
            continue
        links = re.findall(r'\[\[([^\]]+)\]\]', full.read_text(encoding="utf-8"))
        for link in links:
            slug = re.sub(r"[^a-z0-9]+", "-", link.lower()).strip("-")
            for prefix in ("entities/", "concepts/"):
                candidate = f"{prefix}{slug}.md"
                if candidate not in seen and (wiki_dir / candidate).exists():
                    additional.append(candidate)
                    seen.add(candidate)
                    if len(additional) >= max_additional:
                        return additional
    return additional


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
                            "description": (
                                "Name of the clinical parameter or protocol detail, e.g. "
                                "'MAP target in vasopressor-dependent shock', "
                                "'initial ventilator settings post-intubation acute hypoxic respiratory failure', "
                                "'dextrose dose and concentration for hypoglycaemia correction ICU', "
                                "'sodium bicarbonate dose severe metabolic acidosis'."
                            ),
                        },
                        "search_query": {
                            "type": "string",
                            "description": (
                                "Short, focused query to search the medical wiki. "
                                "Strip all patient-specific details — just the clinical concept. "
                                "E.g. 'MAP target vasopressor septic shock', 'noradrenaline titration step ICU', "
                                "'mechanical ventilation settings ARDS initial', 'dextrose 50% hypoglycaemia dose'."
                            ),
                        },
                    },
                    "required": ["parameter", "search_query"],
                },
                "description": (
                    "Specific clinical parameters where precise values matter: drug doses (with renal/hepatic "
                    "adjustment notes if relevant), target ranges, titration protocols, AND procedural "
                    "specifications such as ventilator settings (mode, TV, RR, PEEP, FiO2), intubation "
                    "pre-medication, cardioversion energy, or any other procedure-specific detail required "
                    "to execute an action safely. Each gets its own targeted wiki search. "
                    "If an action in clinical_direction involves a procedure or drug, there should be a "
                    "corresponding query here for its key parameters."
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
                        "resolution_question": {
                            "type": "string",
                            "description": (
                                "Only populate when grounded=false. Write a single precise clinical question "
                                "whose answer would fill this gap — specific to the current patient scenario. "
                                "E.g. 'What is the recommended initial PEEP setting for mechanical ventilation "
                                "in acute hypoxic respiratory failure with SpO2 48%?' rather than 'What is PEEP?'. "
                                "This drives targeted literature search during gap resolution."
                            ),
                        },
                        "entity": {
                            "type": "string",
                            "description": "The drug, device, or clinical entity this parameter belongs to, e.g. 'Oxygen Therapy', 'Dexamethasone', 'Non-Invasive Ventilation'. Never a raw value.",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "The ## section heading this gap belongs to on the wiki page, e.g. 'Dosing', 'Monitoring Parameters', 'Indications'. Must be a template section name, never a raw value like '2-4 L/min'.",
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": "Page type of the target wiki page for this gap.",
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

_CDS_DAGGER_GAP_TOOL = {
    "name": "register_training_gaps",
    "description": (
        "For each †-marked value in the immediate next steps, identify the specific clinical "
        "parameter and write a precise resolution question grounded in this patient's context."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": "The †-marked term or value exactly as it appears, e.g. 'PRVC', 'SpO2 92–96%', 'PEEP >12 cmH2O'",
                        },
                        "resolution_question": {
                            "type": "string",
                            "description": (
                                "A single precise clinical question whose answer would verify this value "
                                "for this patient's specific scenario. Include relevant patient context "
                                "(e.g. diagnosis, severity, organ dysfunction). "
                                "E.g. 'What is the recommended initial PEEP for lung-protective ventilation "
                                "in a patient with acute hypoxic respiratory failure and SpO2 48%?' — "
                                "NOT just 'What is PEEP?'"
                            ),
                        },
                        "wiki_page": {
                            "type": "string",
                            "description": "Suggested wiki page path for this gap, e.g. 'entities/oxygen-therapy.md'. Use a broad entity page, never include section names in the path.",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "The ## section heading this gap belongs to on the wiki page, e.g. 'Dosing', 'Indications', 'Monitoring Parameters'. Must match the page template headings — never use a raw clinical value.",
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": "Page type of the target wiki page.",
                        },
                    },
                    "required": ["term", "resolution_question", "wiki_page", "section_heading"],
                },
            }
        },
        "required": ["gaps"],
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
                    "Ordered, fully executable steps for the ICU team right now. "
                    "Each step must be specific enough to act on without further lookup: "
                    "drug name + dose + route + rate; ventilator mode + TV (ml/kg IBW) + RR + PEEP + FiO2; "
                    "numeric target ranges; renal/hepatic dose adjustments where relevant; "
                    "monitoring checkpoints (e.g. 'recheck glucose in 30 min'). "
                    "Use wiki-grounded values where available; fill in from clinical training where not — "
                    "append '†' to any value drawn from training rather than the wiki. "
                    "Never write a vague action like 'intubate the patient' or 'give dextrose' without full detail. "
                    "Order by clinical urgency."
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
                hits = vs_mod.search_sections(sq["search_query"], kb.wiki_dir, top_k=6, include_patients=include_patient_context)
                if exclude_pattern:
                    hits = [h for h in hits if exclude_pattern.lower() not in h["path"].lower()]
                hits = hits[:4]
                log.info("  Targeted section search %r → %s", sq["search_query"],
                         [f'{h["path"].split("/")[-1]}#{h["section"]}={h["score"]:.3f}' for h in hits])

                seed_paths: set[str] = set()
                param_page_texts = []
                for hit in hits:
                    page_path = hit["path"]
                    pages_consulted.add(page_path)
                    full_path = kb.wiki_dir / page_path
                    if not full_path.exists():
                        for subdir in ("entities", "concepts", "sources", "queries"):
                            candidate = kb.wiki_dir / subdir / Path(page_path).name
                            if candidate.exists():
                                full_path = candidate
                                page_path = f"{subdir}/{Path(page_path).name}"
                                break
                    if full_path.exists():
                        seed_paths.add(page_path)
                        page_content = full_path.read_text(encoding="utf-8")
                        section_text = vs_mod.extract_section(page_content, hit["section"]) if hit["section"] else page_content[:2000]
                        if not section_text:
                            section_text = page_content[:2000]
                        label = f"{page_path} — {hit['section']}" if hit["section"] else page_path
                        param_page_texts.append(f"### {label}\n\n{section_text}")

                # Link traversal: one hop from seed pages
                for linked_path in _traverse_links(seed_paths, kb.wiki_dir, max_additional=2):
                    full_path = kb.wiki_dir / linked_path
                    if full_path.exists():
                        linked_sections = vs_mod._split_sections(linked_path, full_path.read_text(encoding="utf-8"))
                        if linked_sections:
                            top_sec = linked_sections[0]
                            param_page_texts.append(f"### {linked_path} — {top_sec['heading']} [linked]\n\n{top_sec['body'][:600]}")

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
                "- Every step must be fully specific and executable — no vague instructions.\n"
                "- For PROCEDURES (e.g. intubation, mechanical ventilation): specify mode, tidal volume "
                "(ml/kg IBW), RR, PEEP, FiO2 target, and any pre-medication (drug, dose, route).\n"
                "- For DRUGS: specify drug name, dose, route, rate, and any renal/hepatic adjustment. "
                "Note monitoring parameters (e.g. 'recheck glucose in 30 min').\n"
                "- For TARGETS: give the exact numeric range (e.g. 'MAP 65–70 mmHg', 'SpO2 94–98%').\n"
                "- Use wiki-grounded values where available. For anything NOT wiki-grounded, use your "
                "clinical training to supply the standard protocol value — append '†' to flag it "
                "(e.g. 'TV 6 ml/kg IBW†'). Do NOT leave a step vague because a value was ungrounded.\n"
                "- Order by clinical urgency (life threats first).\n"
                "- 5–8 steps maximum.\n\n"
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

        # Step 4: register KGs for every †-marked value in immediate_next_steps
        dagger_steps = [s for s in immediate_next_steps if "†" in s]
        if dagger_steps:
            steps_text = "\n".join(f"- {s}" for s in dagger_steps)
            # Truncate patient summary to keep cost low — first 600 chars covers the key findings
            summary_snippet = question[:600]
            dagger_prompt = (
                f"PATIENT CONTEXT (summary):\n{summary_snippet}\n\n"
                f"IMMEDIATE NEXT STEPS containing †-marked values (drawn from training, not wiki-verified):\n"
                f"{steps_text}\n\n"
                "For each †-marked value above, call register_training_gaps with:\n"
                "- term: the exact †-marked value or parameter\n"
                "- resolution_question: a precise clinical question grounded in this patient's context\n"
                "- wiki_page: a suggested wiki page path for this knowledge\n"
                "One entry per distinct †-marked term. Do not duplicate."
            )
            log.info("CDS Step 4: registering KGs for %d †-marked steps", len(dagger_steps))
            step4 = llm.create_message(
                messages=[{"role": "user", "content": dagger_prompt}],
                tools=[_CDS_DAGGER_GAP_TOOL],
                system="You are a clinical knowledge gap analyst. Extract †-marked parameters and generate targeted resolution questions.",
                max_tokens=2000,
                force_tool=True,
            )
            total_input  += step4.usage.input_tokens
            total_output += step4.usage.output_tokens
            log.info("CDS Step 4 done: in=%d out=%d", step4.usage.input_tokens, step4.usage.output_tokens)
            dagger_block = next((b for b in step4.content if b.type == "tool_use"), None)
            if dagger_block:
                dagger_gaps = dagger_block.input.get("gaps", [])
                from .canonical_registry import resolve as _resolve_canonical
                dagger_entries = []
                for g in dagger_gaps:
                    if not (g.get("term") and g.get("resolution_question") and g.get("wiki_page")):
                        continue
                    # Route using the wiki_page path stem, not the raw term value
                    concept = Path(g["wiki_page"]).stem.replace("-", " ").title()
                    section = g.get("section_heading") or "Dosing"
                    entry = {
                        "page": _resolve_canonical(concept, kb.wiki_dir, llm),
                        "missing_sections": [section],
                        "resolution_question": g["resolution_question"],
                    }
                    if g.get("subtype"):
                        entry["subtype"] = g["subtype"]
                    dagger_entries.append(entry)
                if dagger_entries:
                    try:
                        written = write_gap_files(
                            dagger_entries, kb,
                            source_name=f"cds dagger params: {question[:80]}",
                        )
                        log.info("CDS Step 4: †-gap files registered (%d): %s", len(written), written)
                    except Exception as exc:
                        log.warning("CDS Step 4: failed to write †-gap files: %s", exc)

        # Register one gap file per ungrounded parameter (Step 2 grounding misses)
        gap_entity = gap_sections = ""
        gap_written = None
        ungrounded = [p for p in specific_parameters if not p.get("grounded", True)]
        if ungrounded:
            gap_entity   = ungrounded[0]["parameter"]
            gap_sections = [p["parameter"] for p in ungrounded]
            gap_entries  = []
            from .canonical_registry import resolve as _resolve_canonical
            for param in ungrounded:
                # Use entity name for routing, section_heading for the gap section
                concept = param.get("entity") or param["parameter"]
                section = param.get("section_heading") or "Dosing"
                entry = {
                    "page": _resolve_canonical(concept, kb.wiki_dir, llm),
                    "missing_sections": [section],
                }
                if param.get("resolution_question"):
                    entry["resolution_question"] = param["resolution_question"]
                if param.get("subtype"):
                    entry["subtype"] = param["subtype"]
                gap_entries.append(entry)
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

    # ── QnA mode: section-level retrieval + link traversal then answer ───────
    vec_hits = vs_mod.search_sections(question, kb.wiki_dir, top_k=8, include_patients=include_patient_context)
    if exclude_pattern:
        pat = exclude_pattern.lower()
        vec_hits = [h for h in vec_hits if pat not in h["path"].lower()]
    vec_hits = vec_hits[:6]
    pages = list({h["path"] for h in vec_hits})
    log.info("Section retrieval: %d hits from %d pages  %s", len(vec_hits), len(pages),
             [f'{h["path"].split("/")[-1]}#{h["section"]}={h["score"]:.3f}' for h in vec_hits])

    seed_paths: set[str] = set()
    page_contents = []
    for hit in vec_hits:
        page_path = hit["path"]
        full_path = kb.wiki_dir / page_path
        if not full_path.exists():
            for subdir in ("entities", "concepts", "sources", "queries"):
                candidate = kb.wiki_dir / subdir / Path(page_path).name
                if candidate.exists():
                    full_path = candidate
                    page_path = f"{subdir}/{Path(page_path).name}"
                    break
        if full_path.exists():
            seed_paths.add(page_path)
            page_content = full_path.read_text(encoding="utf-8")
            section_text = vs_mod.extract_section(page_content, hit["section"]) if hit["section"] else page_content[:2000]
            if not section_text:
                section_text = page_content[:2000]
            label = f"{page_path} — {hit['section']}" if hit["section"] else page_path
            page_contents.append(f"### {label}\n\n{section_text}")
            log.info("  Section: %s#%s  (%d chars)", page_path, hit["section"], len(section_text))
        else:
            log.warning("  Missing page: %s", page_path)

    # Link traversal: one hop from seed pages, add top section from each linked page
    for linked_path in _traverse_links(seed_paths, kb.wiki_dir, max_additional=3):
        full_path = kb.wiki_dir / linked_path
        if full_path.exists():
            linked_sections = vs_mod._split_sections(linked_path, full_path.read_text(encoding="utf-8"))
            if linked_sections:
                top_sec = linked_sections[0]
                page_contents.append(f"### {linked_path} — {top_sec['heading']} [linked]\n\n{top_sec['body'][:800]}")
                log.info("  Traversal: %s#%s", linked_path, top_sec["heading"])

    context = "\n\n---\n\n".join(page_contents)
    log.info("QnA retrieval done  context_chars=%d  sections=%d", len(context), len(page_contents))

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
