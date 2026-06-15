"""
GCS-backed document store — replaces MongoDB for operational pipeline data.

Design
------
Each collection maps to a GCS prefix.  Most documents are stored as individual
JSON blobs keyed by the document's natural primary key (CPMRN+encounter, _id,
admission_id, etc.).

Special cases
-------------
patient_problems  — multiple problems per patient stored as a JSON array at
                    patient_problems/{CPMRN}_{encounter}.json
snapshots         — append-only; each snapshot is a timestamped blob at
                    snapshots/{CPMRN}_{encounter}/{snapshot_at_iso}.json
note_indexes      — NOT handled here; mongo_cache.py writes directly to GCS
                    (binary FAISS data needs separate handling)
clinical_context_rules — single JSON array blob at clinical_context_rules.json
app_settings      — individual JSON blobs at app_settings/{_id}.json
pipeline_traces   — append-only; each trace is a unique-key blob

MongoDB operator support in update_one
---------------------------------------
$set, $setOnInsert, $unset, $push (with $each and $slice), $inc
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from google.cloud import storage

log = logging.getLogger(__name__)

_ARRAY_COLLECTIONS = frozenset({"patient_problems", "clinical_context_rules"})
_FIXED_BLOB_COLLECTIONS = frozenset({"clinical_context_rules", "app_settings"})


# ── JSON helpers ─────────────────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat()
    if isinstance(obj, bytes):
        raise TypeError("bytes cannot be JSON-serialised; use GCS binary blobs directly")
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def _loads(text: str) -> Any:
    return json.loads(text)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


# ── MongoDB update operator emulation ────────────────────────────────────────

def _apply_update(doc: dict, update: dict, is_insert: bool) -> dict:
    """Apply MongoDB-style update operators to a plain Python dict in-place."""
    for op, fields in update.items():
        if op == "$set":
            for k, v in fields.items():
                doc[k] = v
        elif op == "$setOnInsert":
            if is_insert:
                for k, v in fields.items():
                    doc[k] = v
        elif op == "$unset":
            for k in fields:
                doc.pop(k, None)
        elif op == "$inc":
            for k, v in fields.items():
                doc[k] = doc.get(k, 0) + v
        elif op == "$push":
            for k, v in fields.items():
                arr = doc.setdefault(k, [])
                if isinstance(v, dict) and "$each" in v:
                    arr.extend(v["$each"])
                    if "$slice" in v:
                        doc[k] = arr[v["$slice"]:]
                else:
                    arr.append(v)
    return doc


def _matches(doc: dict, filter: dict) -> bool:
    """Simple filter matching (equality + $gte/$lte/$lt/$gt/$in/$ne)."""
    for k, v in filter.items():
        if k.startswith("$"):
            continue
        actual = doc.get(k)
        if isinstance(v, dict):
            for op, operand in v.items():
                if op == "$gte":
                    if actual is None or _coerce_dt(actual) < _coerce_dt(operand):
                        return False
                elif op == "$lte":
                    if actual is None or _coerce_dt(actual) > _coerce_dt(operand):
                        return False
                elif op == "$lt":
                    if actual is None or _coerce_dt(actual) >= _coerce_dt(operand):
                        return False
                elif op == "$gt":
                    if actual is None or _coerce_dt(actual) <= _coerce_dt(operand):
                        return False
                elif op == "$in":
                    if actual not in operand:
                        return False
                elif op == "$ne":
                    if actual == operand:
                        return False
                elif op == "$exists":
                    if operand and k not in doc:
                        return False
                    if not operand and k in doc:
                        return False
                elif op == "$nin":
                    if actual in operand:
                        return False
        else:
            if actual != v:
                return False
    return True


def _coerce_dt(v) -> datetime:
    """Best-effort coerce a value to datetime for comparisons."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return v  # type: ignore[return-value]


# ── InsertResult stub ─────────────────────────────────────────────────────────

class _InsertResult:
    def __init__(self, inserted_id: str):
        self.inserted_id = inserted_id


class _UpdateResult:
    def __init__(self, upserted_id=None, modified_count=0, matched_count=0):
        self.upserted_id    = upserted_id
        self.modified_count = modified_count
        self.matched_count  = matched_count


# ── GCSCollection ─────────────────────────────────────────────────────────────

class GCSCollection:
    """
    Mimics a pymongo Collection for simple CRUD operations stored as GCS JSON blobs.

    Supports:
    - find_one(filter, projection=None, sort=None)
    - find(filter)  → list[dict]
    - insert_one(doc)
    - update_one(filter, update, upsert=False)
    - count_documents(filter)
    - distinct(field, filter=None)
    """

    def __init__(self, bucket: storage.Bucket, name: str):
        self._bucket = bucket
        self._name   = name
        self._is_array = name in _ARRAY_COLLECTIONS

    # ── blob key resolution ───────────────────────────────────────────────────

    def _primary_key(self, filter: dict) -> str | None:
        """Return a deterministic GCS blob path for a filter, or None if ambiguous."""
        n = self._name

        # Fixed single-file collections
        if n == "clinical_context_rules":
            return "clinical_context_rules.json"

        if n == "app_settings" and "_id" in filter:
            return f"app_settings/{filter['_id']}.json"

        if n == "monitoring_protocols" and "protocol_id" in filter:
            return f"monitoring_protocols/{filter['protocol_id']}.json"

        # CPMRN+encounter (most operational collections)
        if "CPMRN" in filter and "encounter" in filter:
            return f"{n}/{filter['CPMRN']}_{filter['encounter']}.json"

        # admission_id (note_indexes — rarely called through here)
        if "admission_id" in filter:
            return f"{n}/{filter['admission_id']}.json"

        # Named _id pattern
        if "_id" in filter:
            return f"{n}/{filter['_id']}.json"

        return None

    def _snapshot_key(self, doc: dict) -> str:
        ts = doc.get("snapshot_at", datetime.now(timezone.utc))
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts_str = ts.isoformat().replace(":", "-").replace("+", "p")
        else:
            ts_str = str(ts).replace(":", "-").replace("+", "p")
        return f"snapshots/{doc['CPMRN']}_{doc['encounter']}/{ts_str}.json"

    def _trace_key(self, doc: dict) -> str:
        ts = datetime.now(timezone.utc).isoformat().replace(":", "-").replace("+", "p")
        cpmrn = doc.get("CPMRN", "unknown")
        enc   = doc.get("encounter", 1)
        return f"pipeline_traces/{ts}_{cpmrn}_{enc}.json"

    # ── low-level GCS helpers ─────────────────────────────────────────────────

    def _blob_exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists()

    def _load_blob(self, key: str) -> Any | None:
        blob = self._bucket.blob(key)
        if not blob.exists():
            return None
        return _loads(blob.download_as_text(encoding="utf-8"))

    def _save_blob(self, key: str, data: Any):
        self._bucket.blob(key).upload_from_string(
            _dumps(data), content_type="application/json"
        )

    def _list_docs(self, prefix: str = None) -> list[dict]:
        """Download and return all JSON blobs under a prefix."""
        prefix = prefix or (self._name + "/")
        docs = []
        for blob in self._bucket.list_blobs(prefix=prefix):
            if not blob.name.endswith(".json"):
                continue
            try:
                data = _loads(blob.download_as_text(encoding="utf-8"))
                if isinstance(data, list):
                    docs.extend(data)
                else:
                    docs.append(data)
            except Exception:
                log.warning("gcs_store: could not parse %s", blob.name)
        return docs

    # ── public API ────────────────────────────────────────────────────────────

    def find_one(
        self,
        filter: dict,
        projection=None,
        sort: list | None = None,
    ) -> dict | None:
        """Return the first document matching filter, or None."""

        # ── snapshots: append-only, keyed by timestamp ────────────────────────
        if self._name == "snapshots":
            return self._snapshot_find_one(filter, sort)

        # ── array collections (patient_problems, clinical_context_rules) ──────
        if self._is_array:
            key = self._primary_key(filter)
            arr = (self._load_blob(key) or []) if key else self._list_docs()
            for item in arr:
                if _matches(item, filter):
                    return item
            return None

        # ── default: single doc per blob ──────────────────────────────────────
        key = self._primary_key(filter)
        if key:
            doc = self._load_blob(key)
            if doc is None or not _matches(doc, filter):
                return None
            return doc

        # Fallback: list all and filter
        for doc in self._list_docs():
            if _matches(doc, filter):
                return doc
        return None

    def _snapshot_find_one(self, filter: dict, sort=None) -> dict | None:
        cpmrn    = filter.get("CPMRN")
        encounter = filter.get("encounter", 1)
        if not cpmrn:
            return None
        prefix = f"snapshots/{cpmrn}_{encounter}/"
        blobs  = sorted(
            self._bucket.list_blobs(prefix=prefix),
            key=lambda b: b.name,
            reverse=bool(sort and sort[0][1] == -1),
        )
        for blob in blobs:
            if not blob.name.endswith(".json"):
                continue
            # Fast timestamp filter from blob name before downloading
            ts_part = blob.name.split("/")[-1].replace(".json", "")
            # Additional filter checks (snapshot_at $gte)
            snap_at_filter = filter.get("snapshot_at")
            if snap_at_filter and isinstance(snap_at_filter, dict):
                try:
                    # ISO blob names sort lexicographically = chronologically
                    blob_ts_str = ts_part.replace("-", ":").replace("p", "+", 1)
                    blob_ts = datetime.fromisoformat(blob_ts_str)
                    if blob_ts.tzinfo is None:
                        blob_ts = blob_ts.replace(tzinfo=timezone.utc)
                    gte = snap_at_filter.get("$gte")
                    if gte and _coerce_dt(blob_ts) < _coerce_dt(gte):
                        continue
                except Exception:
                    pass
            try:
                doc = _loads(blob.download_as_text(encoding="utf-8"))
                if _matches(doc, filter):
                    return doc
            except Exception:
                log.warning("gcs_store: could not read snapshot blob %s", blob.name)
        return None

    def find(self, filter: dict, projection=None) -> list[dict]:
        """Return all documents matching filter."""
        if self._name == "snapshots":
            # For count/listing use count_documents; full scan rarely needed
            cpmrn    = filter.get("CPMRN")
            encounter = filter.get("encounter", 1)
            prefix = f"snapshots/{cpmrn}_{encounter}/" if cpmrn else "snapshots/"
            docs = []
            for blob in sorted(self._bucket.list_blobs(prefix=prefix), key=lambda b: b.name):
                if not blob.name.endswith(".json"):
                    continue
                try:
                    doc = _loads(blob.download_as_text(encoding="utf-8"))
                    if _matches(doc, filter):
                        docs.append(doc)
                except Exception:
                    pass
            return docs

        if self._name == "clinical_context_rules":
            arr = self._load_blob("clinical_context_rules.json") or []
            return [item for item in arr if _matches(item, filter)]

        if self._is_array:
            key = self._primary_key(filter)
            if key:
                arr = self._load_blob(key) or []
                # Filter out CPMRN/encounter from item-level match since they're in the path
                item_filter = {k: v for k, v in filter.items() if k not in ("CPMRN", "encounter")}
                return [item for item in arr if _matches(item, item_filter)]
            return self._list_docs()

        # Standard: list all blobs in prefix, filter
        docs = self._list_docs()
        return [d for d in docs if _matches(d, filter)]

    def insert_one(self, doc: dict) -> _InsertResult:
        """Write a document to GCS. Returns InsertResult with inserted_id = blob key."""
        if self._name == "snapshots":
            key = self._snapshot_key(doc)
            self._save_blob(key, doc)
            return _InsertResult(key)

        if self._name == "pipeline_traces":
            key = self._trace_key(doc)
            self._save_blob(key, doc)
            return _InsertResult(key)

        # For array collections: load array, append, save
        if self._is_array:
            key = self._primary_key(doc) or f"{self._name}/unknown.json"
            arr = self._load_blob(key) or []
            arr.append(doc)
            self._save_blob(key, arr)
            return _InsertResult(key)

        # Default: single doc per blob
        key = self._primary_key(doc)
        if not key:
            # Use a timestamp-based fallback key
            ts = datetime.now(timezone.utc).isoformat().replace(":", "-")
            key = f"{self._name}/{ts}.json"
        self._save_blob(key, doc)
        return _InsertResult(key)

    def update_one(
        self,
        filter: dict,
        update: dict,
        upsert: bool = False,
    ) -> _UpdateResult:
        """Apply MongoDB-style update operators to a document."""
        if self._is_array:
            return self._update_array_item(filter, update, upsert)

        key = self._primary_key(filter)
        if not key:
            log.warning("gcs_store: update_one — cannot resolve key for filter %s in %s", filter, self._name)
            return _UpdateResult()

        existing = self._load_blob(key)
        is_insert = existing is None

        if is_insert and not upsert:
            return _UpdateResult(matched_count=0)

        if is_insert:
            doc = dict(filter)
            # strip operator keys (like $in) from filter before using as base
            doc = {k: v for k, v in doc.items() if not k.startswith("$") and not isinstance(v, dict)}
        else:
            doc = existing

        _apply_update(doc, update, is_insert=is_insert)
        self._save_blob(key, doc)

        return _UpdateResult(
            upserted_id=key if is_insert else None,
            modified_count=0 if is_insert else 1,
            matched_count=0 if is_insert else 1,
        )

    def _update_array_item(self, filter: dict, update: dict, upsert: bool) -> _UpdateResult:
        """Update or insert one item in a per-patient JSON array blob."""
        key = self._primary_key(filter)
        if not key:
            key = f"{self._name}/unknown.json"
        arr = self._load_blob(key) or []

        # Item-level filter strips CPMRN/encounter (those are in the blob path)
        item_filter = {k: v for k, v in filter.items() if k not in ("CPMRN", "encounter")}

        matched = None
        for item in arr:
            if _matches(item, item_filter):
                matched = item
                break

        is_insert = matched is None
        if is_insert and not upsert:
            return _UpdateResult()

        if is_insert:
            matched = {k: v for k, v in filter.items() if not k.startswith("$") and not isinstance(v, dict)}
            arr.append(matched)

        _apply_update(matched, update, is_insert=is_insert)
        self._save_blob(key, arr)
        return _UpdateResult(
            upserted_id=key if is_insert else None,
            modified_count=0 if is_insert else 1,
            matched_count=0 if is_insert else 1,
        )

    def update_many(self, filter: dict, update: dict) -> _UpdateResult:
        """Apply update to all matching documents (used in study_matcher → being replaced by BQ)."""
        count = 0
        docs = self._list_docs()
        for doc in docs:
            if _matches(doc, filter):
                _apply_update(doc, update, is_insert=False)
                key = self._primary_key(doc)
                if key:
                    self._save_blob(key, doc)
                    count += 1
        return _UpdateResult(modified_count=count)

    def count_documents(self, filter: dict, limit: int = 0) -> int:
        """Count documents matching filter. Optimised for the snapshots collection."""
        if self._name == "snapshots":
            return self._snapshot_count(filter, limit)

        docs = self.find(filter)
        n = len(docs)
        return min(n, limit) if limit else n

    def _snapshot_count(self, filter: dict, limit: int = 0) -> int:
        cpmrn    = filter.get("CPMRN")
        encounter = filter.get("encounter", 1)
        prefix   = f"snapshots/{cpmrn}_{encounter}/" if cpmrn else "snapshots/"
        snap_at  = filter.get("snapshot_at", {})
        gte      = _coerce_dt(snap_at.get("$gte")) if isinstance(snap_at, dict) else None
        lte      = _coerce_dt(snap_at.get("$lte")) if isinstance(snap_at, dict) else None

        count = 0
        for blob in self._bucket.list_blobs(prefix=prefix):
            if not blob.name.endswith(".json"):
                continue
            # Parse timestamp from blob name (fast, no download)
            try:
                ts_part = blob.name.split("/")[-1].replace(".json", "")
                # Reverse the key encoding: - back to :, p back to +
                # Format was: 2026-05-27T10-30-00p00-00 → 2026-05-27T10:30:00+00:00
                # Actually we used isoformat() with replacements, let's just try parse
                ts_str = ts_part
                # Try a few decoding attempts
                for attempt in (
                    ts_str,
                    ts_str.replace("p", "+", 1),
                    ts_str.replace("-", ":", 2).replace("p", "+", 1),
                ):
                    try:
                        dt = datetime.fromisoformat(attempt)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        break
                    except ValueError:
                        dt = None
                if dt is None:
                    count += 1
                    continue
                if gte and dt < gte:
                    continue
                if lte and dt > lte:
                    continue
                count += 1
                if limit and count >= limit:
                    return count
            except Exception:
                count += 1
        return count

    def distinct(self, field: str, filter: dict = None) -> list:
        """Return unique values of field across matching documents."""
        docs = self.find(filter or {})
        seen = set()
        result = []
        for doc in docs:
            v = doc.get(field)
            if v is not None and v not in seen:
                seen.add(v)
                result.append(v)
        return result

    def delete_one(self, filter: dict) -> _UpdateResult:
        key = self._primary_key(filter)
        if key and self._blob_exists(key):
            self._bucket.blob(key).delete()
            return _UpdateResult(modified_count=1)
        return _UpdateResult()


# ── GCSDatabase ───────────────────────────────────────────────────────────────

class GCSDatabase:
    """
    Replaces pymongo Database. Returns a GCSCollection for any collection name.
    Supports both dict-style (db["name"]) and attribute-style (db.name) access.
    """

    def __init__(self, bucket: storage.Bucket):
        self._bucket = bucket

    def __getitem__(self, name: str) -> GCSCollection:
        return GCSCollection(self._bucket, name)

    def __getattr__(self, name: str) -> GCSCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return GCSCollection(self._bucket, name)


# ── Factory ───────────────────────────────────────────────────────────────────

_db_instance: GCSDatabase | None = None


def get_gcs_db(bucket_name: str | None = None) -> GCSDatabase:
    global _db_instance
    if _db_instance is None:
        bucket_name = bucket_name or os.environ.get("GCS_BUCKET", "cds-pipeline-ops")
        client  = storage.Client()
        bucket  = client.bucket(bucket_name)
        _db_instance = GCSDatabase(bucket)
        log.info("gcs_store: connected to bucket gs://%s", bucket_name)
    return _db_instance
