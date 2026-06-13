"""Semantic search over a patient's indexed notes (uses local FAISS cache)."""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _dedup_chunks(chunks: list) -> list:
    """
    Drop near-identical chunks caused by copy-pasted progress note summaries.
    Keeps the first occurrence of each fingerprint (earliest retrieval rank).
    Fingerprint = first 250 chars of HTML-stripped, lowercased, whitespace-collapsed text.
    """
    seen: set[str] = set()
    out = []
    dropped = 0
    for c in chunks:
        normalized = re.sub(r'\s+', ' ', re.sub(r'&\w+;', ' ', c.text.lower())).strip()
        fp = normalized[:250]
        if fp not in seen:
            seen.add(fp)
            out.append(c)
        else:
            dropped += 1
    if dropped:
        logger.debug("query_notes: dedup dropped %d duplicate chunk(s)", dropped)
    return out


def _load_store(cpmrn: str, encounter: int):
    """Load the FAISS note store for this patient. Returns store or None."""
    from .notes_module.admission_loader import AdmissionStore
    from .notes_module.mongo_cache import load_index, index_exists

    admission_id = f"{cpmrn}_{encounter}"
    if not index_exists(admission_id):
        return None, f"No note index found for {cpmrn} encounter {encounter}. Run sync first."

    store = AdmissionStore(admission_id=admission_id, start_time=None, end_time=None, dfs={})
    if not load_index(store, admission_id):
        return None, "Failed to load note index from cache."

    return store, None


def query_patient_notes(cpmrn: str, encounter: int, question: str, k: int = 5) -> str:
    """
    Semantic search over all clinician notes for this patient.
    Returns plain formatted text (no chunk objects) — use for display only.
    For citation-aware retrieval use query_patient_notes_with_chunks().
    """
    text, _ = query_patient_notes_with_chunks(cpmrn, encounter, question, k)
    return text


def query_patient_notes_with_chunks(
    cpmrn: str,
    encounter: int,
    question: str,
    k: int = 5,
    start_index: int = 0,
) -> tuple[str, list]:
    """
    Semantic search over all clinician notes.
    Returns (formatted_text, chunks) where:
      - formatted_text has each chunk prefixed with [N] where N = start_index + i
      - chunks is the raw list of Chunk objects in the same order

    start_index lets callers maintain a global numbering across multiple calls
    so the model sees [0], [1], [2]... across the entire session.
    """
    from .notes_module.rag_index import retrieve

    store, err = _load_store(cpmrn, encounter)
    if store is None:
        return err, []

    chunks = _dedup_chunks(retrieve(store, question, k=k))
    if not chunks:
        return "No relevant notes found.", []

    parts = []
    for i, chunk in enumerate(chunks):
        idx = start_index + i
        header = f"[{idx}] {chunk.note_time or 'unknown time'} | {chunk.note_type or 'note'}"
        if chunk.author:
            header += f" | {chunk.author}"
        parts.append(f"{header}\n{chunk.text}")

    return "\n\n".join(parts), chunks
