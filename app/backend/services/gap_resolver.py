"""
Service layer for resolving wiki knowledge gaps via PubMed search.
Used by /api/resolve/* endpoints.
"""

import json
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Optional

import threading

import httpx

from ..config import ANTHROPIC_API_KEY, GOOGLE_API_KEY, BROWSERBASE_API_KEY, BROWSERBASE_PROJECT_ID, KG_FALLBACK_MODEL
from .token_tracker import log_call, calculate_cost

_GEMINI_SEARCH_MODEL = "gemini-2.5-flash"

log = logging.getLogger("wiki.gap_resolver")

# Limits concurrent BB sessions when NOT using BBPool (single-gap resolve / search).
_BB_SEM = threading.Semaphore(3)

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


class BBPool:
    """
    Pool of long-lived Browserbase sessions for batch gap resolution.

    Opens `size` BB sessions at start and keeps them alive for the whole batch.
    Each call to `acquire()` borrows a CDP URL, creates a fresh Playwright
    connection to it (thread-safe), and returns it when done.  This way 25 gaps
    share 3 BB sessions instead of opening 25 × 60 s-minimum sessions.
    """

    def __init__(self, size: int = 3):
        self._size      = size
        self._sessions: list[_BB] = []
        self._cdp_urls: list[str] = []
        self._available: list[str] = []
        self._lock = threading.Lock()
        self._sem  = threading.Semaphore(0)  # released once per started session

    def start(self):
        for i in range(self._size):
            bb = _BB()
            bb.__enter__()
            self._sessions.append(bb)
            self._cdp_urls.append(bb.cdp_url)
            self._available.append(bb.cdp_url)
            self._sem.release()
            log.info("BBPool: session %d/%d started (%s)", i + 1, self._size, bb._session_id)

    def stop(self):
        for bb in self._sessions:
            try:
                bb.__exit__()
            except Exception:
                pass
        log.info("BBPool: all %d sessions closed", self._size)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    @contextmanager
    def acquire(self):
        """Borrow a CDP URL from the pool. Creates a fresh Playwright connection per call."""
        self._sem.acquire()
        with self._lock:
            cdp_url = self._available.pop()
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_url)
                try:
                    yield browser
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
        finally:
            with self._lock:
                self._available.append(cdp_url)
            self._sem.release()


# ── LLM query generation ──────────────────────────────────────────────────────

def generate_search_query(gap_title: str, gap_sections: list[str], resolution_question: str = "") -> tuple[str, float]:
    """Returns (query_string, cost_usd)."""
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    log.info("[gap=%s] Generating search query via Gemini (missing: %s  question=%s)",
             gap_title, ", ".join(gap_sections), resolution_question[:80] if resolution_question else "none")
    if resolution_question:
        context = (
            f'Specific clinical question to answer: "{resolution_question}"\n'
            f'(Topic: "{gap_title}", Missing: {", ".join(gap_sections)})\n\n'
        )
    else:
        context = (
            f'Fill a knowledge gap about: "{gap_title}"\n'
            f"Missing: {', '.join(gap_sections)}\n\n"
        )
    resp = client.models.generate_content(
        model=_GEMINI_SEARCH_MODEL,
        contents=(
            context
            + "Write ONE Google search query to find a free full-text PubMed review article "
            "that directly answers the question above.\n"
            "- Use the simplest clinical/scientific name\n"
            "- End with 'PMC full text'\n"
            "- Under 10 words\n"
            "Return ONLY the query string, no quotes, no explanation."
        ),
    )
    query = (resp.text or "").strip().strip('"')
    query = query or f"{gap_title} PMC full text"
    log.info("[gap=%s] Search query: '%s'", gap_title, query)
    tokens_in  = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
    tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    cost = log_call(
        operation="gap_search_query",
        source_name=gap_title[:80],
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        model=_GEMINI_SEARCH_MODEL,
    )
    return query, cost


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


def _google_search_in_session(browser, query: str) -> list[dict]:
    """Google search reusing an already-open Playwright browser."""
    from urllib.parse import urlencode
    url = "https://www.google.com/search?" + urlencode({"q": query, "num": 20})
    log.info("Google search (shared BB session): '%s'", query)
    ctx  = browser.contexts[0]
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            btn = page.locator('button:has-text("Accept all")').first
            if btn.is_visible(timeout=3_000):
                btn.click()
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        html = page.content()
    finally:
        page.close()
    hits = _extract_ncbi_hits(html)
    log.info("Google search returned %d PubMed/PMC hits", len(hits))
    return hits


def _is_ollama_reachable() -> bool:
    """Quick TCP check — returns False immediately if MedGemma/Ollama is not up."""
    from ..config import MEDGEMMA_URL
    import urllib.request
    try:
        urllib.request.urlopen(MEDGEMMA_URL.rstrip("/") + "/api/tags", timeout=2)
        return True
    except Exception:
        return False


def _llm_fallback(
    gap_title: str,
    gap_sections: list[str],
    resolution_question: str = "",
    missing_values: list[str] | None = None,
) -> tuple[list[dict], float]:
    """Generate gap content via LLM when PubMed yields 0 articles. Returns (articles, cost_usd)."""
    from .llm_client import get_llm_client
    log.info(
        "[gap=%s] LLM fallback using model=%s  sections=%s  question=%s  missing_values=%s",
        gap_title, KG_FALLBACK_MODEL, gap_sections,
        resolution_question[:80] if resolution_question else "none",
        missing_values or [],
    )
    llm = get_llm_client(model=KG_FALLBACK_MODEL)
    if resolution_question:
        mv_block = (
            "\n\nYou MUST include specific values for these data points:\n"
            + "".join(f"- {v}\n" for v in missing_values)
            if missing_values else ""
        )
        prompt = (
            "You are a medical reference author writing a GENERALIZABLE wiki page — "
            "not a case note or patient-specific answer.\n"
            "The question below was raised by a specific clinical case, but your answer must "
            "describe the standard evidence-based approach applicable to ANY patient with this "
            "condition. Do NOT reference the specific patient's age, sex, or presentation. "
            "Write for a broad clinical audience.\n\n"
            f"Clinical question (use for topic scope only):\n**{resolution_question}**"
            f"{mv_block}\n\n"
            f"Structure your answer using these headings (## heading):\n"
            + "".join(f"- {s}\n" for s in gap_sections)
            + "\nRequirements:\n"
            "- Write generalizable clinical knowledge — doses, ranges, protocols that apply broadly.\n"
            "- Include specific numeric values, doses, and titration ranges where known.\n"
            "- 2-4 paragraphs per section. Plain prose, no bullet spam.\n"
            "- Cite evidence or guidelines where known (e.g. NICE, SSC, ARDSNet).\n"
            "- If the answer varies by patient factors, state the decision logic (e.g. 'in renal impairment, reduce dose to...') without referring to the specific case patient."
        )
    else:
        prompt = (
            f"You are a medical reference author. Write a comprehensive, evidence-based summary for the drug/topic: **{gap_title}**\n\n"
            f"Cover ONLY these sections (use ## headings):\n"
            + "".join(f"- {s}\n" for s in gap_sections)
            + "\nFor each section write 2-5 paragraphs with specific clinical details, doses, and references where known. "
            "Use plain prose, no bullet spam. Be precise and clinically useful."
        )
    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system="You are a medical reference author. Write evidence-based clinical content.",
        max_tokens=4000,
        force_tool=False,
        thinking_budget=0,
    )
    text = next((b.text for b in resp.content if hasattr(b, "text") and b.text), "").strip()
    tokens_in  = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    cost = log_call(
        operation="gap_llm_fallback",
        source_name=gap_title[:80],
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        model=KG_FALLBACK_MODEL,
    )
    if len(text) < 200:
        log.warning("[gap=%s] LLM fallback response too short (%d chars)", gap_title, len(text))
        return [], cost
    log.info("[gap=%s] LLM fallback generated %d chars", gap_title, len(text))
    article = {
        "pmid":               None,
        "pmc_id":             None,
        "title":              f"LLM-generated summary: {gap_title}",
        "authors":            KG_FALLBACK_MODEL,
        "journal":            "LLM fallback",
        "pub_date":           "",
        "abstract":           text[:500],
        "citation":           f"Generated by {KG_FALLBACK_MODEL}. {gap_title}.",
        "content":            text,
        "source_url":         None,
        "relevant":           True,
        "relevance_reason":   f"LLM-generated content covering {', '.join(gap_sections)}",
        "skip_quality_gate":  True,
    }
    return [article], cost


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


def _is_relevant(
    article: dict,
    gap_title: str,
    gap_sections: list[str],
    resolution_question: str = "",
    missing_values: list[str] | None = None,
) -> tuple[bool, str, float]:
    """Returns (is_relevant, reason, cost_usd)."""
    from google import genai
    client = genai.Client(api_key=GOOGLE_API_KEY)
    log.info(
        "Relevance check: article=%r  gap=%r  question=%s  missing_values=%s",
        article.get("title", "")[:80], gap_title,
        resolution_question[:80] if resolution_question else "none",
        missing_values or [],
    )

    # Build a rich gap description so the relevance check uses the actual data
    # points needed, not just the (often generic) section heading.
    gap_lines = [f'Topic: "{gap_title}"']
    if resolution_question:
        gap_lines.append(f'Specific question: "{resolution_question}"')
    if missing_values:
        gap_lines.append("Specific missing values:\n" + "\n".join(f"  - {v}" for v in missing_values))
    elif gap_sections:
        gap_lines.append(f'Missing sections: {", ".join(gap_sections)}')
    gap_description = "\n".join(gap_lines)

    resp = client.models.generate_content(
        model=_GEMINI_SEARCH_MODEL,
        contents=(
            f"{gap_description}\n\n"
            f"Article: {article['title']}\nAbstract: {article['abstract'][:1000]}\n\n"
            "Would this article contain information to answer the specific question or provide "
            "the missing values listed above?\n"
            'JSON only: {"relevant": true/false, "reason": "one sentence"}'
        ),
    )
    tokens_in  = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
    tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    cost = log_call(
        operation="gap_relevance_check",
        source_name=gap_title[:80],
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        model=_GEMINI_SEARCH_MODEL,
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
            return relevant, reason, cost
        except json.JSONDecodeError:
            pass
    log.warning("Relevance check unparseable response: %s", text[:100])
    return False, text, cost


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

def search_for_gap(
    gap_title: str,
    gap_sections: list[str],
    max_results: int = 5,
    bb_pool: Optional["BBPool"] = None,
    resolution_question: str = "",
    missing_values: list[str] | None = None,
    times_opened: int = 0,
) -> tuple[list[dict], float]:
    """
    Search Google/PubMed for free full-text articles covering the gap.
    Falls back to LLM-generated content (KG_FALLBACK_MODEL) when PubMed yields 0 results
    or when times_opened >= 1 (gap already attempted once without closing).
    Pass a BBPool for batch usage — sessions are shared across all gaps in the batch.
    Pass resolution_question for targeted search and fallback generation.
    Returns (articles, cost_usd).
    """
    log.info("=== search_for_gap START  gap='%s'  sections=%s  question=%s  times_opened=%d ===",
             gap_title, gap_sections, resolution_question[:80] if resolution_question else "none", times_opened)

    if times_opened >= 1:
        if KG_FALLBACK_MODEL.startswith("medgemma") and not _is_ollama_reachable():
            log.warning(
                "[gap=%s] MedGemma is down — gap left open, will be resolved when MedGemma is back",
                gap_title,
            )
            return [], 0.0
        log.info("[gap=%s] times_opened=%d >= 1 — skipping PubMed, going straight to LLM fallback",
                 gap_title, times_opened)
        llm_articles, llm_cost = _llm_fallback(gap_title, gap_sections, resolution_question, missing_values)
        log.info("=== search_for_gap END  gap='%s'  articles=%d ===", gap_title, len(llm_articles))
        return llm_articles, llm_cost

    query, search_cost = generate_search_query(gap_title, gap_sections, resolution_question)
    total_search_cost  = search_cost
    articles: list[dict] = []

    from playwright.sync_api import sync_playwright

    @contextmanager
    def _browser_ctx():
        if bb_pool is not None:
            with bb_pool.acquire() as browser:
                yield browser
        else:
            with _BB_SEM, _BB() as bb:
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(bb.cdp_url)
                    yield browser

    with _browser_ctx() as browser:

            seen_ids: set[str] = set()
            all_hits: list[dict] = []
            for h in _google_search_in_session(browser, query):
                if h["id"] not in seen_ids:
                    seen_ids.add(h["id"])
                    all_hits.append(h)

            log.info("[gap=%s] Google search found %d unique PubMed/PMC hits", gap_title, len(all_hits))

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
                except Exception as exc:
                    log.warning("[gap=%s] Metadata fetch failed for PMID %s: %s", gap_title, pmid, exc)
                    continue

                if known_pmc and not article["pmc_id"]:
                    article["pmc_id"] = known_pmc

                if not article["pmc_id"]:
                    log.debug("[gap=%s] PMID %s has no PMC full text — skipping", gap_title, pmid)
                    continue

                relevant, reason, rel_cost = _is_relevant(
                    article, gap_title, gap_sections,
                    resolution_question=resolution_question,
                    missing_values=missing_values,
                )
                total_search_cost += rel_cost
                article["relevant"]         = relevant
                article["relevance_reason"] = reason
                if relevant:
                    articles.append(article)

            browser.close()

    log.info("[gap=%s] PubMed path: %d relevant article(s) from %d hits",
             gap_title, len(articles), len(all_hits))

    # ── LLM fallback when PubMed yields nothing ───────────────────────────────
    if not articles:
        if KG_FALLBACK_MODEL.startswith("medgemma") and not _is_ollama_reachable():
            log.warning(
                "[gap=%s] MedGemma is down — gap left open, will be resolved when MedGemma is back",
                gap_title,
            )
        else:
            llm_articles, llm_cost = _llm_fallback(gap_title, gap_sections, resolution_question, missing_values)
            articles.extend(llm_articles)
            total_search_cost += llm_cost

    log.info("=== search_for_gap END  gap='%s'  articles=%d ===", gap_title, len(articles))
    return articles, total_search_cost


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
