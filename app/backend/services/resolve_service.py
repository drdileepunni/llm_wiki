"""
Synchronous resolve-all service — usable from both the router and the learn pipeline.
"""

import asyncio
import logging
import uuid

from ..config import KBConfig
from .fill_sections_pipeline import fill_sections
from .gap_resolver import fetch_article_content, search_for_gap

log = logging.getLogger("wiki.resolve_service")


def _list_pending_gaps(kb: KBConfig) -> list[dict]:
    """Return all non-patient gap files with unresolved sections."""
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return []
    results = []
    for f in sorted(gaps_dir.glob("*.md")):
        if f.stem.startswith("patient-"):
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        referenced_page = ""
        for line in text.splitlines():
            if line.startswith("referenced_page:"):
                referenced_page = line.split(":", 1)[1].strip()
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
        if missing:
            results.append({
                "file": f"wiki/gaps/{f.name}",
                "title": f.stem.replace("-", " ").title(),
                "referenced_page": referenced_page,
                "missing_sections": missing,
            })
    return results


def resolve_all_gaps(kb: KBConfig, max_results: int = 3) -> dict:
    """
    Synchronously resolve all pending non-patient gap files.
    Returns stats: gaps_attempted, articles_ingested, cost_usd.

    Safe to call from a thread (uses asyncio.run for the async search step).
    """
    gaps = _list_pending_gaps(kb)
    log.info("resolve_all_gaps: %d pending gaps", len(gaps))

    gaps_attempted = 0
    articles_ingested = 0
    total_cost = 0.0

    for gap in gaps:
        gaps_attempted += 1
        try:
            articles, search_cost = search_for_gap(gap["title"], gap["missing_sections"], max_results)
            total_cost += search_cost
        except Exception as exc:
            log.warning("search_for_gap failed for '%s': %s", gap["title"], exc)
            continue

        for article in articles:
            pmc_id   = article.get("pmc_id", "")
            title    = article.get("title", "")
            citation = article.get("citation", "")
            content  = article.get("content", "")

            try:
                if not content and pmc_id:
                    content = fetch_article_content(pmc_id) or ""
                if not content:
                    log.warning("No content for article '%s' (pmc_id=%s)", title, pmc_id)
                    continue

                if not citation:
                    citation = f"PubMed Central. {title}. PMC{pmc_id}."

                result = fill_sections(
                    content, title, citation,
                    gap["title"],
                    gap["missing_sections"],
                    gap["referenced_page"],
                    gap["file"],
                    kb,
                )
                total_cost += result.get("cost_usd", 0.0)
                articles_ingested += 1
                log.info("resolve_all_gaps: filled '%s' via '%s'", gap["title"], title)
            except Exception as exc:
                log.warning("fill_sections failed for '%s': %s", title, exc)

    return {
        "gaps_attempted":  gaps_attempted,
        "articles_ingested": articles_ingested,
        "cost_usd":        total_cost,
    }
