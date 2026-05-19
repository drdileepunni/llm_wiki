"""Chunk notes, embed with Gemini, build FAISS index; retrieve top-k chunks."""
import logging
import os
import re
import time
from typing import Any

from .chunk import Chunk

logger = logging.getLogger(__name__)

# ── Config (env-overridable) ──────────────────────────────────────────────────
CHUNK_TOKEN_TARGET   = int(os.environ.get("CHUNK_TOKEN_TARGET",   "600"))
CHUNK_OVERLAP_TOKENS = int(os.environ.get("CHUNK_OVERLAP_TOKENS", "80"))
RAG_TOP_K            = int(os.environ.get("RAG_TOP_K",            "6"))
GEMINI_EMBEDDING_MODEL = os.environ.get("GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-001")

CHARS_PER_TOKEN = 4  # fallback when tiktoken unavailable

try:
    import tiktoken
    _TIKTOKEN_AVAILABLE = True
except ImportError:
    _TIKTOKEN_AVAILABLE = False


def _gemini_api_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def _estimate_tokens(text: str) -> int:
    if _TIKTOKEN_AVAILABLE:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── Text chunking ─────────────────────────────────────────────────────────────

def _tokenize_count(text: str) -> int:
    return _estimate_tokens(text)


def _chunk_text(text: str, target: int = CHUNK_TOKEN_TARGET, overlap: int = CHUNK_OVERLAP_TOKENS) -> list[str]:
    if not text or not text.strip():
        return []
    if _TIKTOKEN_AVAILABLE:
        try:
            enc = tiktoken.get_encoding("cl100k_base")
            tokens = enc.encode(text)
            step = max(1, target - overlap)
            chunks = []
            for i in range(0, len(tokens), step):
                chunk_tokens = tokens[i : i + target]
                if not chunk_tokens:
                    break
                chunks.append(enc.decode(chunk_tokens))
            return chunks
        except Exception:
            pass
    overlap_chars = overlap * CHARS_PER_TOKEN
    step = max(1, target * CHARS_PER_TOKEN - overlap_chars)
    chunks, start = [], 0
    while start < len(text):
        end = min(start + target * CHARS_PER_TOKEN, len(text))
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        start += step
    return chunks


def build_note_chunks(store: Any) -> list[Chunk]:
    """Build Chunk list from store.dfs['notes'] DataFrame."""
    notes_df = store.dfs.get("notes")
    if notes_df is None or notes_df.empty:
        return []
    chunks_out = []
    for note_idx, row in notes_df.iterrows():
        doc_id     = f"{store.admission_id}:{note_idx}"
        note_time  = str(row.get("time", ""))
        note_type  = str(row.get("type", ""))
        author     = str(row.get("author", ""))
        text       = str(row.get("text", ""))
        if not text.strip():
            continue
        for ci, chunk_text in enumerate(_chunk_text(text)):
            chunks_out.append(Chunk(
                doc_id=doc_id,
                chunk_index=ci,
                text=chunk_text,
                note_time=note_time,
                note_type=note_type,
                author=author,
            ))
    return chunks_out


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_chunks(chunks: list[Chunk]) -> list[list[float]]:
    """Embed all chunks via Gemini batch API with exponential backoff."""
    if not chunks:
        return []
    from google import genai
    api_key = _gemini_api_key()
    if not api_key:
        logger.warning("embed_chunks: no Gemini/Google API key set, skipping")
        return []
    genai_client = genai.Client(api_key=api_key)

    MAX_TOKENS_PER_BATCH = 20_000
    MAX_TEXTS_PER_BATCH  = 250
    texts = [c.text for c in chunks]
    all_embeddings: list[list[float]] = []
    i = 0

    while i < len(texts):
        batch_texts, batch_tokens = [], 0
        while i < len(texts) and len(batch_texts) < MAX_TEXTS_PER_BATCH:
            t = texts[i]
            tok = _estimate_tokens(t)
            if batch_tokens + tok > MAX_TOKENS_PER_BATCH and batch_texts:
                break
            batch_texts.append(t)
            batch_tokens += tok
            i += 1

        result = _embed_with_retry(genai_client, batch_texts)
        if result is not None:
            all_embeddings.extend(result)
        if i < len(texts):
            time.sleep(1)

    return all_embeddings


def _embed_with_retry(client, texts: list[str]) -> list[list[float]] | None:
    """Batch embed with exponential backoff; falls back to one-by-one on failure."""
    for attempt in range(5):
        try:
            r = client.models.embed_content(model=GEMINI_EMBEDDING_MODEL, contents=texts)
            return _extract_embeddings(r)
        except Exception as e:
            e_str = str(e)
            is_rate = any(k in e_str for k in ("ResourceExhausted", "429", "rate limit", "quota"))
            is_timeout = any(k in e_str for k in ("DeadlineExceeded", "504", "timeout", "deadline"))
            if (is_rate or is_timeout) and attempt < 4:
                delay = (10 if is_rate else 2) * (2 ** attempt)
                logger.warning("embed API error (attempt %d/5), retrying in %ds: %s", attempt + 1, delay, e_str[:80])
                time.sleep(delay)
            else:
                logger.error("embed batch failed after retries: %s", e_str[:120])
                # fall back one-by-one
                results = []
                for t in texts:
                    try:
                        r = client.models.embed_content(model=GEMINI_EMBEDDING_MODEL, contents=[t])
                        embs = _extract_embeddings(r)
                        if embs:
                            results.extend(embs)
                        time.sleep(0.3)
                    except Exception:
                        pass
                return results if results else None
    return None


def _extract_embeddings(r) -> list[list[float]]:
    if isinstance(r, dict):
        emb = r.get("embedding") or r.get("embeddings")
    else:
        emb = getattr(r, "embeddings", None) or getattr(r, "embedding", None)
    if emb is None:
        return []
    # google.genai returns list of ContentEmbedding objects with .values
    if isinstance(emb, list) and emb and hasattr(emb[0], "values"):
        return [list(e.values) for e in emb]
    if isinstance(emb, list) and emb and isinstance(emb[0], (list, tuple)):
        return list(emb)
    if isinstance(emb, list) and emb and isinstance(emb[0], float):
        return [emb]
    return []


# ── FAISS index ───────────────────────────────────────────────────────────────

def build_faiss_index(embeddings: list[list[float]]):
    """Build and return faiss.IndexFlatL2, or None if embeddings are empty."""
    if not embeddings:
        return None
    import numpy as np
    import faiss
    x = np.array(embeddings, dtype=np.float32)
    index = faiss.IndexFlatL2(x.shape[1])
    index.add(x)
    return index


def build_index(store: Any) -> None:
    """Build and attach text_chunks + vector_index to store in-place."""
    store.text_chunks = build_note_chunks(store)
    if not store.text_chunks:
        store.vector_index = None
        logger.info("build_index: no note chunks found")
        return
    if not _gemini_api_key():
        store.vector_index = None
        logger.warning("build_index: no Gemini API key, skipping embeddings")
        return
    try:
        logger.info("build_index: embedding %d chunks", len(store.text_chunks))
        embeddings = embed_chunks(store.text_chunks)
        store.vector_index = build_faiss_index(embeddings)
        logger.info("build_index: done, %d vectors", len(embeddings))
    except Exception:
        store.vector_index = None
        logger.exception("build_index: failed")


# ── Retrieval ─────────────────────────────────────────────────────────────────

def retrieve(store: Any, query: str, k: int = RAG_TOP_K) -> list[Chunk]:
    """Embed query, search FAISS, return top-k Chunk objects."""
    if not store.vector_index or not store.text_chunks:
        return []
    from google import genai
    import numpy as np
    api_key = _gemini_api_key()
    if not api_key:
        return []
    client = genai.Client(api_key=api_key)
    r = None
    for attempt in range(5):
        try:
            r = client.models.embed_content(model=GEMINI_EMBEDDING_MODEL, contents=[query])
            break
        except Exception as e:
            e_str = str(e)
            is_rate = any(kw in e_str for kw in ("ResourceExhausted", "429", "rate limit", "quota"))
            is_timeout = any(kw in e_str for kw in ("DeadlineExceeded", "504", "timeout", "deadline"))
            if (is_rate or is_timeout) and attempt < 4:
                delay = (10 if is_rate else 2) * (2 ** attempt)
                time.sleep(delay)
            else:
                raise
    if r is None:
        return []
    embs = _extract_embeddings(r)
    if not embs:
        return []
    q_vec = np.array([embs[0]], dtype=np.float32)
    _, indices = store.vector_index.search(q_vec, min(k, len(store.text_chunks)))
    return [store.text_chunks[idx] for idx in indices[0] if 0 <= idx < len(store.text_chunks)][:k]
