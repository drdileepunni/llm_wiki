"""
Structured activity log for wiki changes.

Each pipeline write appends a JSON line to wiki/activity.jsonl.
Schema per event:
  timestamp     ISO-8601 UTC string
  operation     "ingest" | "gap_resolve"
  kb            KB name
  source        Human-readable source/article title
  files_written list of wiki-relative paths written
  gaps_opened   list of gap file paths opened
  gaps_closed   list of gap file paths closed/resolved
  sections_filled list of section names filled (gap_resolve only)
  tokens_in     int
  tokens_out    int
  cost_usd      float
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("wiki.activity_log")
_ACTIVITY_FILE = "activity.jsonl"


def append_event(wiki_dir: Path, event: dict) -> None:
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    path = wiki_dir / _ACTIVITY_FILE
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("activity_log: failed to write event: %s", exc)


def read_events(wiki_dir: Path, limit: int = 500) -> list[dict]:
    """Return the most recent `limit` events, newest first."""
    path = wiki_dir / _ACTIVITY_FILE
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return list(reversed(events[-limit:]))
