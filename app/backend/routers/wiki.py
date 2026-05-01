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


@router.delete("/file")
def delete_file(path: str = Query(...), kb: KBConfig = Depends(resolve_kb)):
    full = _safe_path(path, kb)
    if not full.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    full.unlink()
    log.info("Deleted wiki file: %s  kb=%s", path, kb.name)
    return {"deleted": True, "path": path}


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
        placement = "confirmed"
        times_opened = 1
        status = ""
        for line in text.splitlines():
            if line.startswith("referenced_page:"):
                referenced_page = line.split(":", 1)[1].strip()
            elif line.startswith("updated:"):
                updated = line.split(":", 1)[1].strip()
            elif line.startswith("placement:"):
                placement = line.split(":", 1)[1].strip()
            elif line.startswith("times_opened:"):
                try:
                    times_opened = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("status:"):
                status = line.split(":", 1)[1].strip()

        # Parse missing sections list
        missing: list[str] = []
        in_missing = False
        # Parse specific missing values list
        missing_values: list[str] = []
        in_missing_values = False
        for line in text.splitlines():
            if line.strip() == "## Missing Sections":
                in_missing = True
                in_missing_values = False
                continue
            if line.strip() == "## Specific Missing Values":
                in_missing_values = True
                in_missing = False
                continue
            if in_missing:
                if line.startswith("##"):
                    in_missing = False
                elif line.startswith("- "):
                    item = line[2:].strip()
                    if not item.startswith("RESOLVED:"):
                        missing.append(item)
            elif in_missing_values:
                if line.startswith("##"):
                    in_missing_values = False
                elif line.startswith("- "):
                    missing_values.append(line[2:].strip())

        if missing and not f.stem.startswith("patient-"):
            stem = f.stem
            # Enrich with page-level metrics from page_metrics.json
            page_metrics: dict = {}
            try:
                from ..services.page_metrics import get_page as _get_page_metrics
                page_metrics = _get_page_metrics(referenced_page, kb.wiki_dir)
            except Exception:
                pass
            gaps.append({
                "file": f"wiki/gaps/{f.name}",
                "title": stem.replace("-", " ").title(),
                "referenced_page": referenced_page,
                "missing_sections": missing,
                "missing_values": missing_values,
                "placement": placement,
                "updated": updated,
                "times_opened": times_opened,
                "status": status,                          # "persistent" or ""
                "cds_query_count": page_metrics.get("cds_query_count", 0),
                "gap_opens": page_metrics.get("gap_opens", times_opened),
            })

    return {"gaps": gaps}


@router.get("/gap-intelligence")
def get_gap_intelligence(kb: KBConfig = Depends(resolve_kb)):
    """
    Returns three datasets for the Gap Intelligence view:
    - open_gaps: current unresolved gap files
    - persistent_gaps: entries in gap_history.json where any section was filed >= 2 times
    - scope_contamination: wiki pages flagged with scope_contamination in their frontmatter
    """
    from ..services.ingest_pipeline import _load_gap_history
    import json as _json

    gaps_dir = kb.wiki_dir / "gaps"
    history = _load_gap_history(gaps_dir) if gaps_dir.exists() else {}

    # ── Open gaps (current gap files) ────────────────────────────────────────
    open_gaps = []
    open_stems: set[str] = set()
    if gaps_dir.exists():
        for f in sorted(gaps_dir.glob("*.md")):
            if f.stem.startswith("patient-"):
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            referenced_page, placement, times_opened = "", "confirmed", 1
            section_times: dict = {}
            for line in text.splitlines():
                if line.startswith("referenced_page:"):
                    referenced_page = line.split(":", 1)[1].strip()
                elif line.startswith("placement:"):
                    placement = line.split(":", 1)[1].strip()
                elif line.startswith("times_opened:"):
                    try: times_opened = int(line.split(":", 1)[1].strip())
                    except ValueError: pass
                elif line.startswith("section_times_opened:"):
                    try: section_times = _json.loads(line.split(":", 1)[1].strip())
                    except Exception: pass
            # Merge with history
            for s, cnt in history.get(f.stem, {}).items():
                section_times[s] = max(section_times.get(s, 0), cnt)

            missing, missing_values, resolution_question = [], [], ""
            in_m = in_mv = in_q = False
            for line in text.splitlines():
                if line.strip() == "## Missing Sections": in_m = True; in_mv = in_q = False; continue
                if line.strip() == "## Specific Missing Values": in_mv = True; in_m = in_q = False; continue
                if line.strip() == "## Resolution Question": in_q = True; in_m = in_mv = False; continue
                if line.startswith("##"): in_m = in_mv = in_q = False; continue
                if in_m and line.startswith("- ") and not line[2:].strip().startswith("RESOLVED:"):
                    missing.append(line[2:].strip())
                if in_mv and line.startswith("- "): missing_values.append(line[2:].strip())
                if in_q and line.strip() and not resolution_question: resolution_question = line.strip()

            if missing:
                open_stems.add(f.stem)
                open_gaps.append({
                    "stem": f.stem,
                    "title": f.stem.replace("-", " ").title(),
                    "referenced_page": referenced_page,
                    "missing_sections": missing,
                    "missing_values": missing_values,
                    "resolution_question": resolution_question,
                    "placement": placement,
                    "times_opened": times_opened,
                    "section_times_opened": section_times,
                    "max_section_opens": max(section_times.values()) if section_times else times_opened,
                })

    # ── Persistent gaps (from history, any section >= 2, whether open or closed) ──
    persistent_gaps = []
    for stem, sections in sorted(history.items()):
        max_opens = max(sections.values()) if sections else 0
        if max_opens >= 2:
            is_open = stem in open_stems
            persistent_gaps.append({
                "stem": stem,
                "title": stem.replace("-", " ").title(),
                "section_times_opened": sections,
                "max_section_opens": max_opens,
                "is_open": is_open,
            })
    persistent_gaps.sort(key=lambda x: x["max_section_opens"], reverse=True)

    # ── Scope contamination ──────────────────────────────────────────────────
    scope_contamination = []
    for prefix in ("entities", "concepts"):
        d = kb.wiki_dir / prefix
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            if "scope_contamination:" not in text:
                continue
            violations = []
            in_sc = False
            for line in text.splitlines():
                if line.strip().startswith("scope_contamination:"):
                    in_sc = True; continue
                if in_sc:
                    if line.startswith("  - section:"):
                        violations.append({"section": line.split(":", 1)[1].strip()})
                    elif line.startswith("    belongs_on:") and violations:
                        violations[-1]["belongs_on"] = line.split(":", 1)[1].strip()
                    elif line.startswith("    excerpt:") and violations:
                        violations[-1]["excerpt"] = line.split(":", 1)[1].strip().strip('"')
                    elif not line.startswith(" ") and not line.startswith("\t"):
                        break
            if violations:
                scope_contamination.append({
                    "path": f"{prefix}/{f.name}",
                    "title": f.stem.replace("-", " ").title(),
                    "violations": violations,
                })

    return {
        "open_gaps": open_gaps,
        "persistent_gaps": persistent_gaps,
        "scope_contamination": scope_contamination,
    }


class CreateGapRequest(BaseModel):
    title: str
    missing_sections: list[str]


@router.post("/gaps")
def create_gap(req: CreateGapRequest, kb: KBConfig = Depends(resolve_kb)):
    if not req.title.strip() or not req.missing_sections:
        raise HTTPException(status_code=400, detail="title and at least one missing_section required")

    today = __import__("datetime").date.today().isoformat()
    slug  = re.sub(r"[^a-z0-9]+", "-", req.title.lower()).strip("-")
    ref   = f"entities/{slug}.md"
    title_fm = req.title.strip().title()

    gaps_dir = kb.wiki_dir / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)
    gap_path = gaps_dir / f"{slug}.md"

    sections_block = "\n".join(f"- {s}" for s in req.missing_sections if s.strip())
    content = (
        f"---\n"
        f'title: "Knowledge Gap — {title_fm}"\n'
        f"type: gap\n"
        f"referenced_page: {ref}\n"
        f"tags: [gap]\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        f"---\n\n"
        f"## Missing Sections\n\n"
        f"{sections_block}\n"
    )
    gap_path.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "file": f"wiki/gaps/{slug}.md",
        "title": title_fm,
        "referenced_page": ref,
        "missing_sections": [s.strip() for s in req.missing_sections if s.strip()],
    }


class UpdateGapRequest(BaseModel):
    title: str | None = None
    missing_sections: list[str] | None = None


@router.patch("/gaps/{stem}")
def update_gap(stem: str, req: UpdateGapRequest, kb: KBConfig = Depends(resolve_kb)):
    gap_path = kb.wiki_dir / "gaps" / f"{stem}.md"
    if not gap_path.exists():
        raise HTTPException(status_code=404, detail=f"Gap {stem!r} not found")

    text = gap_path.read_text(encoding="utf-8")
    today = __import__("datetime").date.today().isoformat()

    # Update referenced_page slug and title in frontmatter when title changes
    if req.title is not None:
        new_slug = re.sub(r"[^a-z0-9]+", "-", req.title.lower()).strip("-")
        new_ref  = f"entities/{new_slug}.md"
        title_fm = req.title.title()

        # Replace frontmatter fields
        text = re.sub(r'^title:.*$', f'title: "Knowledge Gap — {title_fm}"', text, flags=re.MULTILINE)
        text = re.sub(r'^referenced_page:.*$', f'referenced_page: {new_ref}', text, flags=re.MULTILINE)

    # Update updated date
    text = re.sub(r'^updated:.*$', f'updated: {today}', text, flags=re.MULTILINE)

    # Replace missing sections block
    if req.missing_sections is not None:
        sections_block = "## Missing Sections\n\n" + "\n".join(f"- {s}" for s in req.missing_sections) + "\n"
        if "## Missing Sections" in text:
            text = re.sub(r"## Missing Sections\n.*", sections_block, text, flags=re.DOTALL)
        else:
            text = text.rstrip() + "\n\n" + sections_block

    # If title changed, rename the file
    if req.title is not None:
        new_slug = re.sub(r"[^a-z0-9]+", "-", req.title.lower()).strip("-")
        new_path = kb.wiki_dir / "gaps" / f"{new_slug}.md"
        gap_path.write_text(text, encoding="utf-8")
        if new_path != gap_path:
            gap_path.rename(new_path)
            gap_path = new_path
    else:
        gap_path.write_text(text, encoding="utf-8")

    return {"ok": True, "file": f"wiki/gaps/{gap_path.name}"}


@router.get("/activity")
def get_activity(limit: int = Query(500), kb: KBConfig = Depends(resolve_kb)):
    """Return the structured activity log for this KB, newest first."""
    from ..services.activity_log import read_events
    return {"events": read_events(kb.wiki_dir, limit=limit)}


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


@router.get("/quality")
def get_quality(
    rescore: bool = Query(False, description="Re-score all pages even if scores already exist"),
    llm_enhance: bool = Query(False, description="Use LLM to refine borderline scores (costs tokens)"),
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Return section-level quality scores for all entity/concept pages.

    Default: reads existing section_quality from frontmatter (or scores if absent).
    rescore=true: forces re-scoring of every page regardless of cached scores.
    llm_enhance=true: uses the LLM to refine sections scored 1 or 2 by the rule engine.

    Results are sorted worst-first (lowest min_score, then most flags).
    """
    from ..services.quality_scorer import (
        score_page,
        update_quality_frontmatter,
        parse_section_quality,
        llm_enhance_scores,
        _parse_sections,
        _parse_subtype,
    )
    from collections import Counter

    results = []

    for section_dir in ("entities", "concepts"):
        d = kb.wiki_dir / section_dir
        if not d.exists():
            continue

        for md_file in sorted(d.glob("*.md")):
            content = md_file.read_text(encoding="utf-8", errors="replace")

            needs_score = rescore or "section_quality:" not in content

            if needs_score:
                scores = score_page(content)
                if llm_enhance and scores:
                    sections = _parse_sections(content)
                    subtype  = _parse_subtype(content)
                    scores   = llm_enhance_scores(scores, sections, subtype)
                if scores:
                    updated = update_quality_frontmatter(content, scores)
                    md_file.write_text(updated, encoding="utf-8")
            else:
                scores = parse_section_quality(content)

            if not scores:
                continue

            min_score    = min(d["score"] for d in scores.values())
            worst        = min(scores, key=lambda s: scores[s]["score"])
            total_flags  = sum(len(d.get("flags", [])) for d in scores.values())

            results.append({
                "path":            f"wiki/{section_dir}/{md_file.name}",
                "title":           md_file.stem.replace("-", " ").title(),
                "section_quality": scores,
                "min_score":       min_score,
                "worst_section":   worst,
                "total_flags":     total_flags,
            })

    results.sort(key=lambda r: (r["min_score"], -r["total_flags"]))

    score_dist: Counter = Counter()
    for r in results:
        for data in r["section_quality"].values():
            score_dist[data["score"]] += 1

    return {
        "total_pages":       len(results),
        "pages":             results,
        "score_distribution": {str(k): v for k, v in sorted(score_dist.items())},
    }


@router.post("/defrag")
async def defrag_wiki(
    background_tasks: BackgroundTasks,
    path: str = Query(None, description="Defrag a single page (wiki-relative). Omit to defrag all."),
    dry_run: bool = Query(False),
    kb: KBConfig = Depends(resolve_kb),
):
    """Resolve scope_contamination flags by moving misplaced content to the correct pages."""
    from ..services.defrag_pipeline import defrag_page as _defrag_page, defrag_all as _defrag_all

    if dry_run:
        return await asyncio.to_thread(_defrag_all, kb, True)

    if path:
        background_tasks.add_task(asyncio.to_thread, _defrag_page, path, kb)
        return {"status": "started", "mode": "single", "path": path}
    else:
        background_tasks.add_task(asyncio.to_thread, _defrag_all, kb)
        return {"status": "started", "mode": "all"}


@router.post("/migrate/scope")
async def migrate_scope(background_tasks: BackgroundTasks, kb: KBConfig = Depends(resolve_kb)):
    """Add scope: field to all entity/concept pages that don't have one. Runs in background."""
    def _run(kb: KBConfig):
        from ..services.quality_scorer import (
            parse_scope, update_scope_frontmatter, _default_scope, _parse_subtype
        )
        from ..services.activity_log import append_event
        updated = 0
        updated_files = []
        for section in ("entities", "concepts"):
            d = kb.wiki_dir / section
            if not d.exists():
                continue
            for md_file in sorted(d.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                    if parse_scope(content):
                        continue
                    scope = _default_scope(md_file.stem.replace("-", " ").title(), _parse_subtype(content))
                    md_file.write_text(update_scope_frontmatter(content, scope), encoding="utf-8")
                    updated += 1
                    updated_files.append(f"wiki/{section}/{md_file.name}")
                except Exception as exc:
                    log.warning("migrate_scope: %s: %s", md_file.name, exc)
        log.info("migrate_scope: added scope to %d page(s)", updated)
        append_event(kb.wiki_dir, {
            "operation": "migrate",
            "kb": kb.name,
            "source": f"Scope Migration — {updated} page(s) updated",
            "files_written": updated_files,
            "gaps_opened": [], "gaps_closed": [], "sections_filled": [],
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "detail": f"Added scope: field to {updated} entity/concept pages",
        })

    background_tasks.add_task(asyncio.to_thread, _run, kb)
    total = sum(len(list((kb.wiki_dir / s).glob("*.md"))) for s in ("entities", "concepts") if (kb.wiki_dir / s).exists())
    return {"status": "started", "pages_to_scan": total}


@router.post("/migrate/reconcile-gaps")
async def migrate_reconcile_gaps(background_tasks: BackgroundTasks, kb: KBConfig = Depends(resolve_kb)):
    """Scan all gap files and close any sections already written in the referenced page."""
    def _run(kb: KBConfig):
        from ..services.ingest_pipeline import reconcile_gaps
        from ..services.activity_log import append_event
        result = reconcile_gaps(kb)
        append_event(kb.wiki_dir, {
            "operation": "migrate",
            "kb": kb.name,
            "source": f"Gap Reconciliation — {len(result['fully_closed'])} closed, {len(result['partially_resolved'])} trimmed",
            "files_written": [],
            "gaps_opened": [],
            "gaps_closed": result["fully_closed"],
            "sections_filled": [],
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "detail": (
                f"Checked {result['checked']} gap files. "
                f"Fully closed: {len(result['fully_closed'])}. "
                f"Partially resolved: {len(result['partially_resolved'])}. "
                f"Errors: {len(result['errors'])}."
            ),
        })

    background_tasks.add_task(asyncio.to_thread, _run, kb)
    gaps_count = len(list((kb.wiki_dir / "gaps").glob("*.md"))) if (kb.wiki_dir / "gaps").exists() else 0
    return {"status": "started", "gaps_to_check": gaps_count}


@router.post("/migrate/scan-contamination")
async def migrate_scan_contamination(background_tasks: BackgroundTasks, kb: KBConfig = Depends(resolve_kb)):
    """LLM-scan all entity/concept pages for scope contamination and stamp frontmatter flags."""
    def _run(kb: KBConfig):
        from ..services.quality_scorer import (
            check_scope, parse_scope, _default_scope, _parse_subtype,
            update_contamination_frontmatter, parse_scope_contamination,
        )
        pages = [
            (s, f)
            for s in ("entities", "concepts")
            for f in sorted((kb.wiki_dir / s).glob("*.md"))
            if (kb.wiki_dir / s).exists()
        ]
        existing_titles = [f.stem.replace("-", " ").title() for _, f in pages]
        from ..services.quality_scorer import clear_contamination_frontmatter
        flagged = 0
        cleared = 0
        for section, md_file in pages:
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
                title  = md_file.stem.replace("-", " ").title()
                scope  = parse_scope(content) or _default_scope(title, _parse_subtype(content))
                result = check_scope(title, content, scope, existing_titles)
                if not result["clean"]:
                    md_file.write_text(update_contamination_frontmatter(content, result["violations"]), encoding="utf-8")
                    flagged += 1
                    log.info("scan: flagged %s (%d violation(s))", md_file.name, len(result["violations"]))
                elif parse_scope_contamination(content):
                    # Previously flagged but now clean — clear the stale flag
                    md_file.write_text(clear_contamination_frontmatter(content), encoding="utf-8")
                    cleared += 1
                    log.info("scan: cleared stale flag on %s", md_file.name)
            except Exception as exc:
                log.warning("scan: %s: %s", md_file.name, exc)
        log.info("migrate_scan_contamination: %d/%d flagged, %d cleared", flagged, len(pages), cleared)
        from ..services.activity_log import append_event
        append_event(kb.wiki_dir, {
            "operation": "migrate",
            "kb": kb.name,
            "source": f"Scope Contamination Scan — {flagged}/{len(pages)} flagged, {cleared} stale flags cleared",
            "files_written": [],
            "gaps_opened": [], "gaps_closed": [], "sections_filled": [],
            "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "detail": f"Scanned {len(pages)} pages, found contamination on {flagged}, cleared {cleared} stale flags",
        })

    background_tasks.add_task(asyncio.to_thread, _run, kb)
    total = sum(len(list((kb.wiki_dir / s).glob("*.md"))) for s in ("entities", "concepts") if (kb.wiki_dir / s).exists())
    return {"status": "started", "pages_to_scan": total}


class FalsePositiveRequest(BaseModel):
    path: str          # e.g. "wiki/entities/chlordiazepoxide.md"
    section: str       # section heading of the violation
    belongs_on: str    # the erroneous target page title


@router.post("/false-positive")
def mark_false_positive(req: FalsePositiveRequest, kb: KBConfig = Depends(resolve_kb)):
    """
    Whitelist a scope_contamination violation as a false positive.
    Adds it to scope_ignore: in frontmatter so it is never re-flagged on future scans.
    """
    from ..services.quality_scorer import add_scope_ignore

    rel = req.path.lstrip("/")
    md_file = kb.wiki_dir.parent / rel
    if not md_file.exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"File not found: {rel}")

    content = md_file.read_text(encoding="utf-8", errors="replace")
    updated = add_scope_ignore(content, req.section, req.belongs_on)
    md_file.write_text(updated, encoding="utf-8")
    log.info("false-positive: whitelisted '%s' → '%s' on %s", req.section, req.belongs_on, md_file.name)
    return {"status": "ok", "path": rel, "section": req.section, "belongs_on": req.belongs_on}


@router.get("/contamination")
def get_contamination(kb: KBConfig = Depends(resolve_kb)):
    """Return all pages currently flagged with scope_contamination."""
    from ..services.quality_scorer import parse_scope_contamination
    flagged = []
    for section in ("entities", "concepts"):
        d = kb.wiki_dir / section
        if not d.exists():
            continue
        for md_file in sorted(d.glob("*.md")):
            try:
                content    = md_file.read_text(encoding="utf-8", errors="replace")
                violations = parse_scope_contamination(content)
                if violations:
                    flagged.append({
                        "path":       f"wiki/{section}/{md_file.name}",
                        "title":      md_file.stem.replace("-", " ").title(),
                        "violations": violations,
                    })
            except Exception:
                pass
    return {"total": len(flagged), "pages": flagged}


@router.delete("/contents")
def clear_wiki_contents(kb: KBConfig = Depends(resolve_kb)):
    deleted = 0
    for f in kb.wiki_dir.rglob("*"):
        if f.is_file():
            f.unlink()
            deleted += 1
    log.info("Cleared wiki contents: %d files deleted  kb=%s", deleted, kb.name)
    return {"deleted": deleted}
