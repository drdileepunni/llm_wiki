"""
Canonical page registry for the wiki.

Ensures that knowledge gaps always resolve to a stable, broad canonical page
rather than a new granular stub on each encounter. The registry is stored as
canonical_registry.json inside each KB's wiki directory.

Public API
----------
find_canonical(concept, wiki_dir, threshold) -> str | None
    Similarity search against registered pages. Returns path on hit, None on miss.

register(path, covers_text, wiki_dir) -> None
    Add a path to the registry, create a stub page if absent, upsert vector store.

resolve(concept, wiki_dir, llm) -> str
    find_canonical first; on miss ask LLM for a canonical path, then register it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger("wiki.canonical_registry")

_REGISTRY_FILE = "canonical_registry.json"
_DEFAULT_THRESHOLD = 0.85


# ── Registry I/O ─────────────────────────────────────────────────────────────

def _load(wiki_dir: Path) -> list[dict]:
    p = wiki_dir / _REGISTRY_FILE
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Canonical registry corrupt, resetting: %s", e)
        return []


def _save(wiki_dir: Path, entries: list[dict]) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / _REGISTRY_FILE).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _registry_paths(wiki_dir: Path) -> set[str]:
    return {e["path"] for e in _load(wiki_dir)}


# ── Public API ────────────────────────────────────────────────────────────────

def find_canonical(
    concept: str,
    wiki_dir: Path,
    threshold: float = _DEFAULT_THRESHOLD,
) -> str | None:
    """
    Embed concept and search for the best matching canonical page.
    First checks registry entries at `threshold`; then falls back to searching
    all existing entity/concept pages at a slightly higher threshold (0.88) so
    the registry self-bootstraps from pages written before it was populated.
    Returns the page path (relative to wiki_dir) on a hit, None on miss.
    """
    from . import vector_store as vs_mod

    reg_paths = _registry_paths(wiki_dir)

    results = vs_mod.search(concept, wiki_dir, top_k=10)
    if not results:
        return None

    # Pass 1: registry entries at the configured threshold
    for r in results:
        if r["path"] in reg_paths and r["score"] >= threshold:
            log.debug("find_canonical: '%s' → %s (score=%.3f) [registry]",
                      concept[:60], r["path"], r["score"])
            return r["path"]

    # Pass 2: any existing entity/concept page at a higher threshold
    _FALLBACK_THRESHOLD = max(threshold, 0.88)
    for r in results:
        path = r["path"]
        if (r["score"] >= _FALLBACK_THRESHOLD
                and (path.startswith("entities/") or path.startswith("concepts/"))
                and (wiki_dir / path).exists()):
            log.info("find_canonical: '%s' → %s (score=%.3f) [vector fallback]",
                     concept[:60], path, r["score"])
            # Opportunistically register so future lookups are cheaper
            _register_path_only(path, concept, wiki_dir)
            return path

    log.debug("find_canonical: no canonical hit for '%s' (best=%.3f)", concept[:60],
              results[0]["score"] if results else 0.0)
    return None


def _register_path_only(path: str, covers_text: str, wiki_dir: Path) -> None:
    """Add path to registry without creating a stub or upserting vectors (page already exists)."""
    entries = _load(wiki_dir)
    if any(e["path"] == path for e in entries):
        return
    entries.append({
        "path": path,
        "covers": covers_text,
        "auto": True,
        "created": date.today().isoformat(),
    })
    _save(wiki_dir, entries)
    log.debug("canonical_registry: opportunistically registered '%s'", path)


def seed_registry_vectors(wiki_dir: Path) -> None:
    """
    Ensure all registered canonical pages have vector store entries.
    Call once at server startup. Skips pages already in the store.
    """
    from . import vector_store as vs_mod

    entries = _load(wiki_dir)
    if not entries:
        return

    store_paths = {r["path"] for r in vs_mod._load(wiki_dir)}
    missing = [e for e in entries if e["path"] not in store_paths]
    if not missing:
        log.debug("canonical_registry: all %d canonical pages already in vector store", len(entries))
        return

    log.info("canonical_registry: seeding %d canonical page(s) into vector store", len(missing))
    for e in missing:
        page_path = wiki_dir / e["path"]
        if not page_path.exists():
            continue
        try:
            content = page_path.read_text(encoding="utf-8")
            vs_mod.upsert(e["path"], content, wiki_dir)
        except Exception as exc:
            log.warning("canonical_registry: seed upsert failed for '%s': %s", e["path"], exc)


def register(path: str, covers_text: str, wiki_dir: Path) -> None:
    """
    Register a canonical page. Creates a minimal stub page if the file doesn't
    exist yet, and upserts it into the vector store so future find_canonical
    calls can locate it.
    """
    from . import vector_store as vs_mod

    entries = _load(wiki_dir)
    existing_paths = {e["path"] for e in entries}

    if path not in existing_paths:
        entries.append({
            "path": path,
            "covers": covers_text,
            "auto": True,
            "created": date.today().isoformat(),
        })
        _save(wiki_dir, entries)
        log.info("canonical_registry: registered '%s'", path)

    # Create stub page if absent
    full_path = wiki_dir / path
    if not full_path.exists():
        full_path.parent.mkdir(parents=True, exist_ok=True)
        stem = Path(path).stem
        title = stem.replace("-", " ").title()
        stub = (
            f"---\n"
            f'title: "{title}"\n'
            f"type: entity\n"
            f"tags: []\n"
            f"created: {date.today().isoformat()}\n"
            f"updated: {date.today().isoformat()}\n"
            f"sources: []\n"
            f"---\n\n"
            f"_{covers_text}_\n"
        )
        full_path.write_text(stub, encoding="utf-8")
        log.info("canonical_registry: created stub page '%s'", path)

    # Upsert into vector store so future searches can find this page
    try:
        content = (wiki_dir / path).read_text(encoding="utf-8")
        vs_mod.upsert(path, content, wiki_dir)
    except Exception as exc:
        log.warning("canonical_registry: vector upsert failed for '%s': %s", path, exc)


_RESOLVE_TOOL = {
    "name": "canonical_page",
    "description": "Return the canonical wiki page for a clinical concept.",
    "input_schema": {
        "type": "object",
        "required": ["path", "covers"],
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative wiki path for the canonical page, e.g. 'entities/mechanical-ventilation.md'. "
                    "Use broad, topic-level names — not drug-specific stubs. "
                    "E.g. prefer 'entities/vasopressors.md' over 'entities/noradrenaline-dosing.md'."
                ),
            },
            "covers": {
                "type": "string",
                "description": (
                    "One sentence describing what clinical knowledge this page covers. "
                    "E.g. 'Vasopressors used in ICU shock management including noradrenaline, "
                    "adrenaline, vasopressin, MAP targets, and titration protocols.'"
                ),
            },
        },
    },
}


# Section headings that are NOT standalone concepts — strip these suffixes before routing
# so "Central Venous Catheter Contraindications" → "Central Venous Catheter"
_SECTION_SUFFIXES = (
    " Contraindications", " Indications", " Dosing", " Dose Adjustment",
    " Mechanism Of Action", " Mechanism", " Adverse Effects", " Side Effects",
    " Drug Interactions", " Monitoring", " Management", " Complications",
    " Prognosis", " Definition", " Clinical Significance", " Technique",
    " Reference Range", " Interpretation", " Aetiology", " Clinical Features",
)

def _normalise_concept(concept: str) -> str:
    """Strip trailing section-heading words so 'Drug X Dosing' → 'Drug X'."""
    c = concept.strip()
    for suffix in _SECTION_SUFFIXES:
        if c.lower().endswith(suffix.lower()):
            stripped = c[: len(c) - len(suffix)].strip()
            if stripped:
                log.debug("canonical_registry: normalised concept '%s' → '%s'", c, stripped)
                return stripped
    return c


def resolve(concept: str, wiki_dir: Path, llm=None) -> str:
    """
    Return a canonical page path for concept. Searches the registry first;
    if no match, asks the LLM to propose a broad canonical path and registers it.

    Falls back gracefully to a slug-derived path if the LLM call fails.
    """
    normalised = _normalise_concept(concept)
    hit = find_canonical(normalised, wiki_dir)
    if hit:
        return hit
    # Also try the original name in case normalisation overstripped
    if normalised != concept:
        hit = find_canonical(concept, wiki_dir)
        if hit:
            return hit

    # No registry hit — ask LLM to propose a canonical path
    if llm is None:
        from .llm_client import get_llm_client
        llm = get_llm_client()

    existing = _load(wiki_dir)
    existing_table = "\n".join(
        f"  {e['path']} — {e.get('covers', '')}" for e in existing[:40]
    )
    prompt = (
        f"Clinical concept to file: {normalised}\n\n"
        f"Existing canonical pages (prefer reusing one if it fits, score would have been below threshold):\n"
        f"{existing_table}\n\n"
        "If the concept fits an existing page, return that path. "
        "If it's genuinely a new topic, return a new broad entity/concept path — "
        "e.g. 'entities/central-venous-catheter.md', NOT 'entities/central-venous-catheter-contraindications.md'. "
        "NEVER include section-heading words (Dosing, Contraindications, Mechanism, Indications, etc.) in the page name."
    )

    try:
        resp = llm.create_message(
            messages=[{"role": "user", "content": prompt}],
            tools=[_RESOLVE_TOOL],
            system=(
                "You assign clinical concepts to broad canonical wiki pages. "
                "Prefer broad topic pages over narrow stubs. "
                "Existing canonical pages are listed; reuse them when the concept fits."
            ),
            max_tokens=256,
            force_tool=True,
        )
        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if block:
            path = block.input.get("path", "").strip().lstrip("/")
            covers = block.input.get("covers", concept)
            if path:
                # Normalise: ensure it starts with entities/ or concepts/
                if not (path.startswith("entities/") or path.startswith("concepts/") or path.startswith("wiki/")):
                    path = f"entities/{Path(path).name}"
                path = path.removeprefix("wiki/")
                register(path, covers, wiki_dir)
                log.info("canonical_registry: resolved '%s' → new canonical '%s'", concept[:60], path)
                return path
    except Exception as exc:
        log.warning("canonical_registry: LLM resolve failed for '%s': %s", concept[:60], exc)

    # Hard fallback: slug from concept
    slug = re.sub(r"[^a-z0-9]+", "-", concept.lower()).strip("-")[:60]
    path = f"entities/{slug}.md"
    register(path, concept, wiki_dir)
    log.info("canonical_registry: slug fallback for '%s' → '%s'", concept[:60], path)
    return path
