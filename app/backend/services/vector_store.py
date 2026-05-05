"""
Semantic vector store for wiki pages.

Uses Google text-embedding-004 to embed each page at write time.
Vectors are stored per-KB in wiki/vector_store.json.

Public API
----------
upsert(page_path, content, wiki_dir)   — embed and store/update a page
search(query, wiki_dir, top_k)         — return top_k similar pages
rebuild_all(wiki_dir)                  — (re)embed every page in the wiki
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

log = logging.getLogger("wiki.vector_store")

_STORE_FILE   = "vector_store.json"
_MAX_BODY_CHARS = 3000   # chars of body to embed (after frontmatter)


# ── Embedding ────────────────────────────────────────────────────────────────

def _embed(text: str) -> list[float]:
    from ..config import GOOGLE_API_KEY
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is required for vector search")
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    result = client.models.embed_content(
        model="models/gemini-embedding-001",
        contents=text,
    )
    return list(result.embeddings[0].values)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Text extraction ──────────────────────────────────────────────────────────

def _extract_embed_text(content: str, page_path: str) -> str:
    """Return title + body text suitable for embedding."""
    title = Path(page_path).stem.replace("-", " ").title()
    fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            if line.lower().startswith("title:"):
                t = line.split(":", 1)[1].strip().strip("\"'")
                if t:
                    title = t
                break
        body = content[fm.end():].strip()
    else:
        body = content.strip()

    return f"{title}\n\n{body[:_MAX_BODY_CHARS]}"


# ── Store I/O ────────────────────────────────────────────────────────────────

def _load(wiki_dir: Path) -> list[dict]:
    p = wiki_dir / _STORE_FILE
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Vector store corrupt, resetting: %s", e)
        return []


def _save(wiki_dir: Path, records: list[dict]) -> None:
    (wiki_dir / _STORE_FILE).write_text(
        json.dumps(records, separators=(",", ":")),
        encoding="utf-8",
    )


# ── Public API ───────────────────────────────────────────────────────────────

def upsert(page_path: str, content: str, wiki_dir: Path) -> bool:
    """
    Embed the page and store/update its vector.
    page_path is relative to wiki_dir, e.g. "concepts/aki.md".
    Returns True on success, False if embedding failed.
    """
    text = _extract_embed_text(content, page_path)
    try:
        embedding = _embed(text)
    except Exception as e:
        log.warning("Embed failed for %s: %s", page_path, e)
        return False

    records = _load(wiki_dir)
    updated = False
    for rec in records:
        if rec["path"] == page_path:
            rec["embedding"] = embedding
            updated = True
            break
    if not updated:
        records.append({"path": page_path, "embedding": embedding})
    _save(wiki_dir, records)
    log.debug("Vector %s: %s", "updated" if updated else "added", page_path)
    # Also index at section level so search_sections() can find this page
    upsert_sections(page_path, content, wiki_dir)
    return True


def search(query: str, wiki_dir: Path, top_k: int = 8, include_patients: bool = False) -> list[dict]:
    """
    Return top_k pages most semantically similar to query.
    Each result: {"path": "concepts/aki.md", "score": 0.87}
    Returns [] if the store is empty or embedding fails.
    Set include_patients=True to also search patient-specific pages.
    """
    records = _load(wiki_dir)
    if not records:
        return []

    try:
        q_emb = _embed(query)
    except Exception as e:
        log.warning("Query embed failed: %s", e)
        return []

    scored = sorted(
        [
            {"path": r["path"], "score": _cosine(q_emb, r["embedding"])}
            for r in records
            if include_patients or not r["path"].startswith("patients/")
        ],
        key=lambda x: x["score"],
        reverse=True,
    )
    log.info(
        "Vector search: top scores %s",
        [f'{h["path"].split("/")[-1]}={h["score"]:.3f}' for h in scored[:top_k]],
    )
    return scored[:top_k]


def remove(page_path: str, wiki_dir: Path) -> None:
    """Remove a page's vector (call when a page is deleted)."""
    records = _load(wiki_dir)
    records = [r for r in records if r["path"] != page_path]
    _save(wiki_dir, records)


def rename_path(old_path: str, new_path: str, wiki_dir: Path) -> None:
    """Update the stored path key when a file is moved (e.g. by mop-up)."""
    records = _load(wiki_dir)
    for r in records:
        if r["path"] == old_path:
            r["path"] = new_path
    _save(wiki_dir, records)


def count(wiki_dir: Path) -> int:
    return len(_load(wiki_dir))


# ── Section-level indexing ────────────────────────────────────────────────────
# Stored separately in vector_store_sections.json so existing page-level API
# is fully backward-compatible. upsert() calls upsert_sections() automatically,
# so callers only need to change if they want section-level search results.

_SECTIONS_FILE = "vector_store_sections.json"
_MIN_SECTION_CHARS = 60   # skip sections shorter than this (too thin to embed usefully)
_MAX_SECTION_CHARS = 1500  # chars of section body to embed


def _load_sections(wiki_dir: Path) -> list[dict]:
    p = wiki_dir / _SECTIONS_FILE
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Section store corrupt, resetting: %s", e)
        return []


def _save_sections(wiki_dir: Path, records: list[dict]) -> None:
    (wiki_dir / _SECTIONS_FILE).write_text(
        json.dumps(records, separators=(",", ":")),
        encoding="utf-8",
    )


def _split_sections(page_path: str, content: str) -> list[dict]:
    """
    Split page markdown into sections on ## headings.
    Returns list of {"heading": str, "body": str}.
    Falls back to a single chunk using the page title if no ## sections exist.
    Skips frontmatter and sections with < _MIN_SECTION_CHARS of body text.
    """
    # Strip frontmatter
    fm = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    body = content[fm.end():].strip() if fm else content.strip()

    sections: list[dict] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in body.splitlines():
        if line.startswith("## "):
            if current_heading is not None:
                body_text = "\n".join(current_lines).strip()
                if len(body_text) >= _MIN_SECTION_CHARS:
                    sections.append({"heading": current_heading, "body": body_text})
            current_heading = line[3:].strip()
            current_lines = []
        elif current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        body_text = "\n".join(current_lines).strip()
        if len(body_text) >= _MIN_SECTION_CHARS:
            sections.append({"heading": current_heading, "body": body_text})

    # Fallback: embed the whole body as one chunk
    if not sections and len(body) >= _MIN_SECTION_CHARS:
        title = Path(page_path).stem.replace("-", " ").title()
        sections.append({"heading": title, "body": body[:_MAX_SECTION_CHARS]})

    return sections


def _section_chunk_id(page_path: str, heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return f"{page_path}#{slug}"


def upsert_sections(page_path: str, content: str, wiki_dir: Path) -> int:
    """
    Embed each ## section of the page and store in the section store.
    Replaces any existing sections for this page_path.
    Returns number of sections embedded.
    """
    sections = _split_sections(page_path, content)
    if not sections:
        return 0

    records = _load_sections(wiki_dir)
    # Remove existing records for this page
    records = [r for r in records if r["path"] != page_path]

    embedded = 0
    for sec in sections:
        embed_text = f"{sec['heading']}\n\n{sec['body'][:_MAX_SECTION_CHARS]}"
        try:
            emb = _embed(embed_text)
        except Exception as e:
            log.warning("Section embed failed for %s#%s: %s", page_path, sec["heading"], e)
            continue
        records.append({
            "chunk_id": _section_chunk_id(page_path, sec["heading"]),
            "path": page_path,
            "section": sec["heading"],
            "embedding": emb,
        })
        embedded += 1

    _save_sections(wiki_dir, records)
    log.debug("Section vectors: %d sections for %s", embedded, page_path)
    return embedded


_DEMAND_ALPHA   = 0.10   # max fractional boost (10%)
_DEMAND_GAP_NORM   = 10  # gap_opens at which boost saturates to ~76% of max
_DEMAND_QUERY_NORM = 5   # cds_query_count at which boost saturates


def _demand_boost(page_path: str, metrics: dict) -> float:
    """
    Return a small additive multiplier [0, _DEMAND_ALPHA] for a page based on
    how often it is queried and how often it still fails to provide values.
    Uses tanh so the signal saturates rather than growing unboundedly.
    """
    entry = metrics.get(page_path, {})
    gap_opens   = entry.get("gap_opens", 0)
    query_count = entry.get("cds_query_count", 0)
    if gap_opens == 0 and query_count == 0:
        return 0.0
    gap_signal   = math.tanh(gap_opens   / _DEMAND_GAP_NORM)
    query_signal = math.tanh(query_count / _DEMAND_QUERY_NORM)
    # Both signals must be non-zero for a boost — a page with gaps but zero
    # queries might just be thinly covered; a queried page with zero gaps is fine.
    return _DEMAND_ALPHA * (gap_signal * query_signal) ** 0.5


def search_sections(
    query: str,
    wiki_dir: Path,
    top_k: int = 8,
    include_patients: bool = False,
) -> list[dict]:
    """
    Search the section-level store.
    Returns top_k hits: [{"chunk_id": ..., "path": ..., "section": ..., "score": ...}]
    Falls back to page-level search if section store is empty.

    Scores are boosted by a small demand signal: pages that are frequently
    retrieved but still have open gaps get up to _DEMAND_ALPHA (10%) added to
    their cosine similarity, lifting them above retrieval thresholds.
    """
    records = _load_sections(wiki_dir)
    if not records:
        # Graceful fallback to page-level — section store not yet built
        page_hits = search(query, wiki_dir, top_k=top_k, include_patients=include_patients)
        return [
            {"chunk_id": h["path"], "path": h["path"], "section": "", "score": h["score"]}
            for h in page_hits
        ]

    try:
        q_emb = _embed(query)
    except Exception as e:
        log.warning("Section search embed failed: %s", e)
        return []

    # Load metrics once for all pages — cheap JSON read
    try:
        from .page_metrics import _load as _load_metrics
        metrics = _load_metrics(wiki_dir)
    except Exception:
        metrics = {}

    scored = sorted(
        [
            {
                "chunk_id": r["chunk_id"],
                "path": r["path"],
                "section": r["section"],
                "score": _cosine(q_emb, r["embedding"]) * (1 + _demand_boost(r["path"], metrics)),
            }
            for r in records
            if include_patients or not r["path"].startswith("patients/")
        ],
        key=lambda x: x["score"],
        reverse=True,
    )
    log.info(
        "Section search top hits: %s",
        [f'{h["path"].split("/")[-1]}#{h["section"]}={h["score"]:.3f}' for h in scored[:top_k]],
    )
    return scored[:top_k]


def extract_section(content: str, heading: str) -> str:
    """
    Extract the body text of a named ## section from page markdown.
    Returns empty string if section not found.
    """
    lines = content.splitlines()
    in_section = False
    body_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break  # hit next section
            if line[3:].strip().lower() == heading.lower():
                in_section = True
        elif in_section:
            body_lines.append(line)
    return "\n".join(body_lines).strip()


def remove_sections(page_path: str, wiki_dir: Path) -> None:
    """Remove all section records for a page (call alongside remove())."""
    records = _load_sections(wiki_dir)
    records = [r for r in records if r["path"] != page_path]
    _save_sections(wiki_dir, records)


def rebuild_all(wiki_dir: Path) -> dict:
    """
    (Re)embed every .md page in entities/, concepts/, sources/, queries/.
    Skips pages that fail to embed. Returns a summary dict.
    """
    ok = 0
    failed = 0
    for section in ("entities", "concepts", "sources", "queries"):
        # "patients" is intentionally excluded — patient pages must not pollute
        # the general-knowledge vector store
        section_dir = wiki_dir / section
        if not section_dir.exists():
            continue
        for f in sorted(section_dir.glob("*.md")):
            page_path = f"{section}/{f.name}"
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                failed += 1
                continue
            if upsert(page_path, content, wiki_dir):
                ok += 1
                log.info("Rebuilt vector: %s", page_path)
            else:
                failed += 1

    log.info("Vector rebuild complete: %d ok, %d failed", ok, failed)
    return {"embedded": ok, "failed": failed}
