"""Load a Radar patient JSON into AdmissionStore with FAISS note index."""
from __future__ import annotations

import json
from typing import Any

from .radar_adapter import normalize_patient
from .rag_index import build_index


class AdmissionStore:
    """Holds normalized DataFrames, text chunks, and FAISS vector index for one admission."""

    def __init__(
        self,
        admission_id: str,
        start_time: Any,
        end_time: Any,
        dfs: dict[str, Any],
        text_chunks: list | None = None,
        vector_index: Any = None,
    ):
        self.admission_id  = admission_id
        self.start_time    = start_time
        self.end_time      = end_time
        self.dfs           = dfs
        self.text_chunks   = text_chunks or []
        self.vector_index  = vector_index


def load_admission(patient_json: dict | str | bytes) -> AdmissionStore:
    """
    Accept a Radar patient dict (or JSON bytes/string), normalize into DataFrames,
    build FAISS note index, and return AdmissionStore.
    """
    if isinstance(patient_json, (bytes, str)):
        raw = json.loads(patient_json if isinstance(patient_json, str) else patient_json.decode())
    else:
        raw = patient_json

    if isinstance(raw, list):
        patient = raw[0] if raw else {}
    else:
        patient = raw

    if not patient:
        raise ValueError("Empty patient JSON")

    norm = normalize_patient(patient)
    dfs = {
        "labs":           norm["labs_df"],
        "vitals":         norm["vitals_df"],
        "med_admin":      norm["med_admin_df"],
        "io":             norm["io_df"],
        "notes":          norm["notes_df"],
        "diets":          norm["diets_df"],
        "procedures":     norm["procedures_df"],
        "communications": norm["communications_df"],
        "lab_orders":     norm["lab_orders_df"],
        "bloods":         norm["bloods_df"],
        "vents":          norm["vents_df"],
    }
    store = AdmissionStore(
        admission_id=norm["admission_id"],
        start_time=norm["start_time"],
        end_time=norm["end_time"],
        dfs=dfs,
    )
    build_index(store)
    return store
