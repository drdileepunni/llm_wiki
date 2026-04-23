"""
Step 1 – Build the full patient timeline from three JSON files.

Loads patients.json, tasks.json, chat.json and runs all six extractors,
producing a sorted list of TimelineEvent objects which is then rendered
to a CSV file.

Returns the path to the written CSV.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .lib.date_utils import load_json
from .lib.extractors.orders    import extract_order_events
from .lib.extractors.notes     import extract_note_events
from .lib.extractors.tasks     import extract_task_events
from .lib.extractors.chat      import extract_chat_events
from .lib.extractors.vitals    import extract_vital_events
from .lib.extractors.documents import extract_document_events
from .lib.renderers.csv_writer import render_csv

logger = logging.getLogger(__name__)

# Events from patients.json should sort before tasks.json, then chat.json
_SOURCE_PRIORITY = {"patients.json": 0, "tasks.json": 1, "chat.json": 2}


def build(
    json_dir: Path,
    cpmrn: str,
    output_csv: Path,
    *,
    gemini_enabled: bool = True,
) -> Path:
    """
    Run all six extractors against the three JSON files in json_dir and
    write the full timeline to output_csv.

    Args:
        json_dir:        Directory containing patients.json, tasks.json, chat.json.
        cpmrn:           Patient identifier (used only for logging).
        output_csv:      Where to write the full timeline CSV.
        gemini_enabled:  Whether to call Gemini for clinical note event extraction.

    Returns:
        output_csv path (for chaining).
    """
    logger.info("Loading JSON files from %s …", json_dir)
    patients_data = load_json(json_dir / "patients.json")
    patient = patients_data[0] if isinstance(patients_data, list) else patients_data
    tasks   = load_json(json_dir / "tasks.json")
    chat    = load_json(json_dir / "chat.json")

    logger.info("Extracting order events …")
    order_events = extract_order_events(patient)
    logger.info("  → %d order events", len(order_events))

    logger.info(
        "Extracting note events (%s) …",
        "Gemini enabled" if gemini_enabled else "Gemini disabled",
    )
    note_events = extract_note_events(patient, gemini_enabled=gemini_enabled)
    logger.info("  → %d note events", len(note_events))

    logger.info("Extracting task events …")
    task_events = extract_task_events(tasks)
    logger.info("  → %d task events", len(task_events))

    logger.info("Extracting chat events …")
    chat_events = extract_chat_events(chat)
    logger.info("  → %d chat events", len(chat_events))

    logger.info("Extracting vital events …")
    vital_events = extract_vital_events(patient)
    logger.info("  → %d vital events", len(vital_events))

    logger.info("Extracting lab result events …")
    doc_events = extract_document_events(patient)
    logger.info("  → %d lab events", len(doc_events))

    all_events = (
        order_events + note_events + task_events +
        chat_events  + vital_events + doc_events
    )

    # Drop events with unparseable timestamps
    all_events = [e for e in all_events if e.timestamp_utc is not None]

    # Stable sort: primary key = UTC timestamp, secondary = source file priority
    all_events.sort(
        key=lambda e: (
            e.timestamp_utc,
            _SOURCE_PRIORITY.get(e.source_file, 9),
        )
    )

    logger.info("Total events: %d → writing %s", len(all_events), output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    render_csv(all_events, output_csv)

    return output_csv
