import asyncio
import logging
import re
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Depends
from pydantic import BaseModel
from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services import vector_store as vs_mod

router = APIRouter(prefix="/api/wiki", tags=["wiki"])
log = logging.getLogger("wiki.router")

HIDDEN_FILES = {"index.md", "log.md", "overview.md"}
SECTION_ORDER = ["sources", "entities", "concepts", "queries", "gaps"]


def _safe_path(rel: str, kb: KBConfig) -> Path:
    resolved = (kb.wiki_root / rel).resolve()
    if not str(resolved).startswith(str(kb.wiki_dir.resolve())):
        raise HTTPException(status_code=403, detail="Path outside wiki directory")
    return resolved


def _build_tree(directory: Path, wiki_root: Path) -> list[dict]:
    items = []
    try:
        entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return items

    for entry in entries:
        if entry.name in HIDDEN_FILES:
            continue
        rel = str(entry.relative_to(wiki_root)).replace("\\", "/")
        if entry.is_dir():
            children = _build_tree(entry, wiki_root)
            items.append({"name": entry.name, "type": "directory", "path": rel, "children": children})
        elif entry.suffix in (".md", ".txt"):
            items.append({"name": entry.name, "type": "file", "path": rel})
    return items


@router.get("/tree")
def get_tree(kb: KBConfig = Depends(resolve_kb)):
    tree = []
    for section in SECTION_ORDER:
        section_dir = kb.wiki_dir / section
        if section_dir.exists():
            rel = str(section_dir.relative_to(kb.wiki_root)).replace("\\", "/")
            tree.append({
                "name": section,
                "type": "directory",
                "path": rel,
                "children": _build_tree(section_dir, kb.wiki_root),
            })
    for entry in sorted(kb.wiki_dir.iterdir()):
        if entry.is_dir() and entry.name not in SECTION_ORDER:
            rel = str(entry.relative_to(kb.wiki_root)).replace("\\", "/")
            tree.append({
                "name": entry.name,
                "type": "directory",
                "path": rel,
                "children": _build_tree(entry, kb.wiki_root),
            })
    return {"tree": tree}


@router.get("/search")
def search_wiki(q: str = Query(..., min_length=1), kb: KBConfig = Depends(resolve_kb)):
    q_lower = q.lower().strip()
    results = []

    for md_file in sorted(kb.wiki_dir.rglob("*.md")):
        if md_file.name in HIDDEN_FILES:
            continue
        rel = str(md_file.relative_to(kb.wiki_root)).replace("\\", "/")
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

        idx = content_lower.find(q_lower)
        if idx >= 0:
            start = max(0, idx - 60)
            end   = min(len(content), idx + len(q) + 80)
            raw   = content[start:end].replace("\n", " ").strip()
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

    results.sort(key=lambda r: (not r["name_match"], r["name"]))
    return {"results": results[:50], "total": len(results)}


@router.get("/file")
def read_file(path: str = Query(...), kb: KBConfig = Depends(resolve_kb)):
    full = _safe_path(path, kb)
    if not full.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    log.info("Read wiki file: %s  kb=%s", path, kb.name)
    return {"path": path, "content": full.read_text(encoding="utf-8")}


class SavePayload(BaseModel):
    path: str
    content: str


@router.put("/file")
def save_file(payload: SavePayload, kb: KBConfig = Depends(resolve_kb)):
    full = _safe_path(payload.path, kb)
    if not full.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {payload.path}")
    full.write_text(payload.content, encoding="utf-8")
    log.info("Saved wiki file: %s  (%d chars)  kb=%s", payload.path, len(payload.content), kb.name)
    return {"saved": True, "path": payload.path}


@router.get("/gaps")
def get_gaps(kb: KBConfig = Depends(resolve_kb)):
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return {"gaps": []}

    gaps = []
    for f in sorted(gaps_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        # Parse frontmatter fields
        referenced_page = ""
        updated = ""
        for line in text.splitlines():
            if line.startswith("referenced_page:"):
                referenced_page = line.split(":", 1)[1].strip()
            elif line.startswith("updated:"):
                updated = line.split(":", 1)[1].strip()

        # Parse missing sections list
        missing: list[str] = []
        in_missing = False
        for line in text.splitlines():
            if line.strip() == "## Missing Sections":
                in_missing = True
                continue
            if in_missing:
                if line.startswith("##"):
                    break
                if line.startswith("- "):
                    item = line[2:].strip()
                    if not item.startswith("RESOLVED:"):
                        missing.append(item)

        if missing and not f.stem.startswith("patient-"):
            stem = f.stem
            gaps.append({
                "file": f"wiki/gaps/{f.name}",
                "title": stem.replace("-", " ").title(),
                "referenced_page": referenced_page,
                "missing_sections": missing,
                "updated": updated,
            })

    return {"gaps": gaps}


@router.post("/vectors/rebuild")
async def rebuild_vectors(background_tasks: BackgroundTasks, kb: KBConfig = Depends(resolve_kb)):
    """Embed (or re-embed) every wiki page for semantic search. Runs in background."""
    background_tasks.add_task(asyncio.to_thread, vs_mod.rebuild_all, kb.wiki_dir)
    total = sum(
        len(list((kb.wiki_dir / s).glob("*.md")))
        for s in ("entities", "concepts", "sources", "queries")
        if (kb.wiki_dir / s).exists()
    )
    return {"status": "started", "pages_to_embed": total}


@router.get("/vectors/status")
def vectors_status(kb: KBConfig = Depends(resolve_kb)):
    """Return how many pages are currently embedded."""
    return {"embedded": vs_mod.count(kb.wiki_dir)}


@router.delete("/contents")
def clear_wiki_contents(kb: KBConfig = Depends(resolve_kb)):
    deleted = 0
    for f in kb.wiki_dir.rglob("*"):
        if f.is_file():
            f.unlink()
            deleted += 1
    log.info("Cleared wiki contents: %d files deleted  kb=%s", deleted, kb.name)
    return {"deleted": deleted}
