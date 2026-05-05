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
        times_opened = 0
        for line in text.splitlines():
            if line.startswith("referenced_page:"):
                referenced_page = line.split(":", 1)[1].strip()
            elif line.startswith("times_opened:"):
                try:
                    times_opened = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
        missing: list[str] = []
        in_missing = False
        resolution_question = ""
        in_rq = False
        missing_values: list[str] = []
        in_mv = False
        for line in text.splitlines():
            if line.strip() == "## Missing Sections":
                in_missing = True; in_rq = False; in_mv = False
                continue
            if line.strip() == "## Resolution Question":
                in_rq = True; in_missing = False; in_mv = False
                continue
            if line.strip() == "## Specific Missing Values":
                in_mv = True; in_missing = False; in_rq = False
                continue
            if line.startswith("##"):
                in_missing = in_rq = in_mv = False
                continue
            if in_missing and line.startswith("- "):
                item = line[2:].strip()
                if not item.startswith("RESOLVED:"):
                    missing.append(item)
            elif in_rq and line.strip() and not resolution_question:
                resolution_question = line.strip()
            elif in_mv and line.startswith("- "):
                missing_values.append(line[2:].strip())
        if missing:
            results.append({
                "file": f"wiki/gaps/{f.name}",
                "title": f.stem.replace("-", " ").title(),
                "referenced_page": referenced_page,
                "missing_sections": missing,
                "times_opened": times_opened,
                "resolution_question": resolution_question,
                "missing_values": missing_values,
            })
    return results


def resolve_all_gaps(kb: KBConfig, max_results: int = 3, progress_callback=None) -> dict:
    """
    Synchronously resolve all pending non-patient gap files.
    Returns stats: gaps_attempted, articles_ingested, cost_usd.

    progress_callback(idx, total, gap_title, articles_this_gap, cost_this_gap) is
    called after each gap is processed — use it to emit live progress updates.

    Safe to call from a thread (uses asyncio.run for the async search step).
    """
    gaps = _list_pending_gaps(kb)
    log.info("resolve_all_gaps: %d pending gaps", len(gaps))
    total_gaps = len(gaps)

    gaps_attempted = 0
    articles_ingested = 0
    total_cost = 0.0

    for idx, gap in enumerate(gaps, 1):
        gaps_attempted += 1
        gap_articles = 0
        gap_cost = 0.0

        try:
            articles, search_cost = search_for_gap(
                gap["title"], gap["missing_sections"], max_results,
                times_opened=gap["times_opened"],
                resolution_question=gap.get("resolution_question", ""),
                missing_values=gap.get("missing_values") or None,
            )
            gap_cost += search_cost
            total_cost += search_cost
        except Exception as exc:
            log.warning("search_for_gap failed for '%s': %s", gap["title"], exc)
            if progress_callback:
                progress_callback(idx, total_gaps, gap["title"], 0, 0.0)
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
                c = result.get("cost_usd", 0.0)
                gap_cost   += c
                total_cost += c
                gap_articles += 1
                articles_ingested += 1
                log.info("resolve_all_gaps: filled '%s' via '%s'", gap["title"], title)
            except Exception as exc:
                log.warning("fill_sections failed for '%s': %s", title, exc)

        if progress_callback:
            progress_callback(idx, total_gaps, gap["title"], gap_articles, gap_cost)

    return {
        "gaps_attempted":  gaps_attempted,
        "articles_ingested": articles_ingested,
        "cost_usd":        total_cost,
    }
