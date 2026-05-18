"""
Mop-up pipeline — periodic cleanup of stub and thin wiki pages.

Two-phase process:
  Phase 1 — Stub expansion
    Score all entity/concept pages by (cds_query_count + inbound_links × 2).
    Pages with fewer than WORD_THRESHOLD words and score above SCORE_THRESHOLD
    are queued. For each, MedGemma generates the missing template sections.
    Sections are merged into the page; open gap files are closed; the page is
    re-embedded in the vector store.

  Phase 2 — Defrag
    Run defrag_all() to resolve any scope_contamination flags.  Defrag already
    handles gap-index safety internally (calls resolve_gap_sections on target
    pages), so no extra plumbing is needed here.

After both phases: sync_index + activity.jsonl entry.

Invoked via POST /api/mopup/run (see routers/mopup.py).
"""

from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from ..config import KBConfig, KG_FALLBACK_MODEL, _default_kb
from .defrag_pipeline import defrag_all
from .index_sync import sync_index
from .ingest_pipeline import resolve_gap_sections
from .llm_client import get_llm_client
from .page_templates import TEMPLATES, get_template
from .quality_scorer import _parse_subtype, parse_scope, update_quality_frontmatter
from .token_tracker import log_call
from . import vector_store as vs_mod

log = logging.getLogger("wiki.mopup")

WORD_THRESHOLD  = 300   # pages below this are stubs
SCORE_THRESHOLD = 20    # composite score floor — low-traffic stubs aren't worth the cost
MAX_STUBS       = 50    # safety cap per run


# ── Scoring ───────────────────────────────────────────────────────────────────

def _compute_scores(wiki_dir: Path) -> list[dict]:
    """
    Score every entity/concept page by composite importance.
    Returns a list sorted by score descending, each entry:
      { rel, title, subtype, scope, score, cds, inbound, words }
    """
    from .page_metrics import get_all as get_metrics

    pm = get_metrics(wiki_dir)

    # Build inbound-link counts from the entire wiki (including patients)
    inbound: dict[str, int] = defaultdict(int)
    for root, _dirs, files in os.walk(wiki_dir):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            try:
                content = Path(os.path.join(root, fn)).read_text(encoding="utf-8")
                for link in re.findall(r"\[\[([^\]|#]+)", content):
                    inbound[link.strip()] += 1
            except OSError:
                pass

    results: list[dict] = []
    for section in ("entities", "concepts"):
        section_dir = wiki_dir / section
        if not section_dir.exists():
            continue
        for md_path in sorted(section_dir.glob("*.md")):
            rel = f"{section}/{md_path.name}"
            try:
                content = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            # CDS query count
            pm_entry = pm.get(rel, {})
            cds = pm_entry.get("cds_query_count", 0) if isinstance(pm_entry, dict) else 0

            # Inbound links — match by title OR slug
            slug  = md_path.stem
            title_m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
            title = title_m.group(1).strip().strip("\"'") if title_m else slug

            if title.lower() != slug.lower():
                inb = inbound.get(title, 0) + inbound.get(slug, 0)
            else:
                inb = max(inbound.get(title, 0), inbound.get(slug, 0))

            score = cds + inb * 2

            # Body word count (strip frontmatter)
            body  = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, flags=re.DOTALL)
            words = len(body.split())

            raw_subtype = _parse_subtype(content)
            scope       = parse_scope(content)

            results.append({
                "rel":              rel,
                "title":            title,
                "subtype":          raw_subtype,
                "subtype_inferred": raw_subtype == "default",  # will be LLM-classified at run time
                "scope":            scope,
                "score":            score,
                "cds":              cds,
                "inbound":          inb,
                "words":            words,
            })

    results.sort(key=lambda x: -x["score"])
    return results


_VALID_SUBTYPES = {"medication", "parameter", "investigation", "procedure", "condition", "default"}

_SUBTYPE_CLASSIFY_SYSTEM = """\
You are classifying medical wiki pages into structural types.
Respond with EXACTLY ONE word from this list — nothing else:
  medication    — drugs, biologics, IV fluids used as therapy, infusions
  parameter     — physiological measurement or ventilator/monitor setting (SpO2, PEEP, MAP, eGFR, PaCO2)
  investigation — diagnostic tests: labs, imaging, ECG, cultures, biopsies
  procedure     — bedside procedures or interventions (intubation, central line, bronchoscopy)
  condition     — diseases, clinical syndromes, physiological states (sepsis, ARDS, AKI)
  default       — anything else (scoring tools, protocols, care bundles)
"""


def _infer_subtype(title: str, scope: str) -> str:
    """Fast heuristic subtype inference — used during scoring (no LLM call)."""
    scope_lower = scope.lower()
    if any(kw in scope_lower for kw in (
        "pharmacology", "mechanism of action", "dosing", "adverse effects",
        "contraindication", "drug interaction", "infusion", "biologic",
    )):
        return "medication"
    if any(kw in scope_lower for kw in (
        "normal range", "reference range", "measured", "ventilator parameter",
        "haemodynamic", "hemodynamic",
    )):
        return "parameter"
    if any(kw in scope_lower for kw in (
        "pathophysiology", "diagnostic criteria", "clinical syndrome", "disease",
    )):
        return "condition"

    t = title.lower().replace(" ", "").replace("-", "")
    drug_endings = (
        "mide", "zine", "pine", "dine", "rine", "mine", "zole",
        "olol", "pril", "artan", "statin", "mycin", "cillin", "cycline",
        "pam", "lam", "zepam", "mab", "nib", "tide", "orphine",
    )
    if any(t.endswith(e) for e in drug_endings):
        return "medication"

    return "default"


def infer_subtype_llm(title: str, scope: str) -> str:
    """LLM-based subtype classification via MedGemma. Called on demand only."""
    prompt = (
        f"Page title: {title}\n"
        + (f"Scope hint: {scope}\n" if scope else "")
        + "\nWhat is the subtype? Reply with one word only."
    )
    try:
        llm = get_llm_client(model=KG_FALLBACK_MODEL)
        resp = llm.create_message(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            system=_SUBTYPE_CLASSIFY_SYSTEM,
            max_tokens=10,
            force_tool=False,
            thinking_budget=0,
        )
        raw = (
            next(
                (b.text for b in resp.content if hasattr(b, "text") and b.text), ""
            ).strip().lower().split()[0]
            if resp.content
            else "default"
        )
        result = raw if raw in _VALID_SUBTYPES else "default"
        log_call(
            operation="mopup_subtype_classify",
            source_name=title[:80],
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=KG_FALLBACK_MODEL,
        )
        log.info("mopup: subtype classify (llm)  title=%r  raw=%r  result=%s", title, raw, result)
        return result
    except Exception as exc:
        log.warning("mopup: subtype classify (llm) failed for %r: %s", title, exc)
        return "default"


def get_stub_queue(
    wiki_dir: Path,
    score_threshold: int = SCORE_THRESHOLD,
    word_threshold:  int = WORD_THRESHOLD,
    max_stubs:       int = MAX_STUBS,
) -> list[dict]:
    """Return up to max_stubs stub pages sorted by importance."""
    all_pages = _compute_scores(wiki_dir)
    return [
        p for p in all_pages
        if p["words"] < word_threshold and p["score"] >= score_threshold
    ][:max_stubs]


# ── Manual subtype override ───────────────────────────────────────────────────

def set_page_subtype(page_rel: str, subtype: str, kb: "KBConfig") -> None:
    """Write the given subtype into the page's frontmatter, overwriting any existing value."""
    if subtype not in _VALID_SUBTYPES:
        raise ValueError(f"Unknown subtype {subtype!r}")
    from .page_templates import inject_subtype_frontmatter
    path = kb.wiki_dir / page_rel
    content = path.read_text(encoding="utf-8")
    content = inject_subtype_frontmatter(content, subtype)
    path.write_text(content, encoding="utf-8")
    log.info("mopup: manual subtype set  page=%r  subtype=%s", page_rel, subtype)


# ── Prompt building ───────────────────────────────────────────────────────────

_SYSTEM = """\
You are a medical reference author maintaining a structured clinical knowledge base \
for ICU physicians.

Rules:
- Write generalizable, evidence-based clinical knowledge — NOT case notes or \
patient-specific answers
- Use ## Section Heading format exactly as requested
- Use Markdown tables wherever the instructions say to tabulate
- Use [[Page Title]] wiki links when referencing related concepts \
(e.g. [[Acute Kidney Injury]], [[Septic Shock]])
- Include specific numeric values: doses, ranges, thresholds, intervals
- Write 2–4 paragraphs of prose per section unless a table is requested
- Cite guidelines by name where known (e.g. KDIGO, SSC, NICE, ARDSNet) — \
no full references needed
- If something is unknown or not established, say so briefly — do not fabricate
- Scope constraint: {scope}
  Stay strictly within this scope. If a topic belongs on a different page, \
write one line: "See [[Target Page]] for details." and move on.
- Today: {today}
"""

_MEDICATION_SECTIONS = {
    "Mechanism of Action": (
        "Explain the pharmacological mechanism. Include receptor targets, pathway effects, "
        "and how the mechanism explains the clinical effects seen in ICU patients. "
        "Write 2–4 paragraphs of prose."
    ),
    "Indications": (
        "List the main clinical indications relevant to ICU patients. "
        "Write as prose or a short bulleted list."
    ),
    "Dosing": (
        "Standard adult IV/oral dosing. Include:\n"
        "- Usual dose range\n"
        "- Titration approach if applicable\n"
        "- Preparation/concentration (for IV drugs)\n"
        "- Route and rate of administration\n"
        "Write as prose with specific numbers."
    ),
    "Renal Dose Adjustment": (
        "Provide as a Markdown table with columns:\n"
        "| eGFR (mL/min) | Dose Adjustment | Notes |\n"
        "Cover bands: >60, 30–60, 15–30, <15, Dialysis (CRRT and IHD separately if different). "
        "If no adjustment is needed, state that in one line and omit the table."
    ),
    "Hepatic Dose Adjustment": (
        "Provide as a Markdown table with columns:\n"
        "| Child-Pugh Class | Dose Adjustment | Notes |\n"
        "Cover: Class A (mild), Class B (moderate), Class C (severe). "
        "If no adjustment is needed, state that in one line and omit the table."
    ),
    "Pediatric Dosing": (
        "Provide as a Markdown table with columns:\n"
        "| Age / Weight Band | Dose | Route | Notes |\n"
        "Cover neonates, infants, children, adolescents where data exist. "
        "If pediatric use is not established or data are unavailable, state this clearly."
    ),
    "Drug Interactions": (
        "Provide as a Markdown table with columns:\n"
        "| Interacting Drug / Class | Mechanism | Clinical Effect | Management |\n"
        "List the 5–8 most clinically relevant interactions for ICU patients."
    ),
    "Monitoring Parameters": (
        "List the key parameters to monitor during use (labs, vitals, ECG, clinical signs). "
        "Include frequency and thresholds that should prompt dose change or cessation."
    ),
    "Adverse Effects": (
        "Cover the major adverse effects. Distinguish common from serious/rare. "
        "For each serious adverse effect note onset timing, monitoring parameters, "
        "and management. Write as prose (2–3 paragraphs)."
    ),
    "Contraindications": (
        "List absolute and relative contraindications relevant to the ICU. "
        "Write as a short bulleted list or prose."
    ),
    "ICU Considerations": (
        "Summarise the practical ICU-specific points:\n"
        "- When to use / avoid\n"
        "- Monitoring requirements\n"
        "- Common pitfalls\n"
        "- Any organ-failure-specific cautions not covered above\n"
        "Write as prose (2–3 paragraphs)."
    ),
}

_PARAMETER_SECTIONS = {
    "Definition": (
        "What this parameter measures, what it represents physiologically, "
        "and how it is derived or calculated if applicable. Write 1–2 paragraphs."
    ),
    "Normal Ranges": (
        "Provide as a Markdown table with columns:\n"
        "| Population | Normal Range | Units | Notes |\n"
        "Cover: healthy adults, mechanically ventilated patients, elderly, "
        "and any other clinically relevant subgroups."
    ),
    "Clinical Significance": (
        "Explain what abnormal values indicate. Cover both high and low derangements "
        "with their clinical implications. Link to related conditions using [[wiki links]]. "
        "Write 2–3 paragraphs."
    ),
    "Measurement": (
        "How is this parameter measured in the ICU? "
        "For ventilator parameters: how is the target set and titrated? "
        "Include monitoring frequency and reliability caveats. Write 1–2 paragraphs."
    ),
    "ICU Considerations": (
        "Practical points:\n"
        "- Common causes of derangement in ICU patients\n"
        "- Targets in specific conditions (e.g. SpO2 target in ARDS vs COPD)\n"
        "- Pitfalls and artefacts\n"
        "- Interactions with other ventilator/haemodynamic parameters\n"
        "Write 2–3 paragraphs."
    ),
}

_CONDITION_SECTIONS = {
    "Pathophysiology": (
        "Explain the underlying mechanism. Focus on aspects most relevant to ICU management "
        "and organ dysfunction. Link to related entities using [[wiki links]]. "
        "Write 2–4 paragraphs."
    ),
    "Diagnosis": (
        "Provide diagnostic criteria as a Markdown table where applicable:\n"
        "| Criterion / Score | Threshold | Notes |\n"
        "Include clinical, laboratory, and imaging criteria. "
        "Mention relevant scoring tools (e.g. SOFA, Ranson, West Haven) with brief explanation."
    ),
    "Management": (
        "Summarise evidence-based management. "
        "Structure by phase if appropriate (resuscitation → definitive → maintenance). "
        "Include specific targets, doses, and guideline references (e.g. Surviving Sepsis Campaign). "
        "Use [[wiki links]] for all drugs and interventions mentioned. Write 3–5 paragraphs."
    ),
    "ICU Considerations": (
        "- Common complications and how to anticipate them\n"
        "- Organ support decisions (when to start RRT, ventilation thresholds, etc.)\n"
        "- Monitoring parameters and frequency\n"
        "- Common pitfalls and controversies\n"
        "Write 2–3 paragraphs."
    ),
}

_DEFAULT_SECTIONS = {
    "Definition": (
        "Define this entity and its role in ICU care. Write 1–2 paragraphs."
    ),
    "Clinical Significance": (
        "Explain the clinical relevance in ICU patients. Write 2–3 paragraphs."
    ),
    "Management": (
        "Describe the management approach. Include specific targets or doses. "
        "Write 2–3 paragraphs."
    ),
    "Monitoring": (
        "Describe what to monitor, how often, and what thresholds matter. "
        "Write 1–2 paragraphs."
    ),
}

_SECTION_INSTRUCTIONS: dict[str, dict[str, str]] = {
    "medication": _MEDICATION_SECTIONS,
    "parameter":  _PARAMETER_SECTIONS,
    "condition":  _CONDITION_SECTIONS,
    "default":    _DEFAULT_SECTIONS,
    # investigation and procedure fall back to default instructions
}


def _build_prompts(
    title:    str,
    subtype:  str,
    scope:    str,
    missing:  list[str],
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for MedGemma to fill missing sections."""
    instructions = _SECTION_INSTRUCTIONS.get(subtype, _DEFAULT_SECTIONS)
    scope_text   = scope or f"{title} — general clinical reference for ICU use."

    system = _SYSTEM.format(scope=scope_text, today=date.today().isoformat())

    section_lines = []
    for sec in missing:
        instruction = instructions.get(sec, f"Write a comprehensive paragraph about {sec}.")
        section_lines.append(f"## {sec}\n{instruction}\n")

    user = (
        f"Write the following sections for the wiki page: **{title}**\n\n"
        f"Output ONLY the sections listed below — nothing else (no introduction, "
        f"no summary, no frontmatter). Start directly with the first ## heading.\n\n"
        + "\n".join(section_lines)
    )
    return system, user


# ── Section parsing and merging ───────────────────────────────────────────────

def _parse_generated_sections(text: str) -> dict[str, str]:
    """
    Parse MedGemma output into {heading: full_block_including_heading}.
    Returns empty dict if no ## headings are found.
    """
    lines  = text.splitlines()
    result: dict[str, str] = {}
    current_heading: Optional[str] = None
    current_lines:   list[str]     = []

    for line in lines:
        if line.startswith("## "):
            if current_heading is not None:
                result[current_heading] = "\n".join(current_lines).rstrip()
            current_heading = line[3:].strip()
            current_lines   = [line]
        elif current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        result[current_heading] = "\n".join(current_lines).rstrip()

    return result


def _existing_headings(content: str) -> set[str]:
    return {m.lower() for m in re.findall(r"^## (.+)$", content, re.MULTILINE)}


def _merge_into_page(existing: str, new_sections: dict[str, str]) -> str:
    """Append new section blocks to the page. Skips any heading already present."""
    present = _existing_headings(existing)
    additions = [
        block for heading, block in new_sections.items()
        if heading.lower() not in present
    ]
    if not additions:
        return existing
    return existing.rstrip() + "\n\n" + "\n\n".join(additions) + "\n"


def _update_frontmatter_after_mopup(
    content:      str,
    new_sections: list[str],
    subtype:      str,
) -> str:
    """
    Inject mopup-generated section_quality scores and bump the updated date.
    Medications get score 2 (needs human review for dosing numbers).
    Parameters and conditions get score 3.
    """
    score = 2 if subtype == "medication" else 3
    flags = ["mopup_generated"]
    sq    = {sec: {"score": score, "flags": flags} for sec in new_sections}
    content = update_quality_frontmatter(content, sq)

    # Bump updated date
    today = date.today().isoformat()
    if re.search(r"^updated:", content, re.MULTILINE):
        content = re.sub(r"^updated:.*$", f"updated: {today}", content, flags=re.MULTILINE)
    return content


# ── Single-page expand ────────────────────────────────────────────────────────

def expand_stub(page_rel: str, kb: KBConfig) -> dict:
    """
    Expand a single stub page using MedGemma.
    Returns a result dict with keys: page, sections_added, skipped, error.
    """
    page_path = kb.wiki_dir / page_rel.replace("wiki/", "", 1)
    if not page_path.exists():
        return {"page": page_rel, "sections_added": [], "skipped": True, "error": "file not found"}

    content = page_path.read_text(encoding="utf-8")
    subtype = _parse_subtype(content)
    scope   = parse_scope(content)

    title_m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
    title   = title_m.group(1).strip().strip("\"'") if title_m else page_path.stem

    # Auto-correct subtype: default is often wrong for drug pages
    if subtype == "default":
        inferred = _infer_subtype(title, scope)
        if inferred != "default":
            log.info("mopup: %s — inferred subtype %s (was default)", page_rel, inferred)
            from .page_templates import inject_subtype_frontmatter
            content = inject_subtype_frontmatter(content, inferred)
            subtype = inferred

    template  = get_template(subtype)
    present   = _existing_headings(content)
    missing   = [s for s in template if s.lower() not in present]

    if not missing:
        log.info("mopup: %s — no missing sections, skipping", page_rel)
        return {"page": page_rel, "sections_added": [], "skipped": True, "error": None}

    log.info("mopup: expanding %s  subtype=%s  missing=%s", page_rel, subtype, missing)

    system_prompt, user_prompt = _build_prompts(title, subtype, scope, missing)

    llm  = get_llm_client(model=KG_FALLBACK_MODEL)
    resp = llm.create_message(
        messages=[{"role": "user", "content": user_prompt}],
        tools=[],
        system=system_prompt,
        max_tokens=4000,
        force_tool=False,
        thinking_budget=0,
    )

    raw_text = next(
        (b.text for b in resp.content if hasattr(b, "text") and b.text), ""
    ).strip()

    tokens_in  = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    log_call(
        operation="mopup_stub_expand",
        source_name=title[:80],
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        model=KG_FALLBACK_MODEL,
    )

    if len(raw_text) < 100:
        log.warning("mopup: %s — MedGemma response too short (%d chars)", page_rel, len(raw_text))
        return {"page": page_rel, "sections_added": [], "skipped": False, "error": "response too short"}

    new_sections = _parse_generated_sections(raw_text)
    if not new_sections:
        log.warning("mopup: %s — could not parse any ## sections from response", page_rel)
        return {"page": page_rel, "sections_added": [], "skipped": False, "error": "no sections parsed"}

    added_headings = list(new_sections.keys())
    updated_content = _merge_into_page(content, new_sections)
    updated_content = _update_frontmatter_after_mopup(updated_content, added_headings, subtype)

    page_path.write_text(updated_content, encoding="utf-8")
    log.info("mopup: wrote %s  sections_added=%s", page_rel, added_headings)

    # Close / partially resolve any open gap for this page
    gap_rel = f"wiki/gaps/{page_path.stem}.md"
    if (kb.wiki_root / gap_rel).exists():
        try:
            resolve_gap_sections(gap_rel, added_headings, kb)
            log.info("mopup: updated gap %s", gap_rel)
        except Exception as exc:
            log.warning("mopup: gap update failed for %s: %s", gap_rel, exc)

    # Re-embed in vector store
    section_part = page_rel.split("/")[0] if "/" in page_rel else ""
    vs_rel = f"{section_part}/{page_path.name}" if section_part else page_rel
    vs_mod.upsert(vs_rel, updated_content, kb.wiki_dir)
    vs_mod.upsert_sections(vs_rel, updated_content, kb.wiki_dir)

    return {
        "page":           page_rel,
        "sections_added": added_headings,
        "skipped":        False,
        "error":          None,
    }


# ── Full mop-up run ───────────────────────────────────────────────────────────

def mopup_run(
    kb:              Optional[KBConfig] = None,
    score_threshold: int  = SCORE_THRESHOLD,
    word_threshold:  int  = WORD_THRESHOLD,
    max_stubs:       int  = MAX_STUBS,
    run_defrag:      bool = True,
) -> dict:
    """
    Run the full mop-up pipeline:
      1. Expand stub pages via MedGemma
      2. (Optionally) run defrag_all for scope contamination
      3. sync_index
      4. Append to activity.jsonl

    Returns a summary dict suitable for the API response.
    """
    if kb is None:
        kb = _default_kb()

    log.info(
        "=== MOPUP START  score_threshold=%d  word_threshold=%d  max_stubs=%d  defrag=%s ===",
        score_threshold, word_threshold, max_stubs, run_defrag,
    )

    # ── Phase 1: stub expansion ───────────────────────────────────────────────
    queue    = get_stub_queue(kb.wiki_dir, score_threshold, word_threshold, max_stubs)
    log.info("mopup: stub queue size=%d", len(queue))

    stub_results: list[dict] = []
    files_written: list[str] = []

    for page in queue:
        result = expand_stub(page["rel"], kb)
        stub_results.append(result)
        if result["sections_added"]:
            files_written.append(page["rel"])

    stubs_expanded = sum(1 for r in stub_results if r["sections_added"])
    stubs_skipped  = sum(1 for r in stub_results if r["skipped"])
    stubs_errored  = sum(1 for r in stub_results if r["error"] and not r["skipped"])

    log.info(
        "mopup: stub phase done  expanded=%d  skipped=%d  errored=%d",
        stubs_expanded, stubs_skipped, stubs_errored,
    )

    # ── Phase 2: defrag ───────────────────────────────────────────────────────
    defrag_summary: dict = {}
    if run_defrag:
        log.info("mopup: running defrag_all")
        defrag_summary = defrag_all(kb)
        log.info(
            "mopup: defrag done  pages=%d  moves=%d  errors=%d",
            defrag_summary.get("pages_processed", 0),
            defrag_summary.get("total_moves", 0),
            defrag_summary.get("total_errors", 0),
        )

    # ── Phase 3: housekeeping ─────────────────────────────────────────────────
    sync_index(kb.wiki_dir)

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation":     "mopup",
        "kb":            kb.name,
        "stubs_expanded": stubs_expanded,
        "stubs_skipped":  stubs_skipped,
        "stubs_errored":  stubs_errored,
        "files_written":  files_written,
        "defrag_pages":   defrag_summary.get("pages_processed", 0),
        "defrag_moves":   defrag_summary.get("total_moves", 0),
    })

    log.info("=== MOPUP COMPLETE ===")

    return {
        "stub_queue_size": len(queue),
        "stubs_expanded":  stubs_expanded,
        "stubs_skipped":   stubs_skipped,
        "stubs_errored":   stubs_errored,
        "stub_results":    stub_results,
        "defrag":          defrag_summary,
        "files_written":   files_written,
    }
