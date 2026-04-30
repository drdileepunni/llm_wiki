"""
Page-level usage metrics store.

Tracks per wiki page:
  cds_query_count   — times retrieved during a CDS vector search (centrality)
  last_queried      — date of most recent CDS hit
  gap_opens         — times a knowledge gap has been registered/re-opened
  gap_first_opened  — date the first gap was registered
  gap_last_opened   — date of most recent gap registration
  persistent_gap    — true when gap_opens >= PERSISTENT_THRESHOLD

Stored in wiki/page_metrics.json as a flat dict keyed by page path.
All writes are protected by a per-process threading lock.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date
from pathlib import Path

log = logging.getLogger("wiki.page_metrics")

PERSISTENT_THRESHOLD = 3
_METRICS_FILE = "page_metrics.json"

_lock = threading.Lock()


# ── I/O ───────────────────────────────────────────────────────────────────────

def _load(wiki_dir: Path) -> dict:
    p = wiki_dir / _METRICS_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("page_metrics: corrupt file, resetting: %s", exc)
        return {}


def _save(wiki_dir: Path, data: dict) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / _METRICS_FILE).write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def record_query(page_path: str, wiki_dir: Path) -> None:
    """
    Increment cds_query_count for a page.
    Call once per unique page retrieved in a CDS Step-2 vector search.
    """
    with _lock:
        data  = _load(wiki_dir)
        entry = data.setdefault(page_path, {})
        entry["cds_query_count"] = entry.get("cds_query_count", 0) + 1
        entry["last_queried"]    = date.today().isoformat()
        _save(wiki_dir, data)
    log.debug("page_metrics: query recorded  page=%s  total=%d",
              page_path, data[page_path]["cds_query_count"])


def record_gap_open(page_path: str, wiki_dir: Path) -> None:
    """
    Increment gap_opens for a page.
    Sets persistent_gap=True when gap_opens reaches PERSISTENT_THRESHOLD.
    Call every time write_gap_files creates or updates a gap for this page.
    """
    with _lock:
        data  = _load(wiki_dir)
        entry = data.setdefault(page_path, {})
        entry["gap_opens"] = entry.get("gap_opens", 0) + 1
        today = date.today().isoformat()
        if "gap_first_opened" not in entry:
            entry["gap_first_opened"] = today
        entry["gap_last_opened"] = today
        entry["persistent_gap"]  = entry["gap_opens"] >= PERSISTENT_THRESHOLD
        _save(wiki_dir, data)

    opens = data[page_path]["gap_opens"]
    if data[page_path]["persistent_gap"]:
        log.warning(
            "page_metrics: PERSISTENT GAP  page=%s  gap_opens=%d  "
            "(>= threshold %d — may need manual intervention)",
            page_path, opens, PERSISTENT_THRESHOLD,
        )
    else:
        log.debug("page_metrics: gap_open recorded  page=%s  total=%d", page_path, opens)


def get_all(wiki_dir: Path) -> dict:
    """Return the full metrics dict keyed by page path."""
    return _load(wiki_dir)


def get_page(page_path: str, wiki_dir: Path) -> dict:
    """Return metrics for a single page. Empty dict if not tracked yet."""
    return _load(wiki_dir).get(page_path, {})
