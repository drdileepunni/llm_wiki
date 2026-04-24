"""
MongoDB-backed cache for patient timeline JSON data.

Replaces the .timeline_cache file system with three MongoDB collections:
  patients  — patient documents (upserted by _id)
  tasks     — task documents    (upserted by _id)
  chat      — chat documents    (upserted by _id)

All three JSON files exported from GCP use MongoDB Extended JSON format
($date, $oid, $numberLong).  We use bson.json_util to round-trip correctly:
  store: json_util.loads() → proper BSON → pymongo insert
  load:  pymongo find()   → json_util.dumps() → json.loads()
         → same dict shape the extractors already expect
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, ReplaceOne

logger = logging.getLogger(__name__)

_client: MongoClient | None = None


def _get_db():
    global _client
    if _client is None:
        uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
        _client = MongoClient(uri)
    db_name = os.environ.get("DB_NAME", "emr-local")
    return _client[db_name]


# ── Public API ─────────────────────────────────────────────────────────────────

def is_cached(cpmrn: str, encounter: int) -> bool:
    """Return True if all three collections already hold data for this encounter."""
    db = _get_db()
    has_patient = db.patients.count_documents(
        {"CPMRN": cpmrn, "encounters": encounter}, limit=1
    ) > 0
    has_tasks = db.tasks.count_documents(
        {"CPMRN": cpmrn, "encounters": encounter}, limit=1
    ) > 0
    has_chat = db.chat.count_documents(
        {"identifier": f"{cpmrn}:{encounter}"}, limit=1
    ) > 0
    return has_patient and has_tasks and has_chat


def store_export(json_dir: Path, cpmrn: str, encounter: int) -> None:
    """
    Load the three GCP-exported JSON files into MongoDB and delete them.

    patients.json  → patients collection  (upsert by _id)
    tasks.json     → tasks     collection  (upsert by _id)
    chat.json      → chat      collection  (upsert by _id)
    """
    db = _get_db()

    mapping = [
        ("patients.json", db.patients),
        ("tasks.json",    db.tasks),
        ("chat.json",     db.chat),
    ]

    for fname, collection in mapping:
        path = json_dir / fname
        if not path.exists():
            raise FileNotFoundError(f"Expected export file not found: {path}")

        with open(path) as f:
            raw = f.read()

        docs = json_util.loads(raw)
        if not isinstance(docs, list):
            docs = [docs]

        if not docs:
            logger.warning("  [mongo_cache] %s is empty – nothing to store", fname)
            continue

        ops = [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in docs]
        result = collection.bulk_write(ops)
        logger.info(
            "  [mongo_cache] %s → %s: %d upserted, %d modified",
            fname, collection.name,
            result.upserted_count, result.modified_count,
        )

        path.unlink()
        logger.info("  [mongo_cache] deleted %s", path)

    # Remove the now-empty directory (non-recursive; only deletes if empty)
    try:
        json_dir.rmdir()
    except OSError:
        pass  # not empty for some reason – leave it


def load_for_timeline(cpmrn: str, encounter: int) -> tuple[dict, list, list]:
    """
    Load patient, tasks, and chat data from MongoDB for the given encounter.

    Returns:
        (patient, tasks, chat) where each value is in MongoDB Extended JSON
        format — the same dict/list shape that the extractors already handle.
    """
    db = _get_db()

    # Round-trip through json_util so $date/$oid fields are restored to the
    # dict-with-dollar-sign format that _unwrap_mongo() in date_utils.py expects.
    def _fetch(collection, query: dict) -> list:
        docs = list(collection.find(query))
        return json.loads(json_util.dumps(docs))

    patients_list = _fetch(db.patients, {"CPMRN": cpmrn, "encounters": encounter})
    if not patients_list:
        raise RuntimeError(
            f"No patient document found in MongoDB for CPMRN={cpmrn} encounter={encounter}"
        )

    tasks = _fetch(db.tasks, {"CPMRN": cpmrn, "encounters": encounter})
    chat  = _fetch(db.chat,  {"identifier": f"{cpmrn}:{encounter}"})

    logger.info(
        "  [mongo_cache] loaded from MongoDB: 1 patient, %d tasks, %d chat docs",
        len(tasks), len(chat),
    )

    return patients_list[0], tasks, chat
