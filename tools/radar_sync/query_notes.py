"""Semantic search over a patient's indexed notes (uses local FAISS cache)."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def query_patient_notes(cpmrn: str, encounter: int, question: str, k: int = 5) -> str:
    """
    Semantic search over all clinician notes for this patient.
    Returns formatted note excerpts with timestamps and note types.
    Requires the note index to already exist in MongoDB (built by sync pipeline).
    """
    from .notes_module.admission_loader import AdmissionStore
    from .notes_module.mongo_cache import load_index, index_exists
    from .notes_module.rag_index import retrieve

    admission_id = f"{cpmrn}_{encounter}"
    if not index_exists(admission_id):
        return f"No note index found for {cpmrn} encounter {encounter}. Run sync first."

    # Build a minimal store to host the index
    store = AdmissionStore(admission_id=admission_id, start_time=None, end_time=None, dfs={})
    if not load_index(store, admission_id):
        return "Failed to load note index from cache."

    chunks = retrieve(store, question, k=k)
    if not chunks:
        return "No relevant notes found."

    parts = []
    for chunk in chunks:
        header = f"[{chunk.note_time or 'unknown time'} | {chunk.note_type or 'note'}"
        if chunk.author:
            header += f" | {chunk.author}"
        header += "]"
        parts.append(f"{header}\n{chunk.text}")

    return "\n\n".join(parts)
