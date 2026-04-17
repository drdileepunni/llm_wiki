import logging
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from ..config import WIKI_DIR, WIKI_ROOT

router = APIRouter(prefix="/api/wiki", tags=["wiki"])
log = logging.getLogger("wiki.router")

# Files hidden from the tree (system/admin pages)
HIDDEN_FILES = {"index.md", "log.md", "overview.md"}

SECTION_ORDER = ["sources", "entities", "concepts", "queries"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe_path(rel: str) -> Path:
    resolved = (WIKI_ROOT / rel).resolve()
    if not str(resolved).startswith(str(WIKI_DIR.resolve())):
        raise HTTPException(status_code=403, detail="Path outside wiki directory")
    return resolved


def _build_tree(directory: Path) -> list[dict]:
    items = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return items

    for entry in entries:
        if entry.name in HIDDEN_FILES:
            continue
        rel = str(entry.relative_to(WIKI_ROOT)).replace("\\", "/")
        if entry.is_dir():
            children = _build_tree(entry)
            items.append({"name": entry.name, "type": "directory", "path": rel, "children": children})
        elif entry.suffix in (".md", ".txt"):
            items.append({"name": entry.name, "type": "file", "path": rel})
    return items


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/tree")
def get_tree():
    tree = []
    for section in SECTION_ORDER:
        section_dir = WIKI_DIR / section
        if section_dir.exists():
            rel = str(section_dir.relative_to(WIKI_ROOT)).replace("\\", "/")
            tree.append({
                "name": section,
                "type": "directory",
                "path": rel,
                "children": _build_tree(section_dir),
            })
    # Any extra folders not in the ordered list
    for entry in sorted(WIKI_DIR.iterdir()):
        if entry.is_dir() and entry.name not in SECTION_ORDER:
            rel = str(entry.relative_to(WIKI_ROOT)).replace("\\", "/")
            tree.append({
                "name": entry.name,
                "type": "directory",
                "path": rel,
                "children": _build_tree(entry),
            })
    return {"tree": tree}


@router.get("/search")
def search_wiki(q: str = Query(..., min_length=1)):
    """Full-text search across all wiki markdown files."""
    q_lower = q.lower().strip()
    results = []

    for md_file in sorted(WIKI_DIR.rglob("*.md")):
        if md_file.name in HIDDEN_FILES:
            continue
        rel = str(md_file.relative_to(WIKI_ROOT)).replace("\\", "/")
        name = md_file.stem

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        content_lower = content.lower()
        name_match = q_lower in name.lower()
        content_match = q_lower in content_lower

        if not name_match and not content_match:
            continue

        # Build a short excerpt around the first hit in the content
        idx = content_lower.find(q_lower)
        if idx >= 0:
            start = max(0, idx - 60)
            end   = min(len(content), idx + len(q) + 80)
            raw   = content[start:end].replace("\n", " ").strip()
            # Highlight the match with ** markers (rendered as bold in the UI)
            hit_start = idx - start
            hit_end   = hit_start + len(q)
            excerpt = raw[:hit_start] + "**" + raw[hit_start:hit_end] + "**" + raw[hit_end:]
            if start > 0:
                excerpt = "…" + excerpt
            if end < len(content):
                excerpt += "…"
        else:
            excerpt = content[:120].replace("\n", " ").strip() + "…"

        results.append({
            "path":       rel,
            "name":       name,
            "excerpt":    excerpt,
            "name_match": name_match,
        })

    # Name matches first, then alphabetical
    results.sort(key=lambda r: (not r["name_match"], r["name"]))
    return {"results": results[:50], "total": len(results)}


@router.get("/file")
def read_file(path: str = Query(...)):
    full = _safe_path(path)
    if not full.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    log.info("Read wiki file: %s", path)
    return {"path": path, "content": full.read_text(encoding="utf-8")}


class SavePayload(BaseModel):
    path: str
    content: str


@router.put("/file")
def save_file(payload: SavePayload):
    full = _safe_path(payload.path)
    if not full.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {payload.path}")
    full.write_text(payload.content, encoding="utf-8")
    log.info("Saved wiki file: %s  (%d chars)", payload.path, len(payload.content))
    return {"saved": True, "path": payload.path}
