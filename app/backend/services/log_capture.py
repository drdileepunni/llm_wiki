"""
In-memory log capture with file export.

A singleton CaptureHandler is attached to the root logger at startup.
It is idle by default — activate via POST /api/logs/capture/start.
Lines matching _FILTER_PATTERNS are silently dropped to keep captures clean.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path

# Patterns to exclude regardless of capture state (polling noise)
_FILTER_PATTERNS = (
    "GET /api/viva/viva_",
    "GET /api/learn/jobs/",
    "GET /api/clinical-assess/jobs/",
    "GET /api/resolve/batch/",
    "GET /api/resolve/jobs/",
    "/health",
    # uvicorn access log lines for the same paths
    '"GET /api/viva/viva_',
    '"GET /api/learn/jobs/',
    '"GET /api/clinical-assess/jobs/',
    '"GET /api/resolve/batch/',
    '"GET /api/resolve/jobs/',
)

_LOG_DIR = Path(__file__).parent.parent.parent / "logs"


class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._active = False
        self._lines: list[str] = []
        self._started_at: str | None = None
        self._last_file: str | None = None
        self._last_line_count: int = 0

    # ── Public state ──────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def started_at(self) -> str | None:
        return self._started_at

    @property
    def last_file(self) -> str | None:
        return self._last_file

    @property
    def line_count(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def last_line_count(self) -> int:
        return self._last_line_count

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            self._lines = []
            self._active = True
            self._started_at = datetime.utcnow().isoformat()

    def stop(self) -> str:
        """Stop capture, write buffer to a timestamped file, return its path."""
        with self._lock:
            self._active = False
            lines = list(self._lines)
            self._last_line_count = len(lines)

        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"capture_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        path = _LOG_DIR / filename
        path.write_text("\n".join(lines), encoding="utf-8")
        self._last_file = str(path)
        return str(path)

    def list_files(self) -> list[dict]:
        """Return saved capture files newest-first."""
        if not _LOG_DIR.exists():
            return []
        return sorted(
            [
                {"filename": f.name, "size_bytes": f.stat().st_size,
                 "modified": datetime.utcfromtimestamp(f.stat().st_mtime).isoformat()}
                for f in _LOG_DIR.glob("capture_*.txt")
            ],
            key=lambda x: x["modified"],
            reverse=True,
        )

    # ── logging.Handler interface ─────────────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        if not self._active:
            return
        try:
            msg = self.format(record)
            if any(p in msg for p in _FILTER_PATTERNS):
                return
            with self._lock:
                if self._active:
                    self._lines.append(msg)
        except Exception:
            self.handleError(record)


# Singleton — imported by main.py and routers/logs.py
capture_handler = CaptureHandler()
capture_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
