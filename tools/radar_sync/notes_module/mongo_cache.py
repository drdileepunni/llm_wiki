"""MongoDB-backed cache for per-patient FAISS note indexes (replaces GCS cache)."""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import bson

if TYPE_CHECKING:
    from .admission_loader import AdmissionStore

logger = logging.getLogger(__name__)


def _get_collection():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))
    from backend.services.emr.db import get_db
    return get_db()["note_indexes"]


def compute_notes_hash(chart: dict) -> str:
    """SHA-256 of sorted (reportedAt, content) pairs from note documents."""
    docs = chart.get("documents") or []
    notes = sorted(
        (str(d.get("reportedAt", "")), str(d.get("content", "") or d.get("text", "")))
        for d in docs
        if d.get("category") == "notes"
    )
    raw = repr(notes).encode()
    return hashlib.sha256(raw).hexdigest()


def get_stored_notes_hash(admission_id: str) -> str | None:
    """Return the notes_hash stored with the last index, or None if not present."""
    col = _get_collection()
    doc = col.find_one({"admission_id": admission_id}, {"notes_hash": 1})
    return doc.get("notes_hash") if doc else None


def index_exists(admission_id: str) -> bool:
    col = _get_collection()
    return col.count_documents({"admission_id": admission_id}, limit=1) > 0


def save_index(store: "AdmissionStore", admission_id: str, notes_hash: str | None = None) -> None:
    """Serialize FAISS index + chunks into MongoDB note_indexes collection."""
    if store.vector_index is None:
        logger.warning("save_index: no vector_index on store for %s, skipping", admission_id)
        return
    import faiss
    index_bytes = faiss.serialize_index(store.vector_index)
    chunks_data = [dataclasses.asdict(c) for c in store.text_chunks]
    col = _get_collection()
    fields = {
        "admission_id": admission_id,
        "index_bytes":  bson.Binary(bytes(index_bytes)),
        "chunks":       chunks_data,
        "indexed_at":   datetime.now(timezone.utc),
        "note_count":   len(store.text_chunks),
    }
    if notes_hash is not None:
        fields["notes_hash"] = notes_hash
    col.update_one(
        {"admission_id": admission_id},
        {"$set": fields},
        upsert=True,
    )
    logger.info("save_index: saved %d chunks for %s", len(store.text_chunks), admission_id)


def load_index(store: "AdmissionStore", admission_id: str) -> bool:
    """Load FAISS index + chunks from MongoDB into store. Returns True on success."""
    from .chunk import Chunk
    import faiss
    col = _get_collection()
    doc = col.find_one({"admission_id": admission_id})
    if not doc:
        return False
    try:
        import numpy as np
        raw = bytes(doc["index_bytes"])
        store.vector_index = faiss.deserialize_index(np.frombuffer(raw, dtype=np.uint8))
        store.text_chunks  = [Chunk(**c) for c in doc.get("chunks", [])]
        logger.info("load_index: loaded %d chunks for %s", len(store.text_chunks), admission_id)
        return True
    except Exception:
        logger.exception("load_index: failed for %s", admission_id)
        return False
