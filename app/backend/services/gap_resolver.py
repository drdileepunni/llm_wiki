"""
Service layer for resolving wiki knowledge gaps via PubMed search.
Used by /api/resolve/* endpoints.
"""

import json
import logging
import os
import re
import time
from typing import Optional

import threading

import httpx

from ..config import ANTHROPIC_API_KEY, GOOGLE_API_KEY, BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID

log = logging.getLogger("wiki.gap_resolver")

# Hard cap on total Browserbase sessions per search_for_gap call.
# Each session costs money; 1 Google search + up to 3 article fetches = 4 max per gap.
_BB_SEM = threading.Semaphore(3)   # max concurrent sessions (plan limit)
_BB_MAX_PER_SEARCH = 4             # hard total cap per search_for_gap invocation

NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")
EUTILS_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
RATE_DELAY   = 0.35 if not NCBI_API_KEY else 0.11


# ── Browserbase session ───────────────────────────────────────────────────────

class _BB:
    def __init__(self):
        from stagehand import Stagehand
        self._sh = Stagehand(
            browserbase_api_key=BROWSERBASE_API_KEY,
            browserbase_project_id=BROWSERBASE_PROJECT_ID,
            model_api_key=ANTHROPIC_API_KEY,
            server="remote",
        )
        self.cdp_url    = ""
        self._session_id = ""

    def __enter__(self):
        resp = self._sh.sessions.start(model_name="gpt-4o")
        self._session_id = resp.data.session_id
        self.cdp_url     = resp.data.cdp_url
        return self

    def __exit__(self, *_):
        try:
            self._sh.sessions.end(self._session_id)
        except Exception:
            pass
        self._sh.close()


# ── LLM query generation ──────────────────────────────────────────────────────

def generate_search_query(gap_title: str, gap_sections: list[str]) -> str:
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    log.info("[gap=%s] Generating search query via Gemini (missing: %s)",
             gap_title, ", ".join(gap_sections))
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f'Fill a knowledge gap about: "{gap_title}"\n'
            f"Missing: {', '.join(gap_sections)}\n\n"
            "Write ONE Google search query to find a free full-text PubMed review article.\n"
            "- Use the simplest clinical/scientific name\n"
            "- End with 'PMC full text'\n"
            "- Under 10 words\n"
            "Return ONLY the query string, no quotes, no explanation."
        ),
    )
    query = (resp.text or "").strip().strip('"')
    query = query or f"{gap_title} PMC full text"
    log.info("[gap=%s] Search query: '%s'", gap_title, query)
    return query


# ── Google search ─────────────────────────────────────────────────────────────

_RE_PUBMED = re.compile(
    r'href=["\']?(https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,9})/?)["\']?',
    re.IGNORECASE,
)
_RE_PMC = re.compile(
    r'href=["\']?(https?://(?:www\.)?(?:ncbi\.nlm\.nih\.gov/pmc/articles'
    r'|pmc\.ncbi\.nlm\.nih\.gov/articles)/(PMC\d+)/?)["\']?',
    re.IGNORECASE,
)
_RE_MEDSCAPE = re.compile(
    r'(?:href=["\']?/url\?[^"\']*?q=|href=["\']?)(https?://reference\.medscape\.com/(?:drug|article)/[^"\'&\s>]+)',
    re.IGNORECASE,
)


def _extract_ncbi_hits(html: str) -> list[dict]:
    seen: set[str] = set()
    hits: list[dict] = []
    for m in _RE_PUBMED.finditer(html):
        pmid = m.group(2)
        if pmid not in seen:
            seen.add(pmid)
            hits.append({"type": "pubmed", "id": pmid})
    for m in _RE_PMC.finditer(html):
        pmc_id = m.group(2).upper()
        if pmc_id not in seen:
            seen.add(pmc_id)
            hits.append({"type": "pmc", "id": pmc_id})
    return hits


def _extract_medscape_hits(html: str) -> list[dict]:
    seen: set[str] = set()
    hits: list[dict] = []
    for m in _RE_MEDSCAPE.finditer(html):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            hits.append({"type": "medscape", "url": url})
    return hits


def _google_search(query: str) -> list[dict]:
    from urllib.parse import urlencode
    from playwright.sync_api import sync_playwright

    url = "https://www.google.com/search?" + urlencode({"q": query, "num": 20})
    log.info("Google search (BB session): '%s'", query)
    with _BB_SEM, _BB() as bb:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bb.cdp_url)
            ctx  = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                btn = page.locator('button:has-text("Accept all")').first
                if btn.is_visible(timeout=3_000):
                    btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            html = page.content()
            browser.close()
    hits = _extract_ncbi_hits(html)
    log.info("Google search returned %d PubMed/PMC hits", len(hits))
    return hits


def _medscape_direct_search(gap_title: str) -> list[dict]:
    from urllib.parse import urlencode
    from playwright.sync_api import sync_playwright

    search_url = "https://reference.medscape.com/search?" + urlencode({"q": gap_title})
    log.info("[gap=%s] Medscape direct search (BB session): %s", gap_title, search_url)
    try:
        with _BB_SEM, _BB() as bb:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(bb.cdp_url)
                ctx  = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                html = page.content()
                browser.close()
        hits = _extract_medscape_hits(html)
        log.info("[gap=%s] Medscape search returned %d hits: %s",
                 gap_title, len(hits), [h["url"] for h in hits])
        return hits
    except Exception as exc:
        log.warning("[gap=%s] Medscape direct search failed: %s", gap_title, exc)
        return []


def _fetch_medscape_text(medscape_url: str) -> Optional[str]:
    from playwright.sync_api import sync_playwright
    log.info("Fetching Medscape page (BB session): %s", medscape_url)
    try:
        with _BB_SEM, _BB() as bb:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(bb.cdp_url)
                ctx  = browser.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(medscape_url, wait_until="domcontentloaded", timeout=30_000)
                html = page.content()
                browser.close()
        if len(html) > 2000:
            text = _html_to_text(html)
            log.info("Medscape page fetched: %d chars of text", len(text))
            return text
        log.warning("Medscape page too short (%d chars) — skipping", len(html))
    except Exception as exc:
        log.warning("Medscape page fetch failed for %s: %s", medscape_url, exc)
    return None


# ── E-utilities ───────────────────────────────────────────────────────────────

def _ncbi_params(**kwargs) -> dict:
    p = dict(kwargs)
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def _pmc_to_pmid(pmc_id: str) -> Optional[str]:
    log.debug("Resolving PMC→PMID for %s", pmc_id)
    numeric = pmc_id.replace("PMC", "")
    params  = _ncbi_params(dbfrom="pmc", db="pubmed", id=numeric, retmode="json")
    try:
        resp = httpx.get(f"{EUTILS_BASE}/elink.fcgi", params=params, timeout=20)
        for ls in resp.json().get("linksets", []):
            for lsd in ls.get("linksetdbs", []):
                ids = lsd.get("links", [])
                if ids:
                    log.debug("%s → PMID %s", pmc_id, ids[0])
                    return str(ids[0])
    except Exception as exc:
        log.warning("PMC→PMID lookup failed for %s: %s", pmc_id, exc)
    log.debug("No PMID found for %s", pmc_id)
    return None


def _fetch_article_meta(pmid: str) -> dict:
    log.debug("Fetching metadata for PMID %s", pmid)
    time.sleep(RATE_DELAY)
    params = _ncbi_params(db="pubmed", id=pmid, retmode="json")
    resp   = httpx.get(f"{EUTILS_BASE}/esummary.fcgi", params=params, timeout=30)
    resp.raise_for_status()
    meta = resp.json().get("result", {}).get(pmid, {})

    title    = meta.get("title", "")
    authors  = ", ".join(a["name"] for a in meta.get("authors", [])[:3])
    journal  = meta.get("source", "")
    pub_date = meta.get("pubdate", "")

    pmc_id = None
    for aid in meta.get("articleids", []):
        if aid.get("idtype") == "pmc":
            pmc_id = aid["value"]
            break

    time.sleep(RATE_DELAY)
    params   = _ncbi_params(db="pubmed", id=pmid, rettype="abstract", retmode="text")
    abstract = httpx.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=30).text.strip()

    log.debug("PMID %s → '%s' pmc_id=%s", pmid, title[:60], pmc_id)
    return {
        "pmid":     pmid,
        "pmc_id":   pmc_id,
        "title":    title,
        "authors":  authors,
        "journal":  journal,
        "pub_date": pub_date,
        "abstract": abstract,
        "citation": f"{authors}. {title}. {journal}. {pub_date}. PMID:{pmid}",
    }


def _is_relevant(article: dict, gap_title: str, gap_sections: list[str]) -> tuple[bool, str]:
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    log.debug("Checking relevance of '%s' for gap '%s'", article.get("title", "")[:60], gap_title)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f'Gap: "{gap_title}"\nMissing: {", ".join(gap_sections)}\n\n'
            f"Article: {article['title']}\nAbstract: {article['abstract'][:1000]}\n\n"
            "Would this fill one or more missing sections?\n"
            'JSON only: {"relevant": true/false, "reason": "one sentence"}'
        ),
    )
    text = (resp.text or "").strip()
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            relevant = bool(data.get("relevant"))
            reason   = data.get("reason", "")
            log.info("Relevance check → %s | %s | reason: %s",
                     "RELEVANT" if relevant else "not relevant",
                     article.get("title", "")[:60], reason)
            return relevant, reason
        except json.JSONDecodeError:
            pass
    log.warning("Relevance check unparseable response: %s", text[:100])
    return False, text


# ── PMC full-text fetch ───────────────────────────────────────────────────────

def _fetch_pmc_html(pmc_id: str) -> Optional[str]:
    url     = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    log.info("Fetching PMC full text for %s (direct HTTP)", pmc_id)
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers=headers)
        resp.raise_for_status()
        if len(resp.text) > 5000 and "<article" in resp.text:
            log.info("PMC %s fetched via HTTP (%d chars)", pmc_id, len(resp.text))
            return resp.text
        log.debug("PMC HTTP response too short or missing <article> tag — trying BB")
    except Exception as exc:
        log.debug("PMC HTTP fetch failed for %s: %s — trying BB", pmc_id, exc)

    log.info("Fetching PMC full text for %s (BB session fallback)", pmc_id)
    try:
        from playwright.sync_api import sync_playwright
        with _BB_SEM, _BB() as bb:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(bb.cdp_url)
                ctx     = browser.contexts[0]
                page    = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=45_000)
                html = page.content()
                browser.close()
        if len(html) > 5000:
            log.info("PMC %s fetched via BB (%d chars)", pmc_id, len(html))
            return html
        log.warning("PMC BB fetch for %s returned short page (%d chars)", pmc_id, len(html))
    except Exception as exc:
        log.warning("PMC BB fetch failed for %s: %s", pmc_id, exc)
    return None


def _html_to_text(html: str) -> str:
    """Convert PMC/Medscape HTML to plain text. Figures → [Figure: caption]."""
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    body = soup.find("article") or soup.find("div", {"class": "article-body"}) or soup.body or soup

    def walk(node) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""
        tag  = node.name
        kids = "".join(walk(c) for c in node.children)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return f"\n\n{kids.strip()}\n\n"
        if tag == "p":
            return f"\n{kids.strip()}\n"
        if tag in ("ul", "ol"):
            return f"\n{kids}\n"
        if tag == "li":
            return f"- {kids.strip()}\n"
        if tag == "table":
            rows = []
            for tr in node.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
                rows.append(" | ".join(cells))
            return "\n" + "\n".join(rows) + "\n"
        if tag in ("thead", "tbody", "tfoot", "tr", "th", "td"):
            return ""
        if tag == "figure":
            cap = node.find(["figcaption", "p"])
            caption = cap.get_text(" ", strip=True) if cap else ""
            return f"\n[Figure: {caption}]\n"
        if tag == "img":
            return ""
        if tag == "br":
            return "\n"
        return kids

    text = walk(body)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# ── Public API ────────────────────────────────────────────────────────────────

def _medscape_fallback(
    gap_title: str, gap_sections: list[str], query: str, max_results: int,
    bb_budget: int = 3,
) -> list[dict]:
    """Fetch Medscape reference pages as a fallback when PubMed yields nothing relevant."""
    log.info("[gap=%s] No relevant PubMed articles — trying Medscape fallback (BB budget: %d)",
             gap_title, bb_budget)

    if bb_budget <= 0:
        log.warning("[gap=%s] Medscape fallback skipped — BB session budget exhausted", gap_title)
        return []

    medscape_hits = _medscape_direct_search(gap_title)
    bb_budget -= 1

    if not medscape_hits:
        log.warning("[gap=%s] Medscape search returned 0 hits — gap unresolved", gap_title)
        return []

    articles: list[dict] = []
    for hit in medscape_hits:
        if len(articles) >= max_results:
            break
        if bb_budget <= 0:
            log.warning("[gap=%s] Medscape page fetch skipped — BB session budget exhausted", gap_title)
            break
        text = _fetch_medscape_text(hit["url"])
        bb_budget -= 1
        if not text:
            continue
        title = hit["url"].rstrip("/").split("/")[-1].replace("-", " ").title()
        article = {
            "pmid":       None,
            "pmc_id":     None,
            "title":      title,
            "authors":    "Medscape Reference",
            "journal":    "Medscape",
            "pub_date":   "",
            "abstract":   text[:500],
            "citation":   f"Medscape Reference. {title}. {hit['url']}",
            "content":    text,
            "source_url": hit["url"],
        }
        relevant, reason = _is_relevant(article, gap_title, gap_sections)
        article["relevant"]         = relevant
        article["relevance_reason"] = reason
        if relevant:
            articles.append(article)

    log.info("[gap=%s] Medscape fallback: %d relevant article(s) found", gap_title, len(articles))
    return articles


def search_for_gap(gap_title: str, gap_sections: list[str], max_results: int = 5) -> list[dict]:
    """
    Search Google/PubMed for free full-text articles covering the gap.
    Falls back to Medscape reference pages when PubMed yields no relevant results.
    Returns list of article dicts enriched with relevance_reason.
    """
    log.info("=== search_for_gap START  gap='%s'  sections=%s ===", gap_title, gap_sections)
    bb_sessions_used = 0

    query = generate_search_query(gap_title, gap_sections)
    seen_ids: set[str] = set()
    all_hits: list[dict] = []

    if bb_sessions_used < _BB_MAX_PER_SEARCH:
        for h in _google_search(query):
            if h["id"] not in seen_ids:
                seen_ids.add(h["id"])
                all_hits.append(h)
        bb_sessions_used += 1
    else:
        log.warning("[gap=%s] BB budget exhausted before Google search", gap_title)

    log.info("[gap=%s] Google search found %d unique PubMed/PMC hits (BB used: %d/%d)",
             gap_title, len(all_hits), bb_sessions_used, _BB_MAX_PER_SEARCH)

    # ── Normal PubMed/PMC path ────────────────────────────────────────────────
    articles: list[dict] = []

    for hit in all_hits:
        if len(articles) >= max_results:
            log.info("[gap=%s] Reached max_results=%d — stopping PubMed scan", gap_title, max_results)
            break

        if hit["type"] == "pmc":
            pmid = _pmc_to_pmid(hit["id"])
            if not pmid:
                log.debug("[gap=%s] Could not resolve PMID for %s — skipping", gap_title, hit["id"])
                continue
            known_pmc = hit["id"]
        else:
            pmid      = hit["id"]
            known_pmc = None

        try:
            article = _fetch_article_meta(pmid)
        except Exception as exc:
            log.warning("[gap=%s] Metadata fetch failed for PMID %s: %s", gap_title, pmid, exc)
            continue

        if known_pmc and not article["pmc_id"]:
            article["pmc_id"] = known_pmc

        if not article["pmc_id"]:
            log.debug("[gap=%s] PMID %s has no PMC full text — skipping", gap_title, pmid)
            continue

        relevant, reason = _is_relevant(article, gap_title, gap_sections)
        article["relevant"]          = relevant
        article["relevance_reason"]  = reason

        if relevant:
            articles.append(article)

    log.info("[gap=%s] PubMed path: %d relevant article(s) from %d hits",
             gap_title, len(articles), len(all_hits))

    # ── Medscape fallback when PubMed found nothing relevant ─────────────────
    if not articles and bb_sessions_used < _BB_MAX_PER_SEARCH:
        articles = _medscape_fallback(
            gap_title, gap_sections, query, max_results,
            bb_budget=_BB_MAX_PER_SEARCH - bb_sessions_used,
        )

    log.info("=== search_for_gap END  gap='%s'  articles=%d ===", gap_title, len(articles))
    return articles


def fetch_article_content(pmc_id: str) -> Optional[str]:
    """Fetch PMC article as plain text for the ingest pipeline."""
    log.info("fetch_article_content: fetching full text for %s", pmc_id)
    html = _fetch_pmc_html(pmc_id)
    if not html:
        log.warning("fetch_article_content: no HTML retrieved for %s", pmc_id)
        return None
    text = _html_to_text(html)
    log.info("fetch_article_content: %s → %d chars of plain text", pmc_id, len(text))
    return text
