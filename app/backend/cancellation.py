import threading

# Set by the uvicorn shutdown hook. Checked by long-running pipeline threads
# between chunks and between file writes so they can exit cleanly on Ctrl+C.
shutdown_event = threading.Event()

# Per-run stop events — keyed by run_id.
_stop_events: dict[str, threading.Event] = {}

# Per-run resume events — set when user approves the question review gate.
_resume_events: dict[str, threading.Event] = {}


def get_stop_event(run_id: str) -> threading.Event:
    """Return (creating if needed) the stop event for a specific run."""
    if run_id not in _stop_events:
        _stop_events[run_id] = threading.Event()
    return _stop_events[run_id]


def cancel_run(run_id: str) -> bool:
    """Signal a run to stop. Returns True if the run was known, False otherwise."""
    ev = _stop_events.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True


def cleanup_run(run_id: str) -> None:
    """Remove stop event after a run finishes (avoids unbounded growth)."""
    _stop_events.pop(run_id, None)
    _resume_events.pop(run_id, None)


def get_resume_event(run_id: str) -> threading.Event:
    """Return (creating if needed) the resume event for a specific run."""
    if run_id not in _resume_events:
        _resume_events[run_id] = threading.Event()
    return _resume_events[run_id]


def resume_run(run_id: str) -> bool:
    """Unblock a run waiting at the question-review gate. Returns True if known."""
    ev = _resume_events.get(run_id)
    if ev is None:
        return False
    ev.set()
    return True
