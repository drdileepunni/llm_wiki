"""
Persist proposed medication-reconciliation action sets in GCS.

A Google Chat card button can't carry full order payloads, so when we send a
reconciliation card we store the resolved action set here keyed by a uuid, and
the card carries only that id. On Submit, the webhook loads the set and filters
to the action keys the clinician ticked.

Stored via the GCS document store under the `proposed_order_actions/` prefix
(keyed by `_id`, see gcs_store._primary_key).

Schema of a stored action set:
    {
      "_id": "<uuid>",
      "cpmrn": "INKLERN261338",
      "encounter": 1,
      "actions": {
        "edit:lYqfwiYN": {"kind": "edit", "order_no": "lYqfwiYN...", "changes": {...}, "label": "..."},
        "discontinue:90Ims": {"kind": "discontinue", "order_no": "...", "label": "..."},
        "new:0": {"kind": "new", "order": {...}, "label": "..."},
        ...
      }
    }
"""
from __future__ import annotations

from typing import Any

_COLLECTION = "proposed_order_actions"


def _db():
    from app.backend.services.gcs_store import get_gcs_db
    return get_gcs_db()


def save_action_set(action_set_id: str, cpmrn: str, encounter: int, actions: dict[str, dict]) -> None:
    """Persist a proposed action set keyed by action_set_id."""
    _db()[_COLLECTION].insert_one({
        "_id": action_set_id,
        "cpmrn": cpmrn,
        "encounter": encounter,
        "actions": actions,
    })


def load_action_set(action_set_id: str) -> dict[str, Any] | None:
    """Load a stored action set, or None if not found."""
    return _db()[_COLLECTION].find_one({"_id": action_set_id})


def select_actions(action_set: dict, selected_keys: list[str]) -> list[dict]:
    """
    Given a loaded action set and the action keys the clinician ticked, return
    the ordered list of action dicts to pass to apply_order_actions().
    Unknown keys are ignored.
    """
    actions = action_set.get("actions", {}) or {}
    return [actions[k] for k in selected_keys if k in actions]
