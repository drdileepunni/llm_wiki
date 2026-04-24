from __future__ import annotations

import re
from typing import Any

from .db import get_db

VALID_TYPES = {"med", "lab", "procedure", "diet", "blood", "vents", "comm"}


def _serialize(doc: dict) -> dict:
    """Convert ObjectId fields to strings for JSON serialisation."""
    result = {}
    for k, v in doc.items():
        if k == "_id":
            result[k] = str(v)
        elif isinstance(v, list):
            result[k] = [_serialize(i) if isinstance(i, dict) else (str(i) if hasattr(i, "__class__") and i.__class__.__name__ == "ObjectId" else i) for i in v]
        elif isinstance(v, dict):
            result[k] = _serialize(v)
        elif hasattr(v, "__class__") and v.__class__.__name__ in ("ObjectId", "datetime"):
            result[k] = str(v)
        else:
            result[k] = v
    return result


def search_orderables(
    query: str,
    order_type: str | None = None,
    patient_type: str = "adult",
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search the orderables catalog by name (case-insensitive).

    Args:
        query: Drug/test/procedure name fragment (e.g. "Ringer", "Amikacin", "CBC")
        order_type: Optional filter — one of: med, lab, procedure, diet, blood, vents, comm
        patient_type: "adult" (default) | "pediatric" | "neonatal"
        limit: Max results to return (default 10)

    Returns:
        List of matching orderables with their presets (which carry default
        dosing: quantity, unit, route, form, frequency, concentration).
    """
    if order_type and order_type not in VALID_TYPES:
        return {
            "error": f"Invalid order_type '{order_type}'. Must be one of: {sorted(VALID_TYPES)}"
        }

    db = get_db()
    mongo_filter: dict = {
        "name": {"$regex": re.escape(query), "$options": "i"},
    }
    if order_type:
        mongo_filter["type"] = order_type
    if patient_type:
        mongo_filter["patientType"] = patient_type

    cursor = db.orderables.find(mongo_filter, limit=limit)
    results = [_serialize(doc) for doc in cursor]

    return {
        "query": query,
        "order_type": order_type,
        "count": len(results),
        "orderables": results,
    }
