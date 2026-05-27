"""GCS-backed cache for per-patient FAISS note indexes."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .admission_loader import AdmissionStore

log = logging.getLogger(__name__)

_PREFIX = "note_indexes"


def _bucket():
    from google.cloud import storage
    bucket_name = os.environ.get("GCS_BUCKET", "cds-pipeline-ops")
    return storage.Client().bucket(bucket_name)


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
    """Return the notes_hash stored with the last index, or None."""
    blob = _bucket().blob(f"{_PREFIX}/{admission_id}.json")
    if not blob.exists():
        return None
    try:
        meta = json.loads(blob.download_as_text())
        return meta.get("notes_hash")
    except Exception:
        return None


def index_exists(admission_id: str) -> bool:
    return _bucket().blob(f"{_PREFIX}/{admission_id}.faiss").exists()


def save_index(store: "AdmissionStore", admission_id: str, notes_hash: str | None = None) -> None:
    """Serialise FAISS index + chunks to GCS."""
    if store.vector_index is None:
        log.warning("save_index: no vector_index on store for %s, skipping", admission_id)
        return
    import faiss
    import numpy as np

    bucket = _bucket()

    # Binary FAISS index
    index_bytes = bytes(faiss.serialize_index(store.vector_index))
    bucket.blob(f"{_PREFIX}/{admission_id}.faiss").upload_from_string(
        index_bytes, content_type="application/octet-stream"
    )

    # JSON metadata + chunks
    chunks_data = [dataclasses.asdict(c) for c in store.text_chunks]
    meta = {
        "admission_id": admission_id,
        "chunks":       chunks_data,
        "indexed_at":   datetime.now(timezone.utc).isoformat(),
        "note_count":   len(store.text_chunks),
    }
    if notes_hash is not None:
        meta["notes_hash"] = notes_hash
    bucket.blob(f"{_PREFIX}/{admission_id}.json").upload_from_string(
        json.dumps(meta), content_type="application/json"
    )
    log.info("save_index: saved %d chunks for %s", len(store.text_chunks), admission_id)


def load_index(store: "AdmissionStore", admission_id: str) -> bool:
    """Load FAISS index + chunks from GCS into store. Returns True on success."""
    from .chunk import Chunk
    import faiss
    import numpy as np

    bucket = _bucket()
    faiss_blob = bucket.blob(f"{_PREFIX}/{admission_id}.faiss")
    meta_blob  = bucket.blob(f"{_PREFIX}/{admission_id}.json")

    if not faiss_blob.exists():
        return False
    try:
        raw_bytes = faiss_blob.download_as_bytes()
        store.vector_index = faiss.deserialize_index(np.frombuffer(raw_bytes, dtype=np.uint8))
        if meta_blob.exists():
            meta = json.loads(meta_blob.download_as_text())
            store.text_chunks = [Chunk(**c) for c in meta.get("chunks", [])]
        else:
            store.text_chunks = []
        log.info("load_index: loaded %d chunks for %s", len(store.text_chunks), admission_id)
        return True
    except Exception:
        log.exception("load_index: failed for %s", admission_id)
        return False
