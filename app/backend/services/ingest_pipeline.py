import json
import logging
import difflib
import re
from datetime import date
from pathlib import Path
from ..config import MODEL, KBConfig, _default_kb
from ..cancellation import shutdown_event
from .token_tracker import log_call
from .llm_client import get_llm_client
from .index_sync import sync_index
from . import vector_store as vs_mod

log = logging.getLogger("wiki.ingest_pipeline")


def _embed_page(page_path: str, content: str, kb: "KBConfig") -> None:
    """Embed a single wiki page into the vector store. Errors are non-fatal."""
    from pathlib import Path as _Path
    parts = _Path(page_path).parts
    if not parts or parts[0] not in ("wiki",) or len(parts) < 3:
        return
    section = parts[1] if len(parts) >= 2 else ""
    # Patient pages (wiki/patients/{slug}/...) are indexed separately so they
    # can be toggled in/out of search without rebuilding.
    if section == "patients":
        rel = "/".join(parts[1:])  # e.g. "patients/slug/entities/foo.md"
        try:
            vs_mod.upsert(rel, content, kb.wiki_dir)
        except Exception as exc:
            log.warning("Vector upsert skipped for %s: %s", page_path, exc)
        return
    if section not in ("entities", "concepts", "sources", "queries"):
        return
    rel = "/".join(parts[1:])  # strip leading "wiki/"
    try:
        vs_mod.upsert(rel, content, kb.wiki_dir)
    except Exception as exc:
        log.warning("Vector upsert skipped for %s: %s", page_path, exc)

# ── Tool definitions ────────────────────────────────────────────────────────────

# Step 1: planning tool — lightweight, just decides what files to create
PLAN_TOOL = {
    "name": "plan_wiki_files",
    "description": "Plan all wiki file operations for this ingest. Return the list of files to create/update with their key content points. Do NOT write full content here.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-3 sentence summary of what was found in the source."
            },
            "files": {
                "type": "array",
                "description": "Plan of all files to write.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op":      {"type": "string", "enum": ["write", "append", "update"]},
                        "path":    {"type": "string", "description": "e.g. wiki/sources/foo.md"},
                        "section": {"type": "string", "description": "For update ops only"},
                        "key_points": {
                            "type": "string",
                            "description": "Newline-separated bullet points of what THIS SOURCE explicitly says about this file's topic. Do not include background knowledge."
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": (
                                "Page structural type — determines required section headings. "
                                "medication=drugs/biologics/infusions; "
                                "investigation=labs/imaging/ECG/cultures; "
                                "procedure=bedside procedures/interventions; "
                                "condition=clinical syndromes/diseases/physiological states; "
                                "default=protocols/targets/scoring tools/anything else. "
                                "Only required for wiki/entities/ and wiki/concepts/ pages."
                            )
                        }
                    },
                    "required": ["op", "path", "key_points"]
                }
            },
            "knowledge_gaps": {
                "type": "array",
                "description": "Standard knowledge areas NOT addressed by this source. List per entity/concept.",
                "items": {
                    "type": "object",
                    "properties": {
                        "page": {
                            "type": "string",
                            "description": "The entity/concept page path (e.g. wiki/entities/furosemide.md)"
                        },
                        "missing_sections": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Standard sections this source does not cover (e.g. ['Epidemiology', 'Dosing', 'Adverse effects'])"
                        },
                        "resolution_question": {
                            "type": "string",
                            "description": (
                                "A single, precise clinical question whose answer would fill this gap. "
                                "Make it specific to how this gap arose from the current source — "
                                "e.g. 'What is the recommended IV dose of furosemide for acute "
                                "decompensated heart failure in a patient with CKD?' rather than "
                                "'What is the dose of furosemide?'. This question is used to drive "
                                "targeted literature search and LLM resolution later."
                            )
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": "Page structural type of the target page — same enum as file plan subtype."
                        }
                    },
                    "required": ["page", "missing_sections"]
                }
            }
        },
        "required": ["summary", "files"]
    }
}

# Step 2: write tool — one file at a time
WRITE_TOOL = {
    "name": "write_wiki_file",
    "description": "Write the full markdown content for a single wiki file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Full markdown content of the file including frontmatter."
            },
            "scope": {
                "type": "string",
                "description": (
                    "For entity/concept pages only: one sentence declaring exactly what this page "
                    "covers and what belongs on OTHER pages instead. "
                    "Example: 'Acute Pancreatitis — general disease; etiology-specific variants "
                    "(HTG-induced, gallstone, alcohol) belong on their own pages.' "
                    "Omit for source/query pages."
                )
            }
        },
        "required": ["content"]
    }
}

# ── Prompts ─────────────────────────────────────────────────────────────────────

PHI_RULES = """
PRIVACY — Strip all PHI before writing to wiki:
- Patient name → anonymised ID (e.g. "Patient A")
- Exact dates → relative timing ("Day 3 of admission")
- Exact age → age range ("mid-60s")
- MRN / account numbers → omit
- Provider names → role ("the cardiologist")
- Institution names → "the treating institution"
- Geographic details below state level → omit
Keep: clinical findings, diagnoses, lab values, drug doses, treatment decisions, outcomes.
"""

PLAN_PROMPT = f"""You are ingesting a source into a multi-access knowledge base wiki. Today: {date.today().isoformat()}.

{PHI_RULES}

RULES:
- Always proceed. Never refuse. Ingest regardless of topic.
- Minimum plan: 1 source page + at least 3 entity/concept pages + log.md append + index.md update.
- Flag contradictions with existing wiki content explicitly in your plan.
- Use [[Page Title]] wiki links for EVERY significant named entity: drugs, conditions, procedures, devices, biomarkers — even ones that don't have a page yet. Stub pages are auto-created from links you produce.
- For each entity/concept page, key_points MUST contain ONLY information explicitly stated in this source.
  Do NOT add background knowledge, textbook facts, or anything not in the source text.
- For standard sections that apply to an entity/concept (e.g. Epidemiology, Pathophysiology, Treatment,
  Dosing, Prognosis) but are NOT addressed by this source, list them in knowledge_gaps instead.
- Do NOT add knowledge_gaps for patient-specific entity pages (paths like wiki/entities/patient-*.md).
  Those are individual patient records — missing fields (Demographics, Allergies, Family History, etc.)
  are PHI that will never be found by a literature search.

KNOWLEDGE GAP AWARENESS:
- Existing gap files (wiki/gaps/*.md) will be provided below if any exist.
- Use them to understand what's already known to be missing — so you can prioritise those areas
  when planning entity/concept pages for this source.
- Do NOT include wiki/gaps/ files in your file plan. Gap files are managed automatically.

Call plan_wiki_files with your summary, files, and knowledge_gaps.
"""

# Sentinel substituted at write time with the actual template block for the page's subtype.
TEMPLATE_PLACEHOLDER = "{TEMPLATE_PLACEHOLDER}"

WRITE_PROMPT = f"""You are writing a single wiki page. Today: {date.today().isoformat()}.

{PHI_RULES}

PAGE TYPE RULES:

**source pages** (wiki/sources/...):
  - Faithfully represent what the source says. Do not add outside information.
  - Sections: Citation, Abstract, Key Findings, Entities Mentioned, Concepts Mentioned, Open Questions.

**entity and concept pages** (wiki/entities/..., wiki/concepts/...):
  - Write ONLY what this source explicitly states about the entity/concept.
  - DO NOT add background knowledge, textbook content, or anything not in the source text.
  - {TEMPLATE_PLACEHOLDER} — use the section headings provided; omit any not covered by the source
    (they will be tracked as knowledge gaps). Do NOT invent other headings.
  - Specific values (doses, lab cut-offs, thresholds) are welcome IF they come from the source.
  - SCOPE: Declare a scope field in the write_wiki_file call.
    For MEDICATION / DRUG pages: scope should confirm that condition-specific dosing belongs HERE
    (e.g. "Furosemide — dosing across all indications including CHF and renal failure, pharmacology,
    adverse effects. Condition-specific dosing belongs here, on this page.").
    For PROCEDURE / DEVICE pages: scope should confirm that condition-specific indications belong HERE.
    For CONDITION / DISEASE pages: if the source covers a SUBTYPE or VARIANT, route that subtype
    content to the subtype's own page — do NOT write it here.

GENERAL:
- DO NOT summarise. Write only source-derived content. Use ## sections to organise.
- Use [[Page Title]] wiki links for EVERY significant named entity you mention — drugs, conditions, procedures, devices, biomarkers. Link even if the page doesn't exist yet; stubs are auto-created from your links.
- If flagging a contradiction, include a ## Contradictions section.

Write complete markdown with frontmatter:
---
title: Page Title
type: source | entity | concept | query
tags: [tag1, tag2]
created: {date.today().isoformat()}
updated: {date.today().isoformat()}
sources: [slug]
---

Call write_wiki_file with the complete file content.
"""

# ── Diff helper ─────────────────────────────────────────────────────────────────

def compute_diff(path: str, new_content: str, op: str, kb: "KBConfig") -> dict:
    full_path = kb.wiki_root / path
    is_new = not full_path.exists()
    old_lines = [] if is_new else full_path.read_text(encoding="utf-8").splitlines()

    if op == "append":
        new_lines = old_lines + new_content.splitlines()
    else:
        new_lines = new_content.splitlines()

    added   = sum(1 for l in new_lines if l not in old_lines)
    removed = sum(1 for l in old_lines if l not in new_lines)

    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    return {"path": path, "op": op, "is_new": is_new,
            "added": added, "removed": removed, "diff": diff_lines}

# ── Mop-up: enforce allowed wiki structure ───────────────────────────────────────

_WIKI_SECTIONS = {"entities", "concepts", "sources", "queries", "gaps"}

# Map rogue directory names to the correct allowed section.
# Anything not listed defaults to "entities" (named things).
_ROGUE_DIR_MAP: dict[str, str] = {
    "procedures": "entities",
    "medications": "entities",
    "drugs": "entities",
    "labs": "entities",
    "devices": "entities",
    "vitals": "entities",
    "symptoms": "concepts",
    "diagnoses": "concepts",
    "conditions": "concepts",
    "mechanisms": "concepts",
    "pathophysiology": "concepts",
}


def mop_up_wiki_structure(kb: "KBConfig") -> list[str]:
    """
    Move any .md files sitting outside the four allowed wiki directories into
    the correct directory. Returns a list of moves performed as log strings.
    """
    wiki_dir = kb.wiki_dir
    moves: list[str] = []

    for child in sorted(wiki_dir.iterdir()):
        if not child.is_dir() or child.name in _WIKI_SECTIONS:
            continue
        target_section = _ROGUE_DIR_MAP.get(child.name, "entities")
        target_dir = wiki_dir / target_section
        target_dir.mkdir(exist_ok=True)

        for md_file in sorted(child.glob("*.md")):
            dest = target_dir / md_file.name
            if dest.exists():
                # Avoid clobbering — append a suffix
                dest = target_dir / f"{md_file.stem}--from-{child.name}.md"
            md_file.rename(dest)
            rel_from = f"wiki/{child.name}/{md_file.name}"
            rel_to   = f"wiki/{target_section}/{dest.name}"
            moves.append(f"{rel_from} → {rel_to}")
            log.info("mop_up: moved %s → %s", rel_from, rel_to)

            # Fix vector_store paths
            try:
                vs_mod.rename_path(rel_from.removeprefix("wiki/"), rel_to.removeprefix("wiki/"), kb.wiki_dir)
            except Exception:
                pass

        # Remove the now-empty rogue directory
        try:
            child.rmdir()
            log.info("mop_up: removed empty directory wiki/%s/", child.name)
        except OSError:
            log.warning("mop_up: could not remove wiki/%s/ (not empty?)", child.name)

    return moves


# ── File executor ───────────────────────────────────────────────────────────────

def _is_patient_specific(path: str, patient_dir: str) -> bool:
    """True if this path should live under wiki/patients/{patient_dir}/.

    Only patient bookkeeping (index.md, log.md) and pages whose filename
    contains the patient CPMRN belong in the patient folder.  Generic clinical
    knowledge (entities, concepts, etc.) always goes into the shared wiki.
    """
    p = Path(path)
    stem = p.stem.lower()
    name = p.name.lower()
    # CPMRN is the part before the first underscore (encounter separator)
    cpmrn = patient_dir.split("_")[0].lower()

    if name in ("index.md", "log.md"):
        return True
    # source pages and entity pages named after the patient
    return cpmrn in stem


def _normalise_path(path: str) -> str:
    """
    Ensure the path starts with wiki/.
    The LLM occasionally omits the prefix (e.g. returns "concepts/foo.md"
    instead of "wiki/concepts/foo.md"), which would create files outside wiki/.
    """
    parts = Path(path).parts
    if parts and parts[0] in _WIKI_SECTIONS:
        return "wiki/" + path
    if parts and parts[0] in ("log.md", "index.md"):
        return "wiki/" + path
    return path


def execute_file_op(path: str, content: str, op: str, kb: "KBConfig", section: str = ""):
    path = _normalise_path(path)
    full_path = kb.wiki_root / path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    if op == "write":
        full_path.write_text(content, encoding="utf-8")

    elif op == "append":
        with open(full_path, "a", encoding="utf-8") as f:
            f.write("\n" + content)

    elif op == "update":
        # LLM has already merged existing + new content — just write it
        full_path.write_text(content, encoding="utf-8")

    # Auto-register new entity/concept pages so the canonical registry stays current
    rel = path.removeprefix("wiki/")
    if op == "write" and (rel.startswith("entities/") or rel.startswith("concepts/")):
        try:
            from .canonical_registry import _register_path_only
            title = Path(rel).stem.replace("-", " ").title()
            _register_path_only(rel, title, kb.wiki_dir)
        except Exception:
            pass


# ── Gap file helpers ────────────────────────────────────────────────────────────

def _parse_gap_missing(text: str) -> set[str]:
    """Extract missing section names from an existing gap file."""
    sections: set[str] = set()
    in_block = False
    for line in text.splitlines():
        if line.strip() == "## Missing Sections":
            in_block = True
            continue
        if in_block:
            if line.startswith("##"):
                break
            if line.startswith("- "):
                sections.add(line[2:].strip())
    return sections


def _parse_gap_resolution_question(text: str) -> str:
    """Extract the resolution_question from an existing gap file; empty string if absent."""
    in_block = False
    for line in text.splitlines():
        if line.strip() == "## Resolution Question":
            in_block = True
            continue
        if in_block:
            if line.startswith("##"):
                break
            stripped = line.strip()
            if stripped:
                return stripped
    return ""


def _parse_gap_placement(text: str) -> str:
    """Extract the placement field from a gap file's frontmatter; empty string if absent."""
    for line in text.splitlines():
        if line.strip().startswith("placement:"):
            return line.split(":", 1)[1].strip()
        if line.strip() == "---" and text.splitlines().index(line) > 0:
            break
    return ""


def _parse_gap_missing_values(text: str) -> list[str]:
    """Extract bullet items from the '## Specific Missing Values' section of an existing gap file."""
    items: list[str] = []
    in_block = False
    for line in text.splitlines():
        if line.strip() == "## Specific Missing Values":
            in_block = True
            continue
        if in_block:
            if line.startswith("##"):
                break
            if line.startswith("- "):
                items.append(line[2:].strip())
    return items


_GAP_HISTORY_FILE = "gap_history.json"

def _load_gap_history(gaps_dir: Path) -> dict:
    p = gaps_dir / _GAP_HISTORY_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_gap_history(gaps_dir: Path, history: dict) -> None:
    gaps_dir.mkdir(parents=True, exist_ok=True)
    (gaps_dir / _GAP_HISTORY_FILE).write_text(
        json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _parse_gap_section_times_opened(text: str) -> dict:
    """Parse section_times_opened JSON dict from gap file frontmatter."""
    for line in text.splitlines():
        if line.startswith("section_times_opened:"):
            raw = line.split(":", 1)[1].strip()
            try:
                return json.loads(raw)
            except Exception:
                return {}
        if line.strip() == "---" and text.splitlines().index(line) > 0:
            break
    return {}


def write_gap_files(knowledge_gaps: list, kb: "KBConfig", source_name: str) -> list[str]:
    """Write or merge gap files into wiki/gaps/ from the plan's knowledge_gaps output."""
    gaps_dir = kb.wiki_dir / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    written: list[str] = []

    for gap in knowledge_gaps:
        page = gap.get("page", "")
        raw_ms  = gap.get("missing_sections", "")
        missing = set(
            s.strip("- •").strip()
            for s in (raw_ms.splitlines() if isinstance(raw_ms, str) else raw_ms)
            if s.strip()
        )
        if not page or not missing:
            continue

        # Patient entity pages are individual records — their missing fields are PHI,
        # not resolvable by literature search.
        if Path(page).stem.startswith("patient-"):
            log.debug("write_gap_files: skipping patient entity page %s", page)
            continue

        stem = Path(page).stem
        gap_path = gaps_dir / f"{stem}.md"
        created = today

        existing_missing: set[str] = set()
        existing_question: str = ""
        existing_times_opened: int = 0
        existing_section_times: dict = {}

        # Load persistent history (survives gap file deletion on resolve)
        history = _load_gap_history(gaps_dir)
        historical_section_times: dict = history.get(stem, {})

        if gap_path.exists():
            existing_text = gap_path.read_text(encoding="utf-8")
            existing_missing = _parse_gap_missing(existing_text)
            existing_question = _parse_gap_resolution_question(existing_text)
            existing_section_times = _parse_gap_section_times_opened(existing_text)
            for line in existing_text.splitlines():
                if line.startswith("created:"):
                    created = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("times_opened:"):
                    try:
                        existing_times_opened = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass

        # Merge: existing file counts + persistent history (take max to avoid double-counting)
        for s, cnt in historical_section_times.items():
            existing_section_times[s] = max(existing_section_times.get(s, 0), cnt)

        merged = sorted(existing_missing | missing)
        if not merged:
            continue

        # Prefer incoming resolution_question (more specific context) over existing
        resolution_question = gap.get("resolution_question", "").strip() or existing_question
        subtype = gap.get("subtype", "").strip()

        # placement: "confirmed" when retrieval was strong; "approximate" when section
        # assignment is uncertain (weak retrieval or bundled query). Approximate gaps
        # are correct knowledge gaps but may be filed under the wrong section \u2014 the
        # defrag pipeline will relocate them after scope-contamination scanning.
        incoming_placement = gap.get("placement", "")
        existing_placement = _parse_gap_placement(
            gap_path.read_text(encoding="utf-8") if gap_path.exists() else ""
        )
        # Promote to confirmed if either source says confirmed; keep approximate otherwise
        placement = (
            "confirmed"
            if "confirmed" in (incoming_placement, existing_placement)
            else (incoming_placement or existing_placement or "confirmed")
        )

        # Merge incoming missing_values with any already stored in the gap file
        incoming_mv: list[str] = gap.get("missing_values") or []
        existing_mv: list[str] = _parse_gap_missing_values(
            gap_path.read_text(encoding="utf-8") if gap_path.exists() else ""
        )
        merged_mv = list(dict.fromkeys(existing_mv + incoming_mv))  # dedup, preserve order

        # Increment per-section counters for every incoming section
        section_times: dict = dict(existing_section_times)
        for s in missing:
            section_times[s] = section_times.get(s, 0) + 1

        # Persist to gap_history.json so counts survive gap file deletion
        history[stem] = section_times
        _save_gap_history(gaps_dir, history)

        title = stem.replace("-", " ").title()
        rel_gap = f"wiki/gaps/{stem}.md"
        times_opened = existing_times_opened + 1
        is_persistent = times_opened >= 3
        subtype_line = f"subtype: {subtype}\n" if subtype else ""
        placement_line = f"placement: {placement}\n"
        persistent_line = "status: persistent\n" if is_persistent else ""
        section_times_line = f"section_times_opened: {json.dumps(section_times, ensure_ascii=False)}\n"
        missing_values_block = (
            f"\n\n## Specific Missing Values\n\n"
            + "\n".join(f"- {v}" for v in merged_mv)
            if merged_mv else ""
        )
        content = (
            f"---\n"
            f'title: "Knowledge Gap \u2014 {title}"\n'
            f"type: gap\n"
            f"{subtype_line}"
            f"{placement_line}"
            f"{persistent_line}"
            f"times_opened: {times_opened}\n"
            f"{section_times_line}"
            f"referenced_page: {page}\n"
            f"tags: [gap]\n"
            f"created: {created}\n"
            f"updated: {today}\n"
            f"---\n\n"
            f"## Missing Sections\n\n"
            + "\n".join(f"- {s}" for s in merged)
            + (f"\n\n## Resolution Question\n\n{resolution_question}\n" if resolution_question else "")
            + missing_values_block
            + f"\n\n## Suggested Sources\n\n"
            f"- Ingest a reference document, guideline, or monograph that covers the above sections for: **{title}**\n"
            f"\n_Last updated by ingest of: {source_name}_\n"
        )
        gap_path.write_text(content, encoding="utf-8")

        # Metric 2+3: record gap open in page_metrics.json
        try:
            from .page_metrics import record_gap_open as _record_gap_open
            _record_gap_open(page, kb.wiki_dir)
        except Exception as _me:
            log.debug("page_metrics: record_gap_open failed (non-fatal): %s", _me)

        written.append(rel_gap)
        log.info(
            "  gap file written: %s  (%d missing sections)  times_opened=%d%s  question=%s",
            rel_gap, len(merged), times_opened,
            "  \u26a0 PERSISTENT" if is_persistent else "",
            "yes" if resolution_question else "no",
        )

    return written


def _norm_section(s: str) -> str:
    """Normalize a section name for fuzzy matching: lowercase, strip parentheticals."""
    return re.sub(r'\s*\(.*?\)', '', s).strip().lower()


def _section_match(gap_section: str, written_section: str) -> bool:
    """
    Return True if a gap section name is close enough to a written section name
    to be considered resolved. Uses layered matching:
      1. Exact normalized match
      2. Word-overlap ≥ 60 % of gap section words present in written section
      3. difflib sequence ratio ≥ 0.75
    """
    a = _norm_section(gap_section.removeprefix("RESOLVED: "))
    b = _norm_section(written_section)
    if a == b:
        return True
    gap_words = set(a.split())
    written_words = set(b.split())
    if gap_words and len(gap_words & written_words) / len(gap_words) >= 0.60:
        return True
    if difflib.SequenceMatcher(None, a, b).ratio() >= 0.75:
        return True
    return False


def resolve_gap_sections(gap_file_rel: str, resolved_sections: list[str], kb: "KBConfig") -> None:
    """Remove resolved sections from a gap file; delete the file if nothing remains."""
    gap_path = kb.wiki_root / gap_file_rel
    if not gap_path.exists():
        return
    existing = _parse_gap_missing(gap_path.read_text(encoding="utf-8"))
    remaining = {
        s for s in existing
        if not any(_section_match(s, r) for r in resolved_sections)
    }
    if not remaining:
        gap_path.unlink()
        log.info("  gap file fully resolved and deleted: %s", gap_file_rel)
    else:
        # Rewrite with remaining sections only
        text = gap_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        new_lines = []
        in_missing = False
        for line in lines:
            if line.strip() == "## Missing Sections":
                in_missing = True
                new_lines.append(line)
                new_lines.extend(f"- {s}" for s in sorted(remaining))
                continue
            if in_missing:
                if line.startswith("##"):
                    in_missing = False
                    new_lines.append(line)
                # skip old "- " lines inside the block
                elif not line.startswith("- "):
                    new_lines.append(line)
            else:
                new_lines.append(line)
        gap_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        log.info("  gap file updated: %s  (%d sections remain)", gap_file_rel, len(remaining))


def _parse_written_sections(content: str) -> set[str]:
    """Return the set of ## section headings present in written markdown content."""
    sections: set[str] = set()
    for line in content.splitlines():
        if line.startswith("## "):
            sections.add(line[3:].strip())
    return sections


def _parse_gap_referenced_page(text: str) -> str:
    """Extract the referenced_page field from a gap file's frontmatter."""
    for line in text.splitlines():
        if line.strip() == "---" and line != text.splitlines()[0]:
            break
        if line.startswith("referenced_page:"):
            return line.split(":", 1)[1].strip()
    return ""


def reconcile_gaps(kb: "KBConfig") -> dict:
    """
    Scan every gap file and close any sections already written in the
    referenced wiki page. Returns a summary of what was closed/trimmed.
    """
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return {"checked": 0, "fully_closed": [], "partially_resolved": [], "errors": []}

    fully_closed:       list[str] = []
    partially_resolved: list[str] = []
    errors:             list[str] = []
    checked = 0

    for gap_file in sorted(gaps_dir.glob("*.md")):
        checked += 1
        gap_rel = f"wiki/gaps/{gap_file.name}"
        try:
            gap_text = gap_file.read_text(encoding="utf-8", errors="replace")
            referenced = _parse_gap_referenced_page(gap_text)
            if not referenced:
                log.warning("reconcile: no referenced_page in %s — skipping", gap_file.name)
                continue

            page_path = kb.wiki_root / "wiki" / referenced
            if not page_path.exists():
                # Try without the leading directory in case referenced_page already has wiki/
                page_path = kb.wiki_root / referenced
            if not page_path.exists():
                log.warning("reconcile: referenced page not found for %s (%s)", gap_file.name, referenced)
                continue

            page_content = page_path.read_text(encoding="utf-8", errors="replace")
            written_sections = _parse_written_sections(page_content)
            missing_before = _parse_gap_missing(gap_text)

            if not written_sections:
                continue  # page is empty, nothing to resolve

            gap_existed = gap_file.exists()
            resolve_gap_sections(gap_rel, list(written_sections), kb)

            if gap_existed and not gap_file.exists():
                fully_closed.append(gap_rel)
                log.info("reconcile: fully closed %s (had %d sections)", gap_file.name, len(missing_before))
            elif gap_existed:
                missing_after = _parse_gap_missing(gap_file.read_text(encoding="utf-8", errors="replace"))
                resolved_count = len(missing_before) - len(missing_after)
                if resolved_count > 0:
                    partially_resolved.append(gap_rel)
                    log.info("reconcile: %d section(s) resolved in %s (%d remain)",
                             resolved_count, gap_file.name, len(missing_after))

        except Exception as exc:
            log.warning("reconcile: error processing %s: %s", gap_file.name, exc)
            errors.append(f"{gap_file.name}: {exc}")

    log.info(
        "reconcile_gaps: checked=%d fully_closed=%d partially_resolved=%d errors=%d",
        checked, len(fully_closed), len(partially_resolved), len(errors),
    )
    return {
        "checked":            checked,
        "fully_closed":       fully_closed,
        "partially_resolved": partially_resolved,
        "errors":             errors,
    }


def _wiki_link_context(wiki_dir: "Path", max_pages: int = 80) -> str:
    """Return a list of known wiki pages for [[link]] injection in LLM write prompts."""
    pages = []
    for prefix in ("entities", "concepts"):
        d = wiki_dir / prefix
        if d.exists():
            for f in sorted(d.glob("*.md")):
                pages.append(f.stem.replace("-", " ").title())
    if not pages:
        return ""
    return (
        "\nEXISTING WIKI PAGES (already have content — link these when you reference them):\n"
        + ", ".join(f"[[{t}]]" for t in pages[:max_pages])
        + "\nFor any other significant named entity (drug, condition, procedure, device, biomarker) "
        "not listed above, still use [[Page Title]] — a stub will be auto-created.\n"
    )


_STUB_SKIP = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be",
}


def _create_missing_stubs(content: str, kb: "KBConfig") -> None:
    """
    Scan a newly written wiki page for [[wiki links]] and create a minimal stub
    page for any linked title that doesn't yet have an entities/ or concepts/ page.
    Stubs land in entities/ by default.
    """
    from datetime import date as _date
    links = set(re.findall(r'\[\[([^\]]+)\]\]', content))
    for title in links:
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        # Skip empty, very short, or generic slugs
        if len(slug) < 4 or slug in _STUB_SKIP:
            continue
        # Check both entities and concepts
        exists = any(
            (kb.wiki_dir / d / f"{slug}.md").exists()
            for d in ("entities", "concepts")
        )
        if exists:
            continue
        stub_path = kb.wiki_dir / "entities" / f"{slug}.md"
        stub_content = (
            f"---\ntitle: {title}\ntype: entity\nsubtype: default\n"
            f"tags: []\ncreated: {_date.today().isoformat()}\n"
            f"updated: {_date.today().isoformat()}\nsources: []\n---\n\n"
            f"## Definition\n\n*(Stub — referenced from other wiki pages. "
            f"Ingest a source about this topic to fill it in.)*\n"
        )
        stub_path.write_text(stub_content, encoding="utf-8")
        log.info("  stub created: entities/%s.md  (linked as [[%s]])", slug, title)


def _section_enrichment_context(existing_content: str) -> str:
    """
    For op=update on entity/concept pages: build a per-section context block
    showing what is already written in each ## section so the LLM only adds
    genuinely new content rather than duplicating existing points.
    """
    lines = existing_content.splitlines()
    # Skip YAML frontmatter
    fm_end = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                fm_end = i + 1
                break

    sections: list[tuple[str, str]] = []
    current_heading: str | None = None
    current_body: list[str] = []

    for line in lines[fm_end:]:
        if line.startswith("## "):
            if current_heading is not None:
                sections.append((current_heading, "\n".join(current_body).strip()))
            current_heading = line[3:].strip()
            current_body = []
        elif current_heading is not None:
            current_body.append(line)
    if current_heading is not None:
        sections.append((current_heading, "\n".join(current_body).strip()))

    if not sections:
        return ""

    parts = []
    for heading, body in sections:
        if body:
            parts.append(
                f"## {heading}\n"
                f"Current content (DO NOT repeat or rephrase — only append new points from this source):\n"
                f"{body}"
            )
        else:
            parts.append(f"## {heading}\n(empty — create from source if covered)")

    return (
        "\nEXISTING SECTIONS — for each, add ONLY what is new from this source:\n\n"
        + "\n\n".join(parts)
        + "\n"
    )


def load_existing_gaps_context(kb: "KBConfig", max_files: int = 30) -> str:
    """Read wiki/gaps/ and return a context block for the plan prompt."""
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return ""
    gap_files = sorted(gaps_dir.glob("*.md"))[:max_files]
    if not gap_files:
        return ""
    blocks = []
    for f in gap_files:
        rel = f"wiki/gaps/{f.name}"
        blocks.append(f"--- {rel} ---\n{f.read_text(encoding='utf-8')}")
    return (
        "\n\nEXISTING KNOWLEDGE GAPS (wiki/gaps/):\n"
        + "\n\n".join(blocks)
        + "\n\nIf this source resolves any listed missing sections, include those gap files as "
          "update ops and prefix the resolved section names with 'RESOLVED: ' in key_points.\n"
    )


# ── Main pipeline ───────────────────────────────────────────────────────────────

def run_ingest(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",   # local file path (or GCP URI later)
    kb: "KBConfig | None" = None,
    patient_dir: str | None = None,  # if set, reroute all writes to wiki/patients/{patient_dir}/
) -> dict:
    if kb is None:
        kb = _default_kb()
    llm           = get_llm_client()
    if kb.claude_md.exists():
        system_prompt = kb.claude_md.read_text()
    else:
        # Fall back to project-level CLAUDE.md
        fallback = Path(__file__).resolve().parents[3] / "CLAUDE.md"
        system_prompt = fallback.read_text() if fallback.exists() else ""
    source_chars  = len(source_text)
    log.info("=== INGEST START  source=%r  chars=%d  model=%s ===", source_name, source_chars, MODEL)

    source_ref = source_path or source_name   # what gets stored in frontmatter

    # ── Step 1: Plan ──────────────────────────────────────────────────────────
    existing_gaps = load_existing_gaps_context(kb)
    log.info("Step 1: planning  max_tokens=16000  existing_gap_files=%s",
             "yes" if existing_gaps else "none")
    plan_response = llm.create_message(
        messages=[{
            "role": "user",
            "content": f"""Source: {source_name}
Citation: {citation}
Source file: {source_ref}

---SOURCE TEXT START---
{source_text[:50000]}
---SOURCE TEXT END---
{existing_gaps}
{PLAN_PROMPT}"""
        }],
        tools=[PLAN_TOOL],
        system=system_prompt,
        max_tokens=16000,
    )

    log.info(
        "Step 1 response: stop_reason=%s  in=%d  out=%d",
        plan_response.stop_reason,
        plan_response.usage.input_tokens,
        plan_response.usage.output_tokens,
    )
    if plan_response.stop_reason == "max_tokens":
        log.warning("Step 1 HIT TOKEN CEILING — plan is truncated, tool_use block will be missing")
    elif plan_response.stop_reason in ("blocked", "malformed"):
        log.error("Step 1 failed: stop_reason=%s", plan_response.stop_reason)
        return {
            "summary": f"Error: model returned stop_reason={plan_response.stop_reason} in planning step.",
            "files_written": [], "diffs": [],
            "errors": [f"Model stop_reason={plan_response.stop_reason} in step 1"],
            "input_tokens": plan_response.usage.input_tokens,
            "output_tokens": plan_response.usage.output_tokens,
            "cost_usd": 0,
        }

    log_call("ingest", f"[plan] {source_name}",
             plan_response.usage.input_tokens, plan_response.usage.output_tokens,
             model=MODEL)

    plan_block = next((b for b in plan_response.content if b.type == "tool_use"), None)
    if not plan_block:
        log.error("Step 1 returned no tool_use block. Content blocks: %s",
                  [b.type for b in plan_response.content])
        return {
            "summary": "Error: planning step did not return a tool call.",
            "files_written": [], "diffs": [],
            "errors": ["No plan_wiki_files tool call in step 1 response"],
            "input_tokens": plan_response.usage.input_tokens,
            "output_tokens": plan_response.usage.output_tokens,
            "cost_usd": 0,
        }

    plan           = plan_block.input
    summary        = plan.get("summary", "")
    file_plan      = plan.get("files", [])
    knowledge_gaps = plan.get("knowledge_gaps", [])

    # Remap each gap's page to its canonical destination before writing gap files.
    # This ensures repeated KG resolutions for the same concept consolidate into
    # one broad canonical page rather than creating a new granular stub each time.
    if knowledge_gaps:
        from .canonical_registry import resolve as _resolve_canonical
        for gap in knowledge_gaps:
            if gap.get("page"):
                # Derive concept name from the page path stem (e.g. entities/furosemide.md → "Furosemide")
                concept = Path(gap["page"]).stem.replace("-", " ").title()
                gap["page"] = _resolve_canonical(concept, kb.wiki_dir, llm)

    # Skip op=write for files that already exist — they'll be enriched via KG gaps instead.
    # Always skip index.md — sync_index owns it deterministically after every ingest.
    skipped = []
    filtered_plan = []
    for f in file_plan:
        path_check = _normalise_path(f.get("path", ""))
        if Path(path_check).name == "index.md":
            skipped.append(path_check)
        elif f.get("op") == "write" and (kb.wiki_root / path_check).exists():
            skipped.append(path_check)
        else:
            filtered_plan.append(f)
    if skipped:
        log.info("Skipping %d file(s) (index or already-existing): %s", len(skipped), skipped)
    file_plan = filtered_plan

    log.info("Plan received: %d files to write  summary=%r", len(file_plan), summary[:120])
    for i, f in enumerate(file_plan):
        raw_kp = f.get("key_points", "")
        pt_count = len([l for l in (raw_kp.splitlines() if isinstance(raw_kp, str) else raw_kp) if l.strip()])
        log.info("  [%d] %s  op=%s  points=%d", i + 1, f.get("path"), f.get("op"), pt_count)

    # ── Step 2: Write each file individually ──────────────────────────────────
    files_written    = []
    diffs            = []
    errors           = []
    gap_files_written: list[str] = []
    gaps_closed:       list[str] = []
    total_in         = plan_response.usage.input_tokens
    total_out        = plan_response.usage.output_tokens

    for idx, file_spec in enumerate(file_plan):
        path     = file_spec.get("path", "")
        op       = file_spec.get("op", "write")
        section  = file_spec.get("section", "")
        raw_pts  = file_spec.get("key_points", "")
        points   = [p.strip("- •").strip() for p in (raw_pts.splitlines() if isinstance(raw_pts, str) else raw_pts) if p.strip()]
        subtype  = file_spec.get("subtype", "default")

        # Gap files are managed purely in Python — never let the LLM write them
        if path.startswith("wiki/gaps/"):
            log.info("Step 2 [%d/%d]: skipping gap file %s (managed by pipeline)", idx + 1, len(file_plan), path)
            continue

        if shutdown_event.is_set():
            log.info("Step 2 [%d/%d]: shutdown requested — stopping write loop", idx + 1, len(file_plan))
            errors.append("Ingest cancelled: server shutting down")
            break

        log.info("Step 2 [%d/%d]: writing %s  op=%s", idx + 1, len(file_plan), path, op)

        try:
            full_path_for_op = kb.wiki_root / path
            existing_content = ""
            if op in ("update", "append") and full_path_for_op.exists():
                existing_content = full_path_for_op.read_text(encoding="utf-8")

            existing_block = (
                f"\n\nEXISTING FILE CONTENT (return the complete updated file):\n"
                f"---BEGIN EXISTING---\n{existing_content}\n---END EXISTING---"
                if existing_content else ""
            )

            is_entity_page = path.startswith("wiki/entities/") or path.startswith("wiki/concepts/")
            from .page_templates import template_block as _template_block
            template_injection = f"\nPAGE STRUCTURE: {_template_block(subtype)}\n" if is_entity_page else ""

            effective_prompt = WRITE_PROMPT.replace("{TEMPLATE_PLACEHOLDER}", _template_block(subtype) if is_entity_page else "Use clear ## section headings")

            wiki_links = _wiki_link_context(kb.wiki_dir)

            # For update ops on entity/concept pages, show per-section current content
            # so the LLM adds only genuinely new points rather than duplicating.
            enrichment_context = (
                _section_enrichment_context(existing_content)
                if op == "update" and is_entity_page and existing_content
                else ""
            )

            update_instruction = (
                "For each existing section: append ONLY new points from this source — "
                "do NOT repeat, rephrase, or rewrite content already there.\n"
                "For sections not yet in the page: create them if the source covers them.\n"
                if op == "update" and is_entity_page
                else "Write ONLY what this source explicitly states. Do NOT add background knowledge.\n"
            )

            write_response = llm.create_message(
                messages=[{
                    "role": "user",
                    "content": f"""{effective_prompt}

Write the wiki file at: {path}
Operation: {op}{f' (section: {section})' if section else ''}
{existing_block}
{enrichment_context}
{template_injection}
{wiki_links}
---SOURCE TEXT---
{source_text[:40000]}
---END SOURCE TEXT---

Key points the plan identified (use as a minimum checklist, not a ceiling — add more):
{chr(10).join(f'- {p}' for p in points)}

Source: {source_name}
Citation: {citation}
Source file path: {source_ref}

IMPORTANT: {update_instruction}
A page that only covers what the source says is correct — gaps are tracked separately.

Include `source_file: "{source_ref}"` in the frontmatter.
Return the COMPLETE file content — never truncate."""
                }],
                tools=[WRITE_TOOL],
                system=system_prompt,
                max_tokens=16000,
            )

            log.info(
                "  write response: stop_reason=%s  in=%d  out=%d",
                write_response.stop_reason,
                write_response.usage.input_tokens,
                write_response.usage.output_tokens,
            )
            if write_response.stop_reason == "max_tokens":
                log.warning("  WRITE HIT TOKEN CEILING for %s — content may be truncated", path)

            total_in  += write_response.usage.input_tokens
            total_out += write_response.usage.output_tokens

            write_block = next(
                (b for b in write_response.content if b.type == "tool_use"), None
            )

            if not write_block:
                log.error("  No tool_use block for %s. Block types: %s",
                          path, [b.type for b in write_response.content])
                errors.append(f"No content returned for {path}")
                continue

            content = write_block.input.get("content", "")
            llm_scope = write_block.input.get("scope", "")
            # Inject subtype into frontmatter for entity/concept pages
            if is_entity_page and content:
                from .page_templates import inject_subtype_frontmatter
                content = inject_subtype_frontmatter(content, subtype)
            # Inject scope into frontmatter (use LLM-declared or generate default)
            if is_entity_page and content and not path.startswith("wiki/patients/"):
                try:
                    from .quality_scorer import (
                        update_scope_frontmatter, _default_scope, parse_scope
                    )
                    effective_scope = llm_scope.strip() or parse_scope(content) or _default_scope(
                        Path(path).stem.replace("-", " ").title(), subtype
                    )
                    content = update_scope_frontmatter(content, effective_scope)
                    log.info("  Scope: %s", effective_scope[:80])
                except Exception as _se:
                    log.warning("  Scope injection failed (non-fatal): %s", _se)
            # Deduplicate any repeated ## section headings the LLM may have emitted
            if is_entity_page and content:
                from .fill_sections_pipeline import _dedup_sections
                content = _dedup_sections(content)
            # Score and inject quality metadata before writing
            if is_entity_page and content and not path.startswith("wiki/patients/"):
                try:
                    from .quality_scorer import score_page, update_quality_frontmatter
                    _scores = score_page(content)
                    if _scores:
                        content = update_quality_frontmatter(content, _scores)
                        log.info("  Quality scores: %s", {k: v["score"] for k, v in _scores.items()})
                except Exception as _qe:
                    log.warning("  Quality scoring failed (non-fatal): %s", _qe)
            log.info("  Content length: %d chars → executing op=%s  subtype=%s", len(content), op, subtype if is_entity_page else "n/a")

            # Reroute patient-specific pages to wiki/patients/{patient_dir}/.
            # Generic clinical knowledge (entities, concepts not named after the
            # patient) stays in the shared wiki sections.
            if patient_dir and path.startswith("wiki/"):
                if _is_patient_specific(path, patient_dir):
                    path = f"wiki/patients/{patient_dir}/{path[len('wiki/'):]}"
                    log.info("  Patient page rerouted → %s", path)
                else:
                    log.info("  Generic page — keeping in shared wiki: %s", path)

            diff    = compute_diff(path, content, op, kb)
            execute_file_op(path, content, op, kb, section)
            files_written.append(path)
            diffs.append(diff)
            log.info("  ✓ Written: %s  (+%d/-%d lines)", path, diff["added"], diff["removed"])
            # Embed the page for semantic search (_embed_page skips wiki/patients/ automatically)
            _embed_page(path, content, kb)

            # Create stub pages for any [[links]] that don't exist yet
            p = Path(path)
            if len(p.parts) >= 2 and p.parts[-2] in ("entities", "concepts") and not path.startswith("wiki/patients/"):
                _create_missing_stubs(content, kb)

            # Scope contamination check — flag out-of-scope content in frontmatter
            if is_entity_page and content and not path.startswith("wiki/patients/"):
                try:
                    from .quality_scorer import (
                        check_scope, parse_scope, _default_scope,
                        update_contamination_frontmatter,
                    )
                    scope_str = parse_scope(content) or _default_scope(
                        Path(path).stem.replace("-", " ").title(), subtype
                    )
                    page_title = Path(path).stem.replace("-", " ").title()
                    existing_titles = [
                        f.stem.replace("-", " ").title()
                        for d in ("entities", "concepts")
                        for f in sorted((kb.wiki_dir / d).glob("*.md"))
                    ] if kb.wiki_dir.exists() else []
                    sc_result = check_scope(page_title, content, scope_str, existing_titles)
                    if not sc_result["clean"]:
                        updated = update_contamination_frontmatter(content, sc_result["violations"])
                        full_written_path = kb.wiki_root / path
                        full_written_path.write_text(updated, encoding="utf-8")
                        log.warning(
                            "  ⚠ Scope contamination flagged on %s: %d violation(s)",
                            path, len(sc_result["violations"]),
                        )
                except Exception as _sce:
                    log.warning("  Scope check failed (non-fatal): %s", _sce)

            # Gap resolution deferred — runs after write_gap_files below
            # (so new gaps created in this ingest are also checked)

        except Exception as e:
            log.exception("  Exception writing %s: %s", path, e)
            errors.append(f"Error writing {path}: {e}")

    # ── Write new gap files for sections not covered by this source ───────────
    gap_files_written = write_gap_files(knowledge_gaps, kb, source_name)

    # ── Resolve gaps for all entity/concept pages written in this ingest ──────
    # Runs AFTER write_gap_files so newly-opened gaps are also checked against
    # sections already written in the same pass (fixes born-stale gap bug).
    for written_path in files_written:
        p = Path(written_path)
        if len(p.parts) >= 2 and p.parts[-2] in ("entities", "concepts"):
            gap_rel = f"wiki/gaps/{p.stem}.md"
            if (kb.wiki_root / gap_rel).exists():
                full_path = kb.wiki_root / written_path
                if full_path.exists():
                    content = full_path.read_text(encoding="utf-8", errors="replace")
                    written_sections = _parse_written_sections(content)
                    gap_existed = (kb.wiki_root / gap_rel).exists()
                    resolve_gap_sections(gap_rel, list(written_sections), kb)
                    if gap_existed and not (kb.wiki_root / gap_rel).exists():
                        gaps_closed.append(gap_rel)
                        log.info("  gap fully resolved: %s", gap_rel)
                    elif gap_existed:
                        log.info("  gap partially resolved: %s  (%d sections written)",
                                 gap_rel, len(written_sections))

    # ── Mop-up: move any files the LLM placed in rogue directories ────────────
    moved = mop_up_wiki_structure(kb)
    if moved:
        log.info("mop_up: %d file(s) relocated: %s", len(moved), moved)

    cost = log_call("ingest", source_name, total_in, total_out, model=MODEL, kb_name=kb.name)

    log.info(
        "=== INGEST END  files_written=%d  gap_files=%d  errors=%d  total_in=%d  total_out=%d  cost=$%.4f ===",
        len(files_written), len(gap_files_written), len(errors), total_in, total_out, cost,
    )
    if errors:
        for err in errors:
            log.warning("  error: %s", err)

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation": "ingest",
        "kb": kb.name,
        "source": source_name,
        "files_written": files_written,
        "gaps_opened": gap_files_written,
        "gaps_closed": gaps_closed,
        "sections_filled": [],
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": cost,
        "file_diffs": {d["path"]: {"diff": d["diff"], "added": d["added"], "removed": d["removed"], "is_new": d["is_new"]} for d in diffs},
    })

    return {
        "summary":          summary,
        "files_written":    files_written,
        "diffs":            diffs,
        "errors":           errors,
        "knowledge_gaps":   knowledge_gaps,
        "gap_files_written": gap_files_written,
        "input_tokens":     total_in,
        "output_tokens":    total_out,
        "cost_usd":         cost,
        "model":            MODEL,
    }


# ── Chunked entry point ──────────────────────────────────────────────────────

CHUNK_SIZE = 30_000   # chars (~2-3 pages); gives ~10 chunks for a typical 20-page doc


def run_ingest_chunked(
    source_text: str,
    source_name: str,
    citation: str = "",
    source_path: str = "",
    kb: "KBConfig | None" = None,
    patient_dir: str | None = None,
) -> dict:
    """
    Ingest a source, automatically splitting large documents into sequential
    chunks so each fits comfortably within the model's context window.

    Each chunk after the first receives a one-paragraph summary of what prior
    chunks already covered, so the planner knows which pages were already
    created and can issue update ops rather than duplicating work.

    Small documents (≤ CHUNK_SIZE chars) pass straight through to run_ingest().
    """
    if kb is None:
        kb = _default_kb()
    if len(source_text) <= CHUNK_SIZE:
        result = run_ingest(source_text, source_name, citation, source_path, kb, patient_dir)
        sync_index(kb.wiki_dir)
        return result

    # Split on whitespace boundaries near each CHUNK_SIZE boundary
    chunks: list[str] = []
    start = 0
    while start < len(source_text):
        end = start + CHUNK_SIZE
        if end < len(source_text):
            # Walk back to the nearest newline so we don't cut mid-sentence
            boundary = source_text.rfind("\n", start, end)
            if boundary > start:
                end = boundary
        chunks.append(source_text[start:end])
        start = end

    total_chunks = len(chunks)
    log.info(
        "=== CHUNKED INGEST  source=%r  total_chars=%d  chunks=%d  chunk_size=%d ===",
        source_name, len(source_text), total_chunks, CHUNK_SIZE,
    )

    combined: dict = {
        "summary": "",
        "files_written": [],
        "diffs": [],
        "errors": [],
        "knowledge_gaps": [],
        "gap_files_written": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "model": MODEL,
    }
    seen_files: set[str] = set()
    seen_gap_files: set[str] = set()
    # Deduplicate knowledge gaps across chunks by page path
    gaps_by_page: dict[str, set[str]] = {}
    gaps_resolution_questions: dict[str, str] = {}
    running_summary = ""

    for i, chunk in enumerate(chunks, 1):
        if shutdown_event.is_set():
            log.info("--- Chunk %d/%d: shutdown requested — stopping chunked ingest ---", i, total_chunks)
            combined["errors"].append(f"Ingest cancelled at chunk {i}/{total_chunks}: server shutting down")
            break

        log.info("--- Chunk %d/%d  chars=%d ---", i, total_chunks, len(chunk))

        # Prepend prior-chunk context so the planner can issue update ops
        if running_summary:
            context_header = (
                f"[PRIOR CHUNKS ALREADY INGESTED — do not re-create these pages, "
                f"use op=update to add new information from this chunk]\n"
                f"Summary of prior chunks: {running_summary}\n\n"
                f"--- CHUNK {i}/{total_chunks} STARTS HERE ---\n\n"
            )
            chunk_text = context_header + chunk
        else:
            chunk_text = f"--- CHUNK {i}/{total_chunks} STARTS HERE ---\n\n" + chunk

        result = run_ingest(
            source_text=chunk_text,
            source_name=f"{source_name} [part {i}/{total_chunks}]",
            citation=citation,
            source_path=source_path,
            kb=kb,
            patient_dir=patient_dir,
        )

        # Accumulate totals
        combined["input_tokens"]  += result.get("input_tokens", 0)
        combined["output_tokens"] += result.get("output_tokens", 0)
        combined["cost_usd"]      += result.get("cost_usd", 0.0)
        combined["errors"].extend(result.get("errors", []))

        # Merge diffs — keep only the latest diff per path
        for diff in result.get("diffs", []):
            combined["diffs"].append(diff)

        # Track unique files written
        for f in result.get("files_written", []):
            if f not in seen_files:
                seen_files.add(f)
                combined["files_written"].append(f)

        # Track unique gap files written
        for gf in result.get("gap_files_written", []):
            if gf not in seen_gap_files:
                seen_gap_files.add(gf)
                combined["gap_files_written"].append(gf)

        # Merge knowledge gaps — union missing sections per page, keep first resolution_question seen
        for gap in result.get("knowledge_gaps", []):
            page = gap.get("page", "")
            if page:
                gaps_by_page.setdefault(page, set()).update(gap.get("missing_sections", []))
                if gap.get("resolution_question") and page not in gaps_resolution_questions:
                    gaps_resolution_questions[page] = gap["resolution_question"]

        # Set overall summary from first chunk; append subsequent summaries
        chunk_summary = result.get("summary", "")
        if chunk_summary:
            if not combined["summary"]:
                combined["summary"] = chunk_summary
            running_summary = (running_summary + " " + chunk_summary).strip()

        log.info(
            "--- Chunk %d/%d done  files_written=%d  errors=%d ---",
            i, total_chunks, len(result.get("files_written", [])), len(result.get("errors", [])),
        )

    combined["knowledge_gaps"] = [
        {
            "page": page,
            "missing_sections": sorted(sections),
            **({"resolution_question": gaps_resolution_questions[page]} if page in gaps_resolution_questions else {}),
        }
        for page, sections in sorted(gaps_by_page.items())
    ]

    # ── Merge patient entity pages into one timeline (patient ingestion only) ───
    if patient_dir:
        _merge_patient_timeline_pages(combined, patient_dir, kb)

    sync_index(kb.wiki_dir)

    log.info(
        "=== CHUNKED INGEST COMPLETE  total_files=%d  total_errors=%d  total_cost=$%.4f  gaps=%d ===",
        len(combined["files_written"]), len(combined["errors"]), combined["cost_usd"],
        len(combined["knowledge_gaps"]),
    )
    return combined


_MERGE_TIMELINE_TOOL = {
    "name": "write_patient_timeline",
    "description": "Return a single unified patient timeline page merging all date-specific entries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Complete unified chronological patient timeline markdown page with frontmatter.",
            }
        },
        "required": ["content"],
    },
}

_MERGE_TIMELINE_SYSTEM = f"""You are a medical wiki editor merging patient encounter notes into a single timeline.
{PHI_RULES}
Rules:
- One ## section per date
- Under each date: Clinical Findings, Diagnoses, Medications, Procedures, Monitoring
- Remove cross-date repetition (keep in earliest date; later dates say "continued" where unchanged)
- Use [[wiki links]] for all drug/procedure/concept names
- Never add new information — only reorganise what is in the source pages
"""


def _merge_patient_timeline_pages(combined: dict, patient_dir: str, kb: "KBConfig") -> None:
    """
    After chunked patient ingest: if multiple date-specific entity pages were written
    under wiki/patients/{patient_dir}/entities/, merge them into a single timeline page
    and delete the individual date pages.
    Modifies `combined` in place.
    """
    import re

    entities_dir = kb.wiki_dir / "patients" / patient_dir / "entities"
    if not entities_dir.exists():
        return

    # Find all entity pages written during this ingest that look like date pages
    # (any page that isn't already a *_timeline.md)
    date_page_prefix = f"wiki/patients/{patient_dir}/entities/"
    patient_entity_pages = [
        f for f in combined.get("files_written", [])
        if f.startswith(date_page_prefix)
        and not f.endswith("_timeline.md")
        and (kb.wiki_root / f).exists()
    ]

    if len(patient_entity_pages) < 2:
        return  # nothing to merge

    # Derive patient ID from patient_dir (strip trailing _N suffixes like _1, _2)
    patient_id = patient_dir.upper()

    log.info(
        "=== PATIENT TIMELINE MERGE  patient=%s  pages=%d: %s ===",
        patient_dir, len(patient_entity_pages), patient_entity_pages,
    )

    # Read all pages
    pages_text = []
    for i, p in enumerate(sorted(patient_entity_pages), 1):
        content = (kb.wiki_root / p).read_text(encoding="utf-8")
        pages_text.append(f"--- PAGE {i} ({p.split('/')[-1]}) ---\n{content}")

    prompt = (
        f"Merge these {len(patient_entity_pages)} date-specific patient encounter pages "
        f"for patient {patient_id} into ONE unified chronological timeline page.\n\n"
        + "\n\n".join(pages_text)
    )

    llm = get_llm_client()
    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[_MERGE_TIMELINE_TOOL],
        system=_MERGE_TIMELINE_SYSTEM,
        max_tokens=16_000,
    )

    total_in  = resp.usage.input_tokens
    total_out = resp.usage.output_tokens
    cost = log_call("merge_patient_timeline", patient_dir, total_in, total_out, model=MODEL, kb_name=kb.name)

    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_block:
        log.warning("_merge_patient_timeline_pages: no tool_use returned — skipping merge")
        combined["cost_usd"] += cost
        return

    merged_content = tool_block.input.get("content", "")
    timeline_path = f"wiki/patients/{patient_dir}/entities/{patient_id}_timeline.md"

    diff = compute_diff(timeline_path, merged_content, "write", kb)
    execute_file_op(timeline_path, merged_content, "write", kb)
    _embed_page(timeline_path, merged_content, kb)

    log.info(
        "_merge_patient_timeline_pages: written %s  +%d lines  cost=$%.4f",
        timeline_path, diff["added"], cost,
    )

    # Delete the individual date pages and remove from combined
    for p in patient_entity_pages:
        full = kb.wiki_root / p
        if full.exists():
            full.unlink()
            log.info("  Deleted: %s", p)

    # Update combined: replace date pages with single timeline page
    combined["files_written"] = [
        f for f in combined["files_written"] if f not in patient_entity_pages
    ]
    combined["files_written"].append(timeline_path)
    combined["diffs"] = [d for d in combined["diffs"] if d.get("path") not in patient_entity_pages]
    combined["diffs"].append(diff)
    combined["cost_usd"]      += cost
    combined["input_tokens"]  += total_in
    combined["output_tokens"] += total_out

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation": "consolidate",
        "kb": kb.name,
        "source": f"{patient_dir} patient timeline merge",
        "files_written": [timeline_path],
        "gaps_opened": [],
        "gaps_closed": [],
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": cost,
        "file_diffs": {diff["path"]: {"diff": diff["diff"], "added": diff["added"], "removed": diff["removed"], "is_new": True}},
    })


_CONSOLIDATE_TOOL = {
    "name": "consolidate_wiki_page",
    "description": "Return the consolidated, de-duplicated wiki page content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "The COMPLETE consolidated wiki page markdown including frontmatter. "
                    "Within each ## section, semantically duplicate bullet points or sentences "
                    "have been merged into one. No content has been added or removed — only unified."
                ),
            },
        },
        "required": ["content"],
    },
}

_CONSOLIDATE_SYSTEM = f"""You are a wiki editor performing a consolidation pass.
Your ONLY job is to remove semantic duplicates within sections — do not add, invent, or expand anything.
{PHI_RULES}
"""


def _consolidate_page(page_path: str, kb: "KBConfig") -> dict | None:
    """
    Structural + semantic deduplication for a page written by multiple ingest chunks.
    1. Run _dedup_sections (structural — merges identical heading blocks).
    2. Run LLM pass to merge semantically duplicate bullet points within sections.
    Returns a small result dict with cost/token/diff info, or None if no change.
    """
    from .fill_sections_pipeline import _dedup_sections
    page_full_path = kb.wiki_root / page_path
    if not page_full_path.exists():
        log.warning("_consolidate_page: %s not found, skipping", page_path)
        return None

    original = page_full_path.read_text(encoding="utf-8")

    # Step 1: structural dedup
    after_structural = _dedup_sections(original)

    # Step 2: LLM semantic dedup
    llm = get_llm_client()
    prompt = f"""This wiki page was written in multiple passes (chunked ingestion) and may contain
semantically duplicate bullet points or sentences within the same section.

Your task:
- Within each ## section, identify and merge bullet points that say the same thing in different words
- Keep the most informative version of each point
- Do NOT add any new information
- Do NOT remove information that is genuinely distinct
- Preserve the frontmatter, all section headings, and all cross-references exactly
- Return the COMPLETE page content

---BEGIN PAGE---
{after_structural}
---END PAGE---
"""
    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[_CONSOLIDATE_TOOL],
        system=_CONSOLIDATE_SYSTEM,
        max_tokens=16_000,
    )

    total_in  = resp.usage.input_tokens
    total_out = resp.usage.output_tokens
    cost = log_call("consolidate", page_path, total_in, total_out, model=MODEL, kb_name=kb.name)

    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_block:
        consolidated = tool_block.input.get("content", "")
    else:
        # LLM didn't use tool — fall back to structural dedup only
        consolidated = after_structural

    if consolidated.strip() == original.strip():
        log.info("_consolidate_page: no change for %s", page_path)
        return {"cost_usd": cost, "input_tokens": total_in, "output_tokens": total_out, "diff": None}

    diff = compute_diff(page_path, consolidated, "update", kb)
    execute_file_op(page_path, consolidated, "update", kb)
    _embed_page(page_path, consolidated, kb)

    log.info(
        "_consolidate_page: %s consolidated  +%d/-%d  cost=$%.4f",
        page_path, diff["added"], diff["removed"], cost,
    )

    from .activity_log import append_event
    append_event(kb.wiki_dir, {
        "operation": "consolidate",
        "kb": kb.name,
        "source": "chunked-ingest consolidation",
        "files_written": [page_path],
        "gaps_opened": [],
        "gaps_closed": [],
        "tokens_in": total_in,
        "tokens_out": total_out,
        "cost_usd": cost,
        "file_diffs": {diff["path"]: {"diff": diff["diff"], "added": diff["added"], "removed": diff["removed"], "is_new": False}},
    })

    return {"cost_usd": cost, "input_tokens": total_in, "output_tokens": total_out, "diff": diff}
