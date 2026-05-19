"""Chunk model for RAG note retrieval."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    doc_id: str
    chunk_index: int
    text: str
    note_time: str
    note_type: str
    author: str
    metadata: dict[str, Any] | None = None

    def to_citation(self) -> str:
        return f"{self.doc_id}:{self.chunk_index} (note_time={self.note_time})"
