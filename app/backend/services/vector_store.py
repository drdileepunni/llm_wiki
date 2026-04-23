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
    for rec in records:
        if rec["path"] == page_path:
            rec["embedding"] = embedding
            _save(wiki_dir, records)
            log.debug("Vector updated: %s", page_path)
            return True

    records.append({"path": page_path, "embedding": embedding})
    _save(wiki_dir, records)
    log.debug("Vector added: %s", page_path)
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
