"""
Test script: resolve the Amikacin KG using only OpenEvidence via Browserbase.

Usage:
    cd /Users/drdileepunni/github_/llm_wiki
    python -m tools.test_openevidence

What it does:
  1. Opens ONE Browserbase session
  2. Searches OpenEvidence for "Amikacin"
  3. Prints the extracted text + relevance verdict
  4. Closes the session
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_openevidence")

# ── load env ──────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / "app" / ".env")

BROWSERBASE_API_KEY    = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY         = os.getenv("GOOGLE_API_KEY", "")
OE_EMAIL               = os.getenv("OE_EMAIL", "")
OE_PASSWORD            = os.getenv("OE_PASSWORD", "")

if not BROWSERBASE_API_KEY:
    sys.exit("❌  BROWSERBASE_API_KEY not set in app/.env")
if not OE_EMAIL or not OE_PASSWORD:
    sys.exit("❌  OE_EMAIL / OE_PASSWORD not set in app/.env")

GAP_TITLE    = "Amikacin"
GAP_SECTIONS = ["Adverse effects", "Contraindications", "Indications", "Mechanism of action", "Monitoring"]


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
        self.cdp_url     = ""
        self._session_id = ""

    def __enter__(self):
        resp = self._sh.sessions.start(model_name="gpt-4o")
        self._session_id = resp.data.session_id
        self.cdp_url     = resp.data.cdp_url
        log.info("BB session started: %s", self._session_id)
        return self

    def __exit__(self, *_):
        try:
            self._sh.sessions.end(self._session_id)
            log.info("BB session ended: %s", self._session_id)
        except Exception:
            pass
        self._sh.close()


# ── HTML → text ───────────────────────────────────────────────────────────────

def _html_to_text(html: str) -> str:
    import re
    from bs4 import BeautifulSoup, NavigableString, Tag

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.find("div", {"class": "article-body"}) or soup.body or soup

    def walk(node) -> str:
        if isinstance(node, NavigableString):
            return str(node)
        if not isinstance(node, Tag):
            return ""
        tag  = node.name
        kids = "".join(walk(c) for c in node.children)
        if tag in ("h1", "h2", "h3", "h4"):
            return f"\n\n## {kids.strip()}\n\n"
        if tag == "p":
            return f"\n{kids.strip()}\n"
        if tag in ("ul", "ol"):
            return f"\n{kids}\n"
        if tag == "li":
            return f"- {kids.strip()}\n"
        if tag == "br":
            return "\n"
        return kids

    text = walk(body)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# ── OpenEvidence search ───────────────────────────────────────────────────────

def _login_openevidence(page) -> bool:
    """Handle the OE login wall. Returns True on success."""
    try:
        page.screenshot(path="/tmp/oe_before_login.png")
        log.info("Pre-login screenshot → /tmp/oe_before_login.png")

        # Click "Log in" link to reach the existing-account flow (not signup)
        login_link = page.locator('a:has-text("Log in"), a:has-text("Sign in"), button:has-text("Log in"), button:has-text("Sign in")')
        if login_link.count() > 0:
            log.info("Clicking 'Log in' link …")
            login_link.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        else:
            log.info("No 'Log in' link found — assuming already on login form")

        page.screenshot(path="/tmp/oe_login_form.png")
        log.info("Login form screenshot → /tmp/oe_login_form.png")

        # Step 1: enter email and press Enter (Auth0 form has a hidden submit button)
        page.wait_for_selector('#username, input[type="email"], input[name="email"]', timeout=8_000)
        email_inp = page.locator('#username, input[type="email"], input[name="email"]').first
        email_inp.fill(OE_EMAIL)
        log.info("Entered email, pressing Enter …")
        email_inp.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=15_000)

        page.screenshot(path="/tmp/oe_after_email.png")
        log.info("After-email screenshot → /tmp/oe_after_email.png")

        # Step 2: enter password — Auth0 may navigate to a separate page or reveal inline
        # Skip networkidle (Auth0 keeps polling); just wait for ANY password input in DOM
        page.wait_for_selector('input[type="password"]', timeout=20_000)
        log.info("Entered password, pressing Enter …")
        # force=True bypasses visibility/actionability checks
        page.locator('input[type="password"]').first.fill(OE_PASSWORD, force=True)
        page.keyboard.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=20_000)
        log.info("Login complete")
        return True
    except Exception as exc:
        log.error("Login failed: %s", exc)
        page.screenshot(path="/tmp/oe_login_fail.png")
        log.info("Screenshot saved to /tmp/oe_login_fail.png")
        return False


def search_openevidence(gap_title: str) -> str | None:
    from playwright.sync_api import sync_playwright

    with _BB() as bb:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bb.cdp_url)
            ctx  = browser.contexts[0]
            page = ctx.new_page()

            log.info("Navigating to openevidence.com …")
            page.goto("https://www.openevidence.com/", wait_until="domcontentloaded", timeout=30_000)

            # Handle login wall if present
            if page.locator('input[type="email"], input[name="email"]').count() > 0:
                log.info("Login wall detected — logging in …")
                if not _login_openevidence(page):
                    browser.close()
                    return None
                # After login, navigate back to home for the search
                page.goto("https://www.openevidence.com/", wait_until="domcontentloaded", timeout=30_000)

            page.screenshot(path="/tmp/oe_after_login.png")
            log.info("Post-login screenshot → /tmp/oe_after_login.png")

            # Find the search / ask input
            search_sel = (
                'textarea, '
                'input[type="search"], '
                'input[placeholder*="ask" i], '
                'input[placeholder*="search" i], '
                'input[placeholder*="question" i]'
            )
            try:
                page.wait_for_selector(search_sel, timeout=10_000)
                inp = page.locator(search_sel).first
                log.info("Found search input, typing query: %r", gap_title)
                inp.fill(gap_title)
                inp.press("Enter")
                log.info("Waiting for answer to load …")
                # Step 1: wait for OE to start processing (loading indicator appears)
                loading_phrases = ["Analyzing query", "Searching medical literature", "Searching published"]
                js_loading = (
                    "() => ["
                    + ", ".join(f'"{p}"' for p in loading_phrases)
                    + "].some(p => document.body.innerText.includes(p))"
                )
                js_done = (
                    "() => !["
                    + ", ".join(f'"{p}"' for p in loading_phrases)
                    + "].some(p => document.body.innerText.includes(p))"
                )
                try:
                    page.wait_for_function(js_loading, timeout=15_000)
                    log.info("Processing started, waiting for answer …")
                except Exception:
                    log.info("Loading indicators not detected, waiting anyway …")
                # Step 2: wait for loading to finish
                try:
                    page.wait_for_function(js_done, timeout=90_000)
                    log.info("Answer fully loaded")
                    page.wait_for_timeout(2_000)
                except Exception:
                    log.warning("Timed out waiting for answer — grabbing whatever is there")
            except Exception as exc:
                log.error("Search interaction failed: %s", exc)
                page.screenshot(path="/tmp/oe_debug.png")
                log.info("Screenshot saved to /tmp/oe_debug.png")
                browser.close()
                return None

            html = page.content()
            page.screenshot(path="/tmp/oe_result.png")
            log.info("Result screenshot → /tmp/oe_result.png")
            browser.close()

    text = _html_to_text(html)
    return text if len(text) >= 300 else None


# ── Relevance check ───────────────────────────────────────────────────────────

def check_relevance(text: str, gap_title: str, gap_sections: list[str]) -> tuple[bool, str]:
    import json, re as _re
    from google import genai

    client = genai.Client(api_key=GOOGLE_API_KEY)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=(
            f'Gap: "{gap_title}"\nMissing: {", ".join(gap_sections)}\n\n'
            f"Article: OpenEvidence result\nAbstract: {text[:1500]}\n\n"
            "Would this fill one or more missing sections?\n"
            'JSON only: {"relevant": true/false, "reason": "one sentence", "sections_covered": ["..."]}'
        ),
    )
    raw = (resp.text or "").strip()
    m = _re.search(r'\{.*\}', raw, _re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return bool(data.get("relevant")), data.get("reason", ""), data.get("sections_covered", [])
        except Exception:
            pass
    return False, raw, []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== OpenEvidence test: gap=%r  sections=%s ===", GAP_TITLE, GAP_SECTIONS)

    text = search_openevidence(GAP_TITLE)

    if not text:
        print("\n❌  OpenEvidence returned no usable content.")
        return

    print(f"\n{'='*60}")
    print(f"EXTRACTED TEXT  ({len(text)} chars)")
    print('='*60)
    print(text[:3000])
    if len(text) > 3000:
        print(f"\n… [{len(text) - 3000} more chars truncated] …")

    print(f"\n{'='*60}")
    print("RELEVANCE CHECK")
    print('='*60)

    relevant, reason, sections_covered = check_relevance(text, GAP_TITLE, GAP_SECTIONS)

    print(f"Relevant:         {'✅  Yes' if relevant else '❌  No'}")
    print(f"Reason:           {reason}")
    print(f"Sections covered: {sections_covered or '(none identified)'}")
    print()


if __name__ == "__main__":
    main()
