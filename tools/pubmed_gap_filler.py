#!/usr/bin/env python3
"""
tools/pubmed_gap_filler.py

Given a knowledge gap (entity title + missing sections), uses Claude to
generate optimal Google search queries, then finds PubMed/PMC free full-text
articles, evaluates each abstract, and downloads the PDF if relevant.

Flow:
  LLM → search queries
  → Google (Browserbase) → filter pubmed.ncbi.nlm.nih.gov + pmc.ncbi.nlm.nih.gov URLs
  → E-utilities abstract fetch → LLM relevance check
  → PDF download (direct HTTPS, Browserbase fallback)

Usage:
    uv run tools/pubmed_gap_filler.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

_env_path = Path(__file__).parent.parent / "app" / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(_env_path)

ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
BROWSERBASE_API_KEY    = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
NCBI_API_KEY           = os.getenv("NCBI_API_KEY", "")

EUTILS_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
DOWNLOAD_DIR = Path(__file__).parent.parent / "downloads" / "pubmed"
RATE_DELAY   = 0.35 if not NCBI_API_KEY else 0.11


# ── LLM search query generation ───────────────────────────────────────────────

def generate_search_queries(gap_title: str, gap_sections: list[str]) -> list[str]:
    """
    Ask Claude Haiku to produce 3 Google search strings that would find a free
    full-text PubMed review article covering the gap.  Simpler is usually better:
    e.g. '0.9 Saline Solution + [many sections]' → 'normal saline review pubmed'.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    sections_str = ", ".join(gap_sections)

    prompt = (
        f"I need to fill a knowledge gap about: \"{gap_title}\"\n"
        f"Missing information: {sections_str}\n\n"
        "Generate 3 Google search queries that would find free full-text PubMed "
        "review articles covering this topic. Rules:\n"
        "- Each query should end with 'pubmed free full text' or 'PMC review'\n"
        "- Use the simplest clinical/scientific name for the topic (e.g. prefer "
        "'normal saline' over '0.9 Saline Solution Adverse effects Composition')\n"
        "- Vary the angle: one broad review, one focused on the most important "
        "missing section, one using an alternate name if one exists\n"
        "- Keep each query under 10 words\n"
        'Return ONLY a JSON array of 3 strings, e.g. ["query 1", "query 2", "query 3"]'
    )

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )

    text = resp.content[0].text.strip()
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            queries = json.loads(match.group())
            if isinstance(queries, list) and queries:
                print(f"  LLM queries: {queries}")
                return [str(q) for q in queries]
        except json.JSONDecodeError:
            pass

    # fallback: simple concatenation
    fallback = f"{gap_title} review pubmed free full text"
    print(f"  LLM query generation failed — fallback: {fallback!r}")
    return [fallback]


# ── Browserbase session helper ────────────────────────────────────────────────

class BrowserbaseSession:
    def __init__(self):
        from stagehand import Stagehand
        self._sh = Stagehand(
            browserbase_api_key=BROWSERBASE_API_KEY,
            browserbase_project_id=BROWSERBASE_PROJECT_ID,
            model_api_key=ANTHROPIC_API_KEY,
            server="remote",
        )
        self.session_id = ""
        self.cdp_url    = ""

    def __enter__(self):
        resp = self._sh.sessions.start(model_name="gpt-4o")
        self.session_id = resp.data.session_id
        self.cdp_url    = resp.data.cdp_url
        print(f"  Browserbase session: {self.session_id}")
        return self

    def __exit__(self, *_):
        try:
            self._sh.sessions.end(self.session_id)
        except Exception:
            pass
        self._sh.close()


# ── Google → NCBI URL extraction ─────────────────────────────────────────────

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
            hits.append({"type": "pubmed", "url": m.group(1), "id": pmid})
    for m in _RE_PMC.finditer(html):
        pmc_id = m.group(2).upper()
        if pmc_id not in seen:
            seen.add(pmc_id)
            hits.append({"type": "pmc", "url": m.group(1), "id": pmc_id})
    return hits


def _google_search_once(query: str) -> list[dict]:
    search_url = "https://www.google.com/search?" + urlencode({"q": query, "num": 20})
    print(f"  Searching: {query!r}")

    from playwright.sync_api import sync_playwright

    with BrowserbaseSession() as bb:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bb.cdp_url)
            ctx  = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=30_000)
            try:
                btn = page.locator('button:has-text("Accept all")').first
                if btn.is_visible(timeout=3_000):
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            html = page.content()
            browser.close()

    hits = _extract_ncbi_hits(html)
    pubmed_n = sum(h["type"] == "pubmed" for h in hits)
    pmc_n    = sum(h["type"] == "pmc"    for h in hits)
    print(f"  → {len(hits)} hits ({pubmed_n} PubMed, {pmc_n} PMC)")
    return hits


def google_search_ncbi(gap_title: str, gap_sections: list[str], need: int = 5) -> list[dict]:
    """
    Generate LLM queries, run them one at a time until `need` unique hits found.
    Returns deduplicated list of {type, url, id} dicts.
    """
    if not BROWSERBASE_API_KEY or not BROWSERBASE_PROJECT_ID:
        sys.exit("BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID not set.")

    queries   = generate_search_queries(gap_title, gap_sections)
    seen_ids: set[str] = set()
    all_hits: list[dict] = []

    for query in queries:
        if len(all_hits) >= need:
            break
        hits = _google_search_once(query)
        for h in hits:
            if h["id"] not in seen_ids:
                seen_ids.add(h["id"])
                all_hits.append(h)

    print(f"\n  Total unique NCBI hits across all queries: {len(all_hits)}")
    return all_hits


# ── PubMed E-utilities ────────────────────────────────────────────────────────

def _ncbi_params(**kwargs) -> dict:
    p = dict(kwargs)
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    return p


def pmc_id_to_pmid(pmc_id: str) -> Optional[str]:
    numeric = pmc_id.replace("PMC", "")
    params  = _ncbi_params(dbfrom="pmc", db="pubmed", id=numeric, retmode="json")
    try:
        resp = httpx.get(f"{EUTILS_BASE}/elink.fcgi", params=params, timeout=20)
        resp.raise_for_status()
        for ls in resp.json().get("linksets", []):
            for lsd in ls.get("linksetdbs", []):
                ids = lsd.get("links", [])
                if ids:
                    return str(ids[0])
    except Exception as exc:
        print(f"    elink failed for {pmc_id}: {exc}")
    return None


def fetch_article_meta(pmid: str) -> dict:
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
    params = _ncbi_params(db="pubmed", id=pmid, rettype="abstract", retmode="text")
    abstract_text = httpx.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=30).text.strip()

    return {
        "pmid":     pmid,
        "pmc_id":   pmc_id,
        "title":    title,
        "authors":  authors,
        "journal":  journal,
        "pub_date": pub_date,
        "abstract": abstract_text,
        "citation": f"{authors}. {title}. {journal}. {pub_date}. PMID:{pmid}",
    }


# ── Abstract relevance check ──────────────────────────────────────────────────

def is_abstract_relevant(article: dict, gap_title: str, gap_sections: list[str]) -> tuple[bool, str]:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        f'Knowledge gap: "{gap_title}"\n'
        f"Missing sections: {', '.join(gap_sections)}\n\n"
        f"Article: {article['title']}\n"
        f"Abstract:\n{article['abstract']}\n\n"
        "Would the full text likely fill one or more missing sections?\n"
        'JSON only: {"relevant": true/false, "reason": "one sentence"}'
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    text  = resp.content[0].text.strip()
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return bool(data.get("relevant")), data.get("reason", "")
        except json.JSONDecodeError:
            pass
    return '"relevant": true' in text.lower(), text


# ── Full-text + image retrieval ───────────────────────────────────────────────

def _safe_stem(pmc_id: str, title: str) -> str:
    safe = re.sub(r'[^\w\s-]', '_', title[:60]).strip()
    return f"{pmc_id}_{safe}"


def _fetch_pmc_html(pmc_id: str) -> Optional[str]:
    """
    Fetch the PMC article HTML page.
    Tries plain httpx first; falls back to Browserbase if the page looks blocked.
    """
    url     = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True, headers=headers)
        resp.raise_for_status()
        if len(resp.text) > 5000 and "<article" in resp.text:
            print(f"    httpx: {len(resp.text):,} bytes")
            return resp.text
        print("    httpx: response too short or missing article tag — trying Browserbase")
    except Exception as exc:
        print(f"    httpx failed: {exc} — trying Browserbase")

    # Browserbase fallback
    from playwright.sync_api import sync_playwright
    print(f"    Browserbase fetch for {pmc_id}...")
    with BrowserbaseSession() as bb:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bb.cdp_url)
            ctx     = browser.contexts[0]
            page    = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)
            html = page.content()
            browser.close()
    if len(html) > 5000:
        print(f"    Browserbase: {len(html):,} bytes")
        return html
    print("    Browserbase: page too short — likely blocked")
    return None


def _fetch_image_b64(img_url: str) -> Optional[tuple[str, str]]:
    """Fetch image and return (mime_type, base64_string), or None on failure."""
    import base64
    headers = {"User-Agent": "Mozilla/5.0 (compatible; wiki-gap-filler/1.0)"}
    try:
        resp = httpx.get(img_url, timeout=20, follow_redirects=True, headers=headers)
        resp.raise_for_status()
        if len(resp.content) < 512:   # skip icons / tracking pixels
            return None
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not ct.startswith("image/"):
            ct = "image/jpeg"
        return ct, base64.b64encode(resp.content).decode("ascii")
    except Exception as exc:
        print(f"      Image fetch failed ({img_url}): {exc}")
        return None


def _table_to_md(node) -> str:
    """Convert a BeautifulSoup <table> to a column-padded GFM pipe table."""
    rows_raw = []
    for tr in node.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
        rows_raw.append(cells)
    if not rows_raw:
        return ""
    ncols  = max(len(r) for r in rows_raw)
    rows   = [r + [""] * (ncols - len(r)) for r in rows_raw]
    widths = [max(max(len(rows[i][c]) for i in range(len(rows))), 3) for c in range(ncols)]

    def fmt(row):
        return "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    lines = [fmt(rows[0]), sep] + [fmt(r) for r in rows[1:]]
    return "\n" + "\n".join(lines) + "\n"


def _html_to_markdown(html: str, pmc_id: str) -> str:
    """
    Convert PMC article HTML to self-contained Markdown.
    Images are embedded as base64 data URIs — no external files needed.
    Tables are column-padded GFM pipe tables.
    """
    from bs4 import BeautifulSoup, NavigableString, Tag
    from urllib.parse import urljoin

    base_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
    soup     = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "noscript", "nav",
                               "header", "footer", "aside"]):
        tag.decompose()

    body      = soup.find("article") or soup.find("div", {"class": "article-body"}) or soup.body or soup
    img_count = [0]

    def embed_img(src: str, caption: str) -> str:
        abs_url = urljoin(base_url, src) if not src.startswith("http") else src
        img_count[0] += 1
        result = _fetch_image_b64(abs_url)
        if result:
            mime, b64 = result
            return f"![{caption}](data:{mime};base64,{b64})\n"
        return ""   # silently drop failed images

    def node_to_md(node) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""

        tag  = node.name
        kids = "".join(node_to_md(c) for c in node.children)

        if tag == "h1":                   return f"\n# {kids.strip()}\n\n"
        if tag == "h2":                   return f"\n## {kids.strip()}\n\n"
        if tag == "h3":                   return f"\n### {kids.strip()}\n\n"
        if tag in ("h4", "h5", "h6"):     return f"\n#### {kids.strip()}\n\n"
        if tag == "p":                    return f"\n{kids.strip()}\n"
        if tag in ("strong", "b"):        return f"**{kids}**"
        if tag in ("em", "i"):            return f"*{kids}*"
        if tag == "br":                   return "\n"
        if tag in ("ul", "ol"):           return f"\n{kids}\n"
        if tag == "li":                   return f"- {kids.strip()}\n"
        if tag in ("sup", "sub"):         return f"[{kids.strip()}]"
        if tag == "a":
            href = node.get("href", "")
            return f"[{kids.strip()}]({href})" if href and kids.strip() else kids
        if tag == "table":                return _table_to_md(node)
        if tag in ("thead", "tbody", "tfoot", "tr", "th", "td"):
            return ""   # consumed by _table_to_md

        if tag == "figure":
            img_tag = node.find("img")
            cap_tag = node.find(["figcaption", "p"])
            caption = cap_tag.get_text(" ", strip=True) if cap_tag else ""
            if img_tag:
                src = img_tag.get("src") or img_tag.get("data-src", "")
                if src and not src.startswith("data:"):
                    return "\n" + embed_img(src, caption) + "\n"
            return f"\n*[Figure: {caption}]*\n" if caption else ""

        if tag == "img":
            src = node.get("src") or node.get("data-src", "")
            alt = node.get("alt", "")
            if src and not src.startswith("data:"):
                return embed_img(src, alt)
            return ""

        return kids

    md = node_to_md(body)
    md = re.sub(r'\n{3,}', '\n\n', md)
    print(f"    Markdown: {len(md):,} chars, {img_count[0]} image(s) embedded")
    return md.strip()


def fetch_and_save(pmc_id: str, title: str, out_dir: Path) -> Optional[Path]:
    """Fetch PMC article as a single self-contained Markdown file with embedded images."""
    stem    = _safe_stem(pmc_id, title)
    md_path = out_dir / f"{stem}.md"
    out_dir.mkdir(parents=True, exist_ok=True)

    html = _fetch_pmc_html(pmc_id)
    if not html:
        return None

    print(f"    Converting to Markdown...")
    md = _html_to_markdown(html, pmc_id)

    if len(md) < 500:
        print("    Markdown too short — extraction likely failed")
        return None

    md_path.write_text(md, encoding="utf-8")
    print(f"    Saved: {md_path.name}")
    return md_path


# ── Orchestrator ──────────────────────────────────────────────────────────────

def fill_gap(
    gap_title:     str,
    gap_sections:  list[str],
    max_downloads: int  = 3,
    out_dir:       Path = DOWNLOAD_DIR,
) -> list[dict]:
    print(f"\n{'='*64}")
    print(f"Gap:      {gap_title}")
    print(f"Sections: {', '.join(gap_sections)}")
    print(f"{'='*64}\n")

    hits = google_search_ncbi(gap_title, gap_sections, need=max_downloads * 3)
    if not hits:
        print("No NCBI URLs found.")
        return []

    downloaded: list[dict] = []

    for hit in hits:
        if len(downloaded) >= max_downloads:
            break

        # Resolve PMID
        if hit["type"] == "pmc":
            print(f"\n[{hit['id']}] Resolving PMC → PMID...")
            pmid = pmc_id_to_pmid(hit["id"])
            if not pmid:
                print("  Could not resolve PMID — skipping")
                continue
            known_pmc = hit["id"]
        else:
            pmid      = hit["id"]
            known_pmc = None

        print(f"[PMID {pmid}] Fetching metadata...")
        try:
            article = fetch_article_meta(pmid)
        except Exception as exc:
            print(f"  Failed: {exc}")
            continue

        # If we arrived via a PMC URL, we already know it's open access
        if known_pmc and not article["pmc_id"]:
            article["pmc_id"] = known_pmc

        print(f"  Title:  {article['title'][:80]}")
        print(f"  PMC ID: {article['pmc_id'] or '— no free full text'}")

        if not article["pmc_id"]:
            print("  Skipped (not open access)")
            continue

        relevant, reason = is_abstract_relevant(article, gap_title, gap_sections)
        print(f"  Relevant: {'YES' if relevant else 'NO'} — {reason}")
        if not relevant:
            continue

        print(f"  Fetching full text + images ({article['pmc_id']})...")
        md_path = fetch_and_save(article["pmc_id"], article["title"], out_dir)
        article["md_path"] = str(md_path) if md_path else None
        print(f"  {'Saved → ' + str(md_path) if md_path else 'Fetch failed.'}")
        downloaded.append(article)

    print(f"\n── Summary {'─'*50}")
    print(f"Downloaded {len(downloaded)} article(s) for '{gap_title}'")
    for a in downloaded:
        print(f"  [{a['pmid']}] {a['title'][:70]}")
        print(f"          {a['md_path'] or 'no file'}")

    return downloaded


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY not set.")
    if not BROWSERBASE_API_KEY:
        sys.exit("BROWSERBASE_API_KEY not set.")

    fill_gap(
        gap_title="0.9 Saline Solution",
        gap_sections=[
            "Adverse effects",
            "Composition details",
            "Contraindications",
            "Indications",
            "Physiological effects",
        ],
    )
