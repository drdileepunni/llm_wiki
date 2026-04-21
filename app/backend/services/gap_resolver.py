"""
Service layer for resolving wiki knowledge gaps via PubMed search.
Used by /api/resolve/* endpoints.
"""

import json
import os
import re
import time
from typing import Optional

import httpx

from ..config import ANTHROPIC_API_KEY, BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID

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

def generate_search_queries(gap_title: str, gap_sections: list[str]) -> list[str]:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": (
            f'Fill a knowledge gap about: "{gap_title}"\n'
            f"Missing: {', '.join(gap_sections)}\n\n"
            "Generate 3 Google search queries to find free full-text PubMed review articles.\n"
            "- Use the simplest clinical/scientific name\n"
            "- Each query ends with 'pubmed free full text' or 'PMC review'\n"
            "- Under 10 words each\n"
            'Return ONLY a JSON array: ["query 1", "query 2", "query 3"]'
        )}],
    )
    text = resp.content[0].text.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if m:
        try:
            queries = json.loads(m.group())
            if isinstance(queries, list) and queries:
                return [str(q) for q in queries]
        except json.JSONDecodeError:
            pass
    return [f"{gap_title} review pubmed free full text"]


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


def _google_search(query: str) -> list[dict]:
    from urllib.parse import urlencode
    from playwright.sync_api import sync_playwright

    url = "https://www.google.com/search?" + urlencode({"q": query, "num": 20})
    with _BB() as bb:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bb.cdp_url)
            ctx  = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            try:
                btn = page.locator('button:has-text("Accept all")').first
                if btn.is_visible(timeout=3_000):
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            html = page.content()
            browser.close()
    return _extract_ncbi_hits(html)


# ── E-utilities ───────────────────────────────────────────────────────────────

def _ncbi_params(**kwargs) -> dict:
    p = dict(kwargs)
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def _pmc_to_pmid(pmc_id: str) -> Optional[str]:
    numeric = pmc_id.replace("PMC", "")
    params  = _ncbi_params(dbfrom="pmc", db="pubmed", id=numeric, retmode="json")
    try:
        resp = httpx.get(f"{EUTILS_BASE}/elink.fcgi", params=params, timeout=20)
        for ls in resp.json().get("linksets", []):
            for lsd in ls.get("linksetdbs", []):
                ids = lsd.get("links", [])
                if ids:
                    return str(ids[0])
    except Exception:
        pass
    return None


def _fetch_article_meta(pmid: str) -> dict:
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
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": (
            f'Gap: "{gap_title}"\nMissing: {", ".join(gap_sections)}\n\n'
            f"Article: {article['title']}\nAbstract: {article['abstract'][:1000]}\n\n"
            "Would this fill one or more missing sections?\n"
            'JSON only: {"relevant": true/false, "reason": "one sentence"}'
        )}],
    )
    text = resp.content[0].text.strip()
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return bool(data.get("relevant")), data.get("reason", "")
        except json.JSONDecodeError:
            pass
    return False, text


# ── PMC full-text fetch ───────────────────────────────────────────────────────

def _fetch_pmc_html(pmc_id: str) -> Optional[str]:
    url     = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers=headers)
        resp.raise_for_status()
        if len(resp.text) > 5000 and "<article" in resp.text:
            return resp.text
    except Exception:
        pass

    try:
        from playwright.sync_api import sync_playwright
        with _BB() as bb:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(bb.cdp_url)
                ctx     = browser.contexts[0]
                page    = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=45_000)
                html = page.content()
                browser.close()
        if len(html) > 5000:
            return html
    except Exception:
        pass
    return None


def _html_to_text(html: str) -> str:
    """Convert PMC HTML to plain text for the ingest pipeline. Figures → [Figure: caption]."""
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

def search_for_gap(gap_title: str, gap_sections: list[str], max_results: int = 5) -> list[dict]:
    """
    Search Google/PubMed for free full-text articles covering the gap.
    Returns list of article dicts enriched with relevance_reason.
    """
    queries  = generate_search_queries(gap_title, gap_sections)
    seen_ids: set[str] = set()
    all_hits: list[dict] = []

    for query in queries:
        if len(all_hits) >= max_results * 3:
            break
        for h in _google_search(query):
            if h["id"] not in seen_ids:
                seen_ids.add(h["id"])
                all_hits.append(h)

    articles: list[dict] = []

    for hit in all_hits:
        if len(articles) >= max_results:
            break

        if hit["type"] == "pmc":
            pmid = _pmc_to_pmid(hit["id"])
            if not pmid:
                continue
            known_pmc = hit["id"]
        else:
            pmid      = hit["id"]
            known_pmc = None

        try:
            article = _fetch_article_meta(pmid)
        except Exception:
            continue

        if known_pmc and not article["pmc_id"]:
            article["pmc_id"] = known_pmc

        if not article["pmc_id"]:
            continue

        relevant, reason = _is_relevant(article, gap_title, gap_sections)
        article["relevant"]          = relevant
        article["relevance_reason"]  = reason

        if relevant:
            articles.append(article)

    return articles


def fetch_article_content(pmc_id: str) -> Optional[str]:
    """Fetch PMC article as plain text for the ingest pipeline."""
    html = _fetch_pmc_html(pmc_id)
    if not html:
        return None
    return _html_to_text(html)
