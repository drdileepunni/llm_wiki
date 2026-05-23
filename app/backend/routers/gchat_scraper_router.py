"""
GChat scraper endpoints — connects to a debug Chrome instance via CDP.

GET  /api/settings/gchat-scraper/status  — check if debug Chrome is reachable on port 9222
POST /api/settings/gchat-scraper/sync    — scrape latest messages, filter appropriate/inappropriate,
                                           merge into persistent gchat_feedback.csv
GET  /api/settings/gchat-scraper/data    — return all stored feedback rows as JSON
"""
import csv
import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = logging.getLogger(__name__)

CDP_BASE = "http://localhost:9222"
FEEDBACK_CSV = Path(__file__).resolve().parents[2] / "gchat_feedback.csv"
CSV_FIELDS = ["time", "sender", "message", "label", "quoted_sender", "quoted_text"]


# ── Persistence helpers ────────────────────────────────────────────────────────

def _classify(message: str) -> str | None:
    """Return 'appropriate', 'inappropriate', or None."""
    m = message.strip().lower()
    if m.startswith("inappropriate"):
        return "inappropriate"
    if m.startswith("appropriate"):
        return "appropriate"
    return None


def _load_rows() -> dict[str, dict]:
    """Load existing feedback rows keyed by timestamp."""
    if not FEEDBACK_CSV.exists():
        return {}
    with open(FEEDBACK_CSV, newline="", encoding="utf-8") as f:
        return {r["time"]: r for r in csv.DictReader(f)}


def _save_rows(rows: dict[str, dict]) -> None:
    FEEDBACK_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in sorted(rows.values(), key=lambda r: r["time"]):
            w.writerow(row)

# ── Scraper JS ─────────────────────────────────────────────────────────────────
# Runs inside the Google Chat iframe.  Extracts every .nF6pT message card and
# returns a CSV string with columns:
#   sender | time | message | quoted_sender | quoted_text
_SCRAPER_JS = r"""
(() => {
  const rows = [];

  document.querySelectorAll('.nF6pT').forEach(el => {
    const sender =
      el.querySelector('[data-app-profile-name]')?.getAttribute('data-app-profile-name') ||
      el.querySelector('.njhDLd')?.innerText?.trim() || '';

    const tsMs = el.querySelector('[data-absolute-timestamp]')
                   ?.getAttribute('data-absolute-timestamp');
    const time = tsMs
      ? new Date(parseInt(tsMs)).toISOString()
      : el.querySelector('.FvYVyf')?.innerText?.trim() || '';

    const bodyEl = el.querySelector('[jsname="bgckF"]');
    if (!bodyEl) return;

    const quoteBlock = bodyEl.querySelector('.wVNE5');
    let quotedSender = '', quotedText = '';
    if (quoteBlock) {
      quotedSender = quoteBlock.querySelector('[data-name]')?.getAttribute('data-name') || '';
      quotedText   = quoteBlock.querySelector('.J87oZd')?.innerText?.trim() || '';
    }

    const bodyClone = bodyEl.cloneNode(true);
    bodyClone.querySelector('.wVNE5')?.remove();
    const message = bodyClone.innerText?.trim() || bodyEl.innerText?.trim() || '';

    if (message || quotedText) {
      rows.push({ sender, time, message, quotedSender, quotedText });
    }
  });

  const esc = v => '"' + (v || '').replace(/"/g, '""') + '"';
  return [
    'sender,time,message,quoted_sender,quoted_text',
    ...rows.map(r => [
      esc(r.sender), esc(r.time), esc(r.message),
      esc(r.quotedSender), esc(r.quotedText),
    ].join(','))
  ].join('\n');
})()
"""


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/gchat-scraper/debug")
async def gchat_scraper_debug():
    """Return all pages and frames visible to playwright — for diagnosing connection issues."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise HTTPException(status_code=500, detail="playwright not installed")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(CDP_BASE)
            result = {"contexts": []}
            for ci, ctx in enumerate(browser.contexts):
                ctx_info = {"context": ci, "pages": []}
                for pi, p in enumerate(ctx.pages):
                    frames_info = []
                    for fi, frame in enumerate(p.frames):
                        try:
                            count = await frame.evaluate("document.querySelectorAll('.nF6pT').length")
                        except Exception as e:
                            count = f"error: {e}"
                        frames_info.append({"frame": fi, "url": frame.url[:100], "nF6pT": count})
                    ctx_info["pages"].append({"page": pi, "url": p.url[:100], "frames": frames_info})
                result["contexts"].append(ctx_info)
            await browser.close()
            return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/gchat-scraper/status")
async def gchat_scraper_status():
    """Check whether a debug Chrome is reachable on localhost:9222."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{CDP_BASE}/json/version")
            data = r.json()
            return {
                "connected": True,
                "browser": data.get("Browser", "unknown"),
            }
    except Exception as exc:
        return {"connected": False, "error": str(exc)}


async def _run_scraper() -> list[dict]:
    """Connect to debug Chrome, run scraper JS, return parsed rows."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="playwright not installed — run: pip install playwright",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(CDP_BASE)

        page = None
        for ctx in browser.contexts:
            for p in ctx.pages:
                if "mail.google.com" in p.url or "chat.google.com" in p.url:
                    page = p
                    break
            if page:
                break

        if not page:
            raise HTTPException(
                status_code=404,
                detail="No Google Chat tab found — open Gmail or Chat in the debug browser first",
            )

        # Try every frame (main + all sub-frames) and pick the first one
        # that actually contains .nF6pT message cards.
        chat_frame = None
        all_frames = page.frames
        logger.info("gchat-scraper: checking %d frames on page %s", len(all_frames), page.url[:60])

        for frame in all_frames:
            try:
                count = await frame.evaluate("document.querySelectorAll('.nF6pT').length")
                logger.info("gchat-scraper: frame %s → %d messages", frame.url[:60], count)
                if count > 0:
                    chat_frame = frame
                    break
            except Exception:
                continue  # detached or cross-origin frame — skip

        if not chat_frame:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No messages found in any frame — make sure the GChat space is open "
                    "and visible in the debug Chrome window, then try again"
                ),
            )

        csv_text: str = await chat_frame.evaluate(_SCRAPER_JS)
        await browser.close()

    lines = csv_text.strip().splitlines()
    if len(lines) <= 1:
        return []

    rows = []
    reader = csv.DictReader(lines)
    for r in reader:
        rows.append(r)
    return rows


@router.post("/gchat-scraper/sync")
async def gchat_scraper_sync():
    """
    Scrape the open GChat space, keep only appropriate/inappropriate replies,
    merge with the persistent feedback CSV (dedup by timestamp), and save.
    Returns updated stats.
    """
    try:
        scraped = await _run_scraper()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("gchat-scraper: scrape failed")
        raise HTTPException(status_code=500, detail=str(exc))

    existing = _load_rows()
    new_count = 0

    for row in scraped:
        label = _classify(row.get("message", ""))
        if label is None:
            continue  # skip non-feedback messages
        ts = row.get("time", "")
        if not ts or ts in existing:
            continue  # skip already stored rows
        existing[ts] = {
            "time": ts,
            "sender": row.get("sender", ""),
            "message": row.get("message", ""),
            "label": label,
            "quoted_sender": row.get("quoted_sender", ""),
            "quoted_text": row.get("quoted_text", ""),
        }
        new_count += 1

    _save_rows(existing)
    logger.info("gchat-scraper: sync added %d new rows, total=%d", new_count, len(existing))

    return _compute_stats(existing, new_count)


@router.get("/gchat-scraper/data")
def gchat_scraper_data():
    """Return all stored feedback rows and stats."""
    existing = _load_rows()
    return _compute_stats(existing, new_count=None)


def _compute_stats(rows: dict, new_count: int | None) -> dict:
    all_rows = sorted(rows.values(), key=lambda r: r["time"])
    appropriate = [r for r in all_rows if r["label"] == "appropriate"]
    inappropriate = [r for r in all_rows if r["label"] == "inappropriate"]
    total = len(all_rows)
    accuracy = round(len(appropriate) / total * 100, 1) if total else 0.0

    result = {
        "total": total,
        "appropriate": len(appropriate),
        "inappropriate": len(inappropriate),
        "accuracy": accuracy,
        "rows": {
            "appropriate": appropriate,
            "inappropriate": inappropriate,
        },
    }
    if new_count is not None:
        result["new_rows_added"] = new_count
    return result
