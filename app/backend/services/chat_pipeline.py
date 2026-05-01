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

# ── Retrieval helpers ─────────────────────────────────────────────────────────

_MIN_RETRIEVAL_SCORE = 0.55   # below this → try a shorter fallback query
_GAP_COVER_THRESHOLD  = 0.72  # above this → section likely already covered


def _merge_hits(hit_lists: list[list[dict]]) -> list[dict]:
    """Merge hit lists from multiple queries, keeping best score per (path, section)."""
    seen: dict[tuple, dict] = {}
    for hits in hit_lists:
        for h in hits:
            key = (h["path"], h.get("section", ""))
            if key not in seen or h["score"] > seen[key]["score"]:
                seen[key] = h
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)


def _best_section_for_query(sections: list[dict], query: str) -> dict:
    """Pick the most relevant section from a linked page by heading word overlap."""
    query_words = set(query.lower().split())
    scored = [
        (len(query_words & set(s["heading"].lower().split())), s)
        for s in sections
    ]
    best_overlap, best = max(scored, key=lambda t: t[0])
    return best if best_overlap > 0 else sections[0]



def _gap_already_covered(entity: str, section: str, wiki_dir: Path) -> bool:
    """
    Return True if the wiki already has substantial content for entity+section.
    Uses a vector search with a clean concept query — no LLM call needed.
    """
    query = f"{entity} {section}".strip()
    if not query:
        return False
    try:
        hits = vs_mod.search_sections(query, wiki_dir, top_k=3)
        if not hits or hits[0]["score"] < _GAP_COVER_THRESHOLD:
            return False
        hit = hits[0]
        full_path = wiki_dir / hit["path"]
        if not full_path.exists():
            return False
        content = full_path.read_text(encoding="utf-8")
        section_text = vs_mod.extract_section(content, hit["section"]) if hit["section"] else ""
        if not section_text:
            section_text = content[:500]
        covered = len(section_text.strip()) > 100
        if covered:
            log.info(
                "  Pre-write check: '%s / %s' → covered by %s#%s (score=%.2f)",
                entity, section, hit["path"], hit["section"], hits[0]["score"],
            )
        return covered
    except Exception as exc:
        log.debug("_gap_already_covered error (non-fatal): %s", exc)
        return False

log = logging.getLogger("wiki.chat_pipeline")

_NOT_IN_WIKI_PATTERN = re.compile(
    r"value not in wiki|not in wiki|per local protocol|consult local protocol|"
    r"dose not available|doses per local|specific dose|see local protocol|"
    r"refer to local|local guidelines",
    re.IGNORECASE,
)

_SCAN_GAPS_TOOL = {
    "name": "extract_gaps",
    "description": "Extract structured knowledge gaps from clinical action steps that are missing specific values (doses, settings, targets).",
    "input_schema": {
        "type": "object",
        "properties": {
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entity": {
                            "type": "string",
                            "description": "The drug, device, or clinical entity whose value is missing. E.g. 'Furosemide', 'Non-Invasive Ventilation', 'Enoxaparin'. Never a vague term.",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "The wiki section where this value belongs. E.g. 'Dosing', 'Monitoring Parameters', 'Ventilator Settings', 'Targets'. Must be a section name, not a raw value.",
                        },
                        "resolution_question": {
                            "type": "string",
                            "description": "A single precise clinical question whose answer fills this gap. Make it specific to the patient context. E.g. 'What is the initial IV furosemide dose in acute decompensated heart failure with preserved EF?' not 'What is the furosemide dose?'",
                        },
                        "missing_values": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Specific data points missing, each including the clinical context. "
                                "One item per distinct value. ALWAYS append 'for [clinical condition/scenario]' "
                                "so the value is unambiguous when filed out of context. "
                                "E.g. ['Initial IV furosemide bolus dose (mg) for acute decompensated heart failure', "
                                "'Lactulose dose for hepatic encephalopathy', "
                                "'IPAP/EPAP settings for acute hypercapnic respiratory failure']"
                            ),
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": "Page type for the wiki entry.",
                        },
                    },
                    "required": ["entity", "section_heading", "resolution_question", "missing_values", "subtype"],
                },
            },
        },
        "required": ["gaps"],
    },
}


def _values_present_in_section(section_text: str, missing_values: list[str], llm, entity: str = "") -> bool:
    """
    Ask the LLM whether the specific values in missing_values are explicitly
    stated in section_text. Returns True only if ALL values are present.
    A fast yes/no call — no tool use, minimal tokens.
    """
    if not section_text.strip() or not missing_values:
        return False
    values_list = "\n".join(f"- {v}" for v in missing_values)
    entity_clause = f" specifically about '{entity}'" if entity else ""
    prompt = (
        f"Wiki section content:\n{section_text[:1500]}\n\n"
        f"Are ALL of the following specific clinical values explicitly stated "
        f"(as actual numbers, doses, or settings){entity_clause} in the section above?\n{values_list}\n\n"
        "Answer NO if the section discusses a related but different topic (e.g. sedation monitoring "
        "when the question is about neurological assessment frequency, or laxative dosing when the "
        "question is about a specific antibiotic dose).\n"
        "Reply with only: YES or NO"
    )
    try:
        response = llm.create_message(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system="You are a precise fact-checker. Answer only YES or NO.",
            max_tokens=5,
            force_tool=False,
            thinking_budget=0,
        )
        answer = next((b.text for b in response.content if hasattr(b, "text") and b.text), "NO")
        return answer.strip().upper().startswith("YES")
    except Exception as exc:
        log.debug("_values_present_in_section error (non-fatal): %s", exc)
        return False


_GAP_CONFIRM_THRESHOLD = 0.75  # higher than _GAP_COVER_THRESHOLD to avoid cross-topic false positives

def _get_section_text(entity: str, section: str, wiki_dir: Path, threshold: float = _GAP_CONFIRM_THRESHOLD) -> str:
    """
    Retrieve the best-matching section text from the wiki for entity+section.
    Returns empty string if nothing found above threshold.
    Uses a higher default (0.80) than retrieval so borderline cross-topic matches
    don't suppress gap registration.
    """
    query = f"{entity} {section}".strip()
    try:
        hits = vs_mod.search_sections(query, wiki_dir, top_k=3)
        if not hits or hits[0]["score"] < threshold:
            return ""
        hit = hits[0]
        full_path = wiki_dir / hit["path"]
        if not full_path.exists():
            return ""
        content = full_path.read_text(encoding="utf-8")
        section_text = vs_mod.extract_section(content, hit["section"]) if hit["section"] else ""
        return section_text or content[:800]
    except Exception as exc:
        log.debug("_get_section_text error (non-fatal): %s", exc)
        return ""


def _rewrite_grounded_steps(
    steps: list[str],
    kb: "KBConfig",
    llm,
) -> tuple[list[str], tuple[int, int], set[int]]:
    """
    For each step containing a 'value not in wiki' marker, attempt a targeted
    wiki lookup. If the specific value is found, rewrite the step with it.
    If not found, leave the marker unchanged (scan_text_gaps will file the gap).
    Returns (rewritten_steps, (total_input_tokens, total_output_tokens), failed_indices).
    failed_indices: step indices where the marker could not be resolved — scan_text_gaps
    should skip content verification for these and always file a KG.
    """
    flagged_indices = [i for i, s in enumerate(steps) if _NOT_IN_WIKI_PATTERN.search(s)]
    if not flagged_indices:
        return steps, (0, 0), set()

    log.info("CDS Step 4: rewriting %d steps with unresolved markers", len(flagged_indices))
    total_in = total_out = 0
    result = list(steps)
    failed: set[int] = set()

    for i in flagged_indices:
        step = steps[i]
        marker_count = len(_NOT_IN_WIKI_PATTERN.findall(step))

        # Extract one search phrase per distinct missing entity.
        # Multi-marker steps need multiple queries so each entity gets its own hit.
        extract_resp = llm.create_message(
            messages=[{"role": "user", "content": (
                f"Clinical step ({marker_count} missing value(s) marked):\n{step}\n\n"
                "Each '(value not in wiki — consult local protocol)' placeholder refers to a "
                "specific missing clinical value. List ONE short wiki search phrase per distinct "
                "missing entity — drug, device, or parameter. Return a JSON array of strings, "
                "e.g. [\"rapid sequence intubation induction agent dose\", \"mechanical ventilation tidal volume PEEP FiO2\"] "
                "or [\"furosemide IV bolus dose heart failure\"] for a single marker. "
                "IMPORTANT: always spell out abbreviations fully — write 'rapid sequence intubation' not 'RSI', "
                "'non-invasive ventilation' not 'NIV', 'mean arterial pressure' not 'MAP'. "
                "No explanation, JSON only."
            )}],
            tools=[],
            system="You extract clinical wiki search queries. Spell out all abbreviations fully. Return a JSON array of short phrases, one per distinct missing entity.",
            max_tokens=120,
            force_tool=False,
            thinking_budget=0,
        )
        total_in  += extract_resp.usage.input_tokens
        total_out += extract_resp.usage.output_tokens
        raw = next((b.text for b in extract_resp.content if hasattr(b, "text") and b.text), "").strip()

        # Parse the JSON list; fall back to treating the whole response as one query
        import json as _json
        try:
            queries = _json.loads(raw)
            if not isinstance(queries, list):
                queries = [str(queries)]
        except Exception:
            queries = [raw] if raw else []

        queries = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if not queries:
            failed.add(i)
            continue

        # Search for each query independently; collect unique (path, section) hits
        seen_keys: set[tuple] = set()
        context_blocks: list[str] = []
        any_hit = False

        for query in queries:
            hits = vs_mod.search_sections(query, kb.wiki_dir, top_k=3)
            if not hits or hits[0]["score"] < _GAP_COVER_THRESHOLD:
                log.info("  Step 4: no wiki hit for %r (top=%.2f)", query, hits[0]["score"] if hits else 0)
                continue
            hit = hits[0]
            key = (hit["path"], hit.get("section", ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            full_path = kb.wiki_dir / hit["path"]
            if not full_path.exists():
                continue
            content = full_path.read_text(encoding="utf-8")
            section_text = vs_mod.extract_section(content, hit["section"]) if hit["section"] else content[:1200]
            if not section_text:
                section_text = content[:1200]
            log.info("  Step 4: [%r] → %s#%s (score=%.2f)", query, hit["path"], hit["section"], hit["score"])
            context_blocks.append(f"### {hit['path']}#{hit.get('section','')}\n{section_text[:1200]}")
            any_hit = True

        if not any_hit:
            log.info("  Step 4: no wiki hits across %d queries for step %d — marking as gap", len(queries), i)
            failed.add(i)
            continue

        # One rewrite pass with all found sections as combined context
        combined_context = "\n\n".join(context_blocks)
        rewrite_resp = llm.create_message(
            messages=[{"role": "user", "content": (
                f"Wiki sections:\n{combined_context}\n\n"
                f"Clinical step to rewrite:\n{step}\n\n"
                "Replace EVERY '(value not in wiki — consult local protocol)' placeholder with "
                "the exact numeric value from the wiki sections above. "
                "STRICT RULE: only substitute values explicitly stated as numbers in the sections. "
                "If a value is not explicitly present, leave that placeholder unchanged. "
                "Do not add citations or file path references. "
                "Return only the rewritten step text, nothing else."
            )}],
            tools=[],
            system="You are a precise clinical editor. Substitute only values explicitly present in the wiki sections. Never invent or estimate.",
            max_tokens=400,
            force_tool=False,
            thinking_budget=0,
        )
        total_in  += rewrite_resp.usage.input_tokens
        total_out += rewrite_resp.usage.output_tokens

        rewritten = next((b.text for b in rewrite_resp.content if hasattr(b, "text") and b.text), "").strip()
        if not rewritten:
            failed.add(i)
        elif _NOT_IN_WIKI_PATTERN.search(rewritten):
            # Some markers still remain — partial fill is still useful; mark as gap for the unfilled ones
            log.info("  Step 4: partial rewrite for step %d (%d markers remain) — keeping rewritten version, marking as gap",
                     i, len(_NOT_IN_WIKI_PATTERN.findall(rewritten)))
            result[i] = rewritten if rewritten != step else step
            failed.add(i)
        elif rewritten != step:
            log.info("  Step 4: rewrote step %d (%d→%d chars)", i, len(step), len(rewritten))
            result[i] = rewritten

    return result, (total_in, total_out), failed


def scan_text_gaps(
    steps: list[str],
    clinical_context: str,
    kb: "KBConfig",
    model: str | None = None,
    source_name: str = "text-gap-scan",
    force_gap_step_texts: set[str] | None = None,
) -> list[str]:
    """
    Scan immediate_next_steps for '(value not in wiki)' markers, extract structured
    KGs via LLM, then for each candidate verify whether the specific missing values
    are actually present in the wiki section (not just whether the section exists).
    Only skips if the values are explicitly stated. Writes gap files for all others.

    force_gap_step_texts: steps that Step 4 already tried and failed to rewrite.
    Content verification is bypassed for gaps extracted from these steps — the
    wiki clearly doesn't have the specific value, so always file the KG.

    Returns list of gap file paths written.
    """
    flagged = [s for s in steps if _NOT_IN_WIKI_PATTERN.search(s)]
    if not flagged:
        return []

    log.info("scan_text_gaps: %d flagged steps to process", len(flagged))
    llm = get_llm_client(model=model)

    steps_text = "\n".join(f"- {s}" for s in flagged)
    prompt = (
        f"Clinical context: {clinical_context}\n\n"
        "The following clinical action steps are missing specific numeric values — doses, "
        "settings, targets, or rates. They contain phrases like 'value not in wiki', "
        "'per local protocol', 'consult local protocol', or similar placeholders.\n\n"
        f"{steps_text}\n\n"
        "For each missing value, call extract_gaps with one entry per distinct entity+value. "
        "Be specific: name the exact drug/device/parameter, the wiki section it belongs to, "
        "a precise question to resolve it, and the exact values needed. "
        f"IMPORTANT: in missing_values, always include the clinical context from above "
        f"('{clinical_context[:80]}...') — e.g. 'Lactulose dose for hepatic encephalopathy', "
        f"not just 'Lactulose dose'."
    )

    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[_SCAN_GAPS_TOOL],
        system="You are a clinical knowledge gap analyst. Extract structured gaps from clinical action steps that are missing specific values.",
        max_tokens=2000,
        force_tool=True,
        thinking_budget=0,
    )

    tool_input = next(
        (b.input for b in response.content if hasattr(b, "input")),
        None,
    )
    if not tool_input:
        log.warning("scan_text_gaps: LLM returned no tool call")
        return []

    raw_gaps = tool_input.get("gaps", [])
    log.info("scan_text_gaps: LLM extracted %d gap candidates", len(raw_gaps))

    _forced = force_gap_step_texts or set()

    from .canonical_registry import resolve as _resolve_canonical
    gap_entries: list[dict] = []
    for g in raw_gaps:
        entity = g.get("entity", "").strip()
        section = g.get("section_heading", "").strip()
        missing_values = g.get("missing_values") or []
        if not entity or not section:
            continue

        # If Step 4 already tried and failed to fill this entity's marker, skip
        # content verification — the wiki clearly doesn't have the specific value.
        step4_failed = any(entity.lower() in s.lower() for s in _forced)

        # Check whether the specific values are explicitly in the wiki section.
        # A section existing with generic content is NOT sufficient to skip.
        section_text = _get_section_text(entity, section, kb.wiki_dir)
        if section_text and not step4_failed:
            if _values_present_in_section(section_text, missing_values, llm, entity=entity):
                log.info(
                    "  scan_text_gaps: skipping '%s / %s' — specific values confirmed in wiki",
                    entity, section,
                )
                continue
            else:
                log.info(
                    "  scan_text_gaps: '%s / %s' — page exists but specific values missing → filing gap",
                    entity, section,
                )
        elif step4_failed:
            log.info(
                "  scan_text_gaps: '%s / %s' — Step 4 already failed to fill → filing gap (bypassing content check)",
                entity, section,
            )
        else:
            log.info(
                "  scan_text_gaps: '%s / %s' — no wiki page found → filing gap",
                entity, section,
            )

        page = _resolve_canonical(entity, kb.wiki_dir, llm)
        gap_entries.append({
            "page": page,
            "missing_sections": [section],
            "resolution_question": g.get("resolution_question", ""),
            "missing_values": missing_values,
            "subtype": g.get("subtype", "default"),
            "placement": "confirmed" if section_text else "approximate",
            "_entity": entity,
        })

    if not gap_entries:
        log.info("scan_text_gaps: no new gaps after content verification")
        return []

    try:
        written = write_gap_files(gap_entries, kb, source_name=source_name)
        log.info("scan_text_gaps: wrote %d gap files: %s", len(written), written)
        return written
    except Exception as exc:
        log.warning("scan_text_gaps: write_gap_files failed: %s", exc)
        return []


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
                        "missing_values": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Only populate when grounded=false. List the specific data points or values "
                                "that were needed but not found in the wiki. CRITICAL RULES: "
                                "(1) Each drug gets its OWN entry — never group multiple drugs. "
                                "Write 'Etomidate induction dose for RSI (mg/kg IV)' and "
                                "'Rocuronium dose for RSI (mg/kg IV)' as two separate items, NOT "
                                "'RSI pre-medication drugs'. "
                                "(2) Each setting gets its OWN entry — 'Initial IPAP for BiPAP in "
                                "hypercapnic failure', 'Initial EPAP for BiPAP', 'SpO2 target on NIV' "
                                "as three separate items. "
                                "(3) Always name the specific drug, device, or parameter — never use "
                                "vague terms like 'induction agents' or 'RSI protocol'. "
                                "These drive what gets written when the gap is filled."
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
                    "Use ONLY wiki-grounded values for specific numeric parameters. "
                    "If a value was not found in the wiki, state the clinical action WITHOUT the specific number "
                    "and add exactly the text '(value not in wiki — consult local protocol)' — "
                    "never paraphrase this as 'per local protocol', 'doses per local protocol', or any other variant. "
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
Also list specific clinical parameters where precise values matter (drug doses, titration steps, target ranges, protocol thresholds).
These will be looked up in the medical wiki separately.

QUERY RULES — read carefully:
- Generate ONE query per drug: dose, route, rate, and renal/hepatic adjustment each get their own entry.
- Generate ONE query per procedure or device: each specific setting (e.g. TV, PEEP, FiO2 for ventilation; IPAP, EPAP, SpO2 target for NIV) is a separate entry.
- Do NOT group multiple drugs or procedures into one query.
- Keep each search_query short and focused on the clinical concept only — no patient-specific details.
- If a clinical direction involves both a drug AND a procedure, generate queries for both independently."""

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
        score_by_param: dict = {}   # parameter name → top retrieval score from Step 2

        if specific_queries:
            param_contexts = []
            for sq in specific_queries:
                # ── Multi-query retrieval: use both search_query and parameter name ──
                queries_to_run = list(dict.fromkeys([sq["search_query"], sq["parameter"]]))
                raw_hit_lists = [
                    vs_mod.search_sections(q, kb.wiki_dir, top_k=10, include_patients=include_patient_context)
                    for q in queries_to_run
                ]
                hits = _merge_hits(raw_hit_lists)
                if exclude_pattern:
                    hits = [h for h in hits if exclude_pattern.lower() not in h["path"].lower()]
                hits = hits[:6]

                # ── Weak-score fallback: retry with shorter query ──────────────────
                if not hits or hits[0]["score"] < _MIN_RETRIEVAL_SCORE:
                    short_q = " ".join(sq["search_query"].split()[:3])
                    if short_q and short_q != sq["search_query"]:
                        fallback = vs_mod.search_sections(short_q, kb.wiki_dir, top_k=6, include_patients=include_patient_context)
                        if exclude_pattern:
                            fallback = [h for h in fallback if exclude_pattern.lower() not in h["path"].lower()]
                        hits = _merge_hits([hits, fallback])[:6]
                        log.info("  Fallback retrieval %r → top score %.2f", short_q, hits[0]["score"] if hits else 0)

                top_score = hits[0]["score"] if hits else 0.0
                log.info("  Retrieval %r+%r → %d hits  top=%.2f", sq["search_query"], sq["parameter"], len(hits),
                         top_score)

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
                        label = f"{page_path} — {hit['section']} (score={hit['score']:.2f})" if hit["section"] else page_path
                        param_page_texts.append(f"### {label}\n\n{section_text}")

                # ── Link traversal: one hop, best matching section per linked page ─
                for linked_path in _traverse_links(seed_paths, kb.wiki_dir, max_additional=3):
                    full_path = kb.wiki_dir / linked_path
                    if full_path.exists():
                        linked_sections = vs_mod._split_sections(linked_path, full_path.read_text(encoding="utf-8"))
                        if linked_sections:
                            best = _best_section_for_query(linked_sections, sq["search_query"])
                            param_page_texts.append(
                                f"### {linked_path} — {best['heading']} [linked]\n\n{best['body'][:800]}"
                            )

                param_contexts.append({
                    "parameter": sq["parameter"],
                    "context": "\n\n---\n\n".join(param_page_texts) if param_page_texts else "(no relevant wiki pages found)",
                    "top_score": top_score,
                })

            # Index retrieval score by parameter name so we can gate the pre-write check
            score_by_param = {pc["parameter"]: pc["top_score"] for pc in param_contexts}

            # Build grounding prompt with per-parameter wiki context + score hint
            grounding_parts = [
                "For each parameter below, check the provided wiki context.\n"
                "Set grounded=true if the wiki contains a value a clinician could directly apply "
                "to this situation — even if the exact patient setting (ICU, ward, ED) is not "
                "explicitly mentioned. A general drug dose is grounded unless the clinical context "
                "specifically changes the value (e.g. renal failure requiring dose reduction, "
                "hepatic impairment, paediatric weight-based dosing, a setting-specific protocol "
                "that genuinely differs from the general dose). "
                "Only set grounded=false if the actual numeric value, range, or clinical guidance "
                "is completely absent from the wiki — not merely because the page doesn't say "
                "'ICU-specific' or 'in the ICU'.\n"
                "The retrieval score (0–1) indicates how closely the wiki matched the query — "
                "be more skeptical of grounding when the score is below 0.65. "
                "Do NOT fabricate; set grounded=false if uncertain.\n\n"
                "GROUNDING DECISION EXAMPLES:\n"
                "✓ GROUNDED — wiki has 'ondansetron 4 mg IV', query is antiemetic dose in ICU: "
                "same drug, same dose, ICU setting does not change it.\n"
                "✓ GROUNDED — wiki has 'noradrenaline 0.01–3 mcg/kg/min', query is vasopressor "
                "starting dose in septic shock: general ICU dose applies.\n"
                "✓ GROUNDED — wiki has 'PEEP 5 cmH₂O initial', query is PEEP setting on "
                "mechanical ventilation: value is present even if ARDS-specific nuance is absent.\n"
                "✗ NOT GROUNDED — wiki has no dose at all for this drug: value genuinely missing.\n"
                "✗ NOT GROUNDED — wiki has standard morphine dose but patient has eGFR 15: "
                "renal failure changes the dose — context matters clinically.\n"
                "✗ NOT GROUNDED — wiki mentions IPAP/EPAP exist but gives no numeric starting "
                "values: the number is absent, not just the setting label.\n"
            ]
            for pc in param_contexts:
                score_note = f"  [retrieval score: {pc['top_score']:.2f}]" if pc.get("top_score") else ""
                grounding_parts.append(
                    f"PARAMETER: {pc['parameter']}{score_note}\nWIKI CONTEXT:\n{pc['context']}\n"
                )

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

        # ── Metric 1: record a CDS query hit for every page consulted ────────
        if pages_consulted:
            try:
                from .page_metrics import record_query as _record_query
                for _p in pages_consulted:
                    _record_query(_p, kb.wiki_dir)
                log.debug("page_metrics: recorded CDS query for %d page(s)", len(pages_consulted))
            except Exception as _me:
                log.debug("page_metrics: record_query failed (non-fatal): %s", _me)

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
                "- Use ONLY wiki-grounded values for specific numeric parameters. "
                "If a value is not wiki-grounded, write the clinical action but replace the specific "
                "value with '(value not in wiki — consult local protocol)'. "
                "Do NOT invent or estimate values from clinical training.\n"
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

        # Step 4: rewrite any remaining markers where the wiki actually has the value
        _step4_failed_texts: set[str] = set()
        if immediate_next_steps:
            steps_before_rewrite = list(immediate_next_steps)
            immediate_next_steps, rewrite_tokens, failed_idx = _rewrite_grounded_steps(
                immediate_next_steps, kb, llm
            )
            total_input  += rewrite_tokens[0]
            total_output += rewrite_tokens[1]
            # Collect the original text of steps Step 4 couldn't resolve
            _step4_failed_texts = {steps_before_rewrite[i] for i in failed_idx if i < len(steps_before_rewrite)}

        # Register gap files for ungrounded parameters.
        # Pre-write vector check is only run when Step 2 retrieval score was LOW (<0.60)
        # meaning the wiki page wasn't found. If score ≥ 0.60 and still grounded=false,
        # the page exists but specific values are missing — that's a definite gap.
        # placement confidence: score ≥ 0.70 → confirmed; below → approximate.
        # Approximate gaps are valid knowledge gaps but the section assignment may be
        # imprecise — defrag will correct placement after a scope-contamination scan.
        _LOW_RETRIEVAL    = 0.60
        _CONFIRM_RETRIEVAL = 0.70
        gap_entity = ""
        gap_sections: list = []
        gap_written = None
        ungrounded = [p for p in specific_parameters if not p.get("grounded", True)]
        if ungrounded:
            gap_entries: list = []
            from .canonical_registry import resolve as _resolve_canonical
            for param in ungrounded:
                concept = param.get("entity") or param["parameter"]
                section = param.get("section_heading") or "Dosing"
                retrieval_score = score_by_param.get(param["parameter"], 0.0)
                # Only run pre-write check when Step 2 retrieval was weak (page may have been missed)
                if retrieval_score < _LOW_RETRIEVAL:
                    if _gap_already_covered(concept, section, kb.wiki_dir):
                        log.info(
                            "  Skipping gap '%s / %s' — pre-write check: already covered (retrieval_score=%.2f)",
                            concept, section, retrieval_score,
                        )
                        continue
                else:
                    log.info(
                        "  Gap '%s / %s' confirmed — page found (score=%.2f) but values missing",
                        concept, section, retrieval_score,
                    )
                placement = "confirmed" if retrieval_score >= _CONFIRM_RETRIEVAL else "approximate"
                if placement == "approximate":
                    log.info(
                        "  Gap '%s / %s' flagged approximate (score=%.2f) — section may be imprecise",
                        concept, section, retrieval_score,
                    )
                entry = {
                    "page": _resolve_canonical(concept, kb.wiki_dir, llm),
                    "missing_sections": [section],
                    "placement": placement,
                    "_param": param["parameter"],  # keep for metadata after write
                }
                if param.get("resolution_question"):
                    entry["resolution_question"] = param["resolution_question"]
                if param.get("missing_values"):
                    entry["missing_values"] = param["missing_values"]
                if param.get("subtype"):
                    entry["subtype"] = param["subtype"]
                gap_entries.append(entry)
            if gap_entries:
                try:
                    written = write_gap_files(gap_entries, kb, source_name=f"ungrounded cds params: {question[:80]}")
                    gap_written = written[0] if written else None
                    # Report only what was actually filed
                    gap_entity   = gap_entries[0]["_param"] if gap_entries else ""
                    gap_sections = [e["_param"] for e in gap_entries]
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
            "=== CHAT END (cds)  direction=%d  params=%d(ungrounded=%d)  gap=%s  "
            "markers_remaining=%d  in=%d  out=%d  cost=$%.4f ===",
            len(clinical_direction), len(specific_parameters), len(ungrounded),
            gap_written,
            sum(1 for s in immediate_next_steps if _NOT_IN_WIKI_PATTERN.search(s)),
            total_input, total_output, cost,
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
            "_step4_failed_texts": _step4_failed_texts,
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
