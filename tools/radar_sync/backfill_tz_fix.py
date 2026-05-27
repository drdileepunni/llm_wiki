"""
One-time backfill: subtract 5h30m from timestamps in study_task_import and
study_sbar_import that were stored as IST-treated-as-UTC.

Root cause: BigQuery returns IST wall-clock datetimes; the syncer stamped them
as UTC instead of IST, making every timestamp +5:30 ahead of the correct UTC.

This script patches:
  study_task_import  : task_visible_at, window_expires_at
  study_sbar_import  : create_date_time, window_expires_at

Run once from the repo root:
  MONGO_URI=... MONGO_DB_NAME=... python -m tools.radar_sync.backfill_tz_fix

Dry-run by default (set DRY_RUN=0 to apply).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient, UpdateOne

_SHIFT = timedelta(hours=5, minutes=30)
_DRY_RUN = os.getenv("DRY_RUN", "1") != "0"


def _get_db():
    uri     = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    db_name = os.getenv("MONGO_DB_NAME", "emr-local")
    return MongoClient(uri)[db_name]


def _fix_collection(db, col_name: str, fields: list[str]) -> dict:
    col   = db[col_name]
    total = col.count_documents({})
    ops: list[UpdateOne] = []

    for doc in col.find({}, {f: 1 for f in fields}):
        update = {}
        for field in fields:
            val = doc.get(field)
            if not isinstance(val, datetime):
                continue
            # Only fix timestamps that appear to be IST-stored-as-UTC:
            # correct UTC for any recent Indian ICU event should be < UTC+00:00 noon,
            # but after the bad stamping they end up with hours shifted +5:30.
            # We fix ALL datetimes unconditionally — this script is safe to run once
            # because all existing records were written by the buggy syncer.
            if val.tzinfo is None:
                val = val.replace(tzinfo=timezone.utc)
            corrected = val - _SHIFT
            update[field] = corrected

        if update:
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": update}))

    modified = 0
    if ops and not _DRY_RUN:
        result = col.bulk_write(ops, ordered=False)
        modified = result.modified_count

    return {"collection": col_name, "total_docs": total, "ops_built": len(ops), "modified": modified}


def main():
    db = _get_db()

    print(f"{'DRY RUN — ' if _DRY_RUN else ''}Connecting to {os.getenv('MONGO_DB_NAME', 'emr-local')}")
    print(f"Shift to subtract: {_SHIFT}\n")

    results = [
        _fix_collection(db, "study_task_import", ["task_visible_at", "window_expires_at"]),
        _fix_collection(db, "study_sbar_import",  ["create_date_time", "window_expires_at"]),
    ]

    for r in results:
        if _DRY_RUN:
            print(f"  {r['collection']}: {r['total_docs']} docs, {r['ops_built']} would be updated")
        else:
            print(f"  {r['collection']}: {r['total_docs']} docs, {r['modified']} updated")

    if _DRY_RUN:
        print("\nDry run complete. Re-run with DRY_RUN=0 to apply.")
    else:
        print("\nBackfill complete.")


if __name__ == "__main__":
    main()
