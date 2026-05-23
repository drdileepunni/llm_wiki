"""
Clinical Context Rules CRUD — per-patient guardrails injected into the problem
tracker prompt when a patient's active problem list matches a condition pattern.

GET    /api/clinical-context-rules/            — list all rules
POST   /api/clinical-context-rules/            — create a rule
PUT    /api/clinical-context-rules/{rule_id}   — update a rule
DELETE /api/clinical-context-rules/{rule_id}   — delete a rule

Rules are stored in the `clinical_context_rules` MongoDB collection (global,
not KB-scoped) and fetched at runtime by problem_tracker._get_clinical_context_overrides().
"""

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.emr.db import get_db

router = APIRouter(prefix="/api/clinical-context-rules", tags=["clinical-context-rules"])
log = logging.getLogger("wiki.clinical_context_rules")

_COLL = "clinical_context_rules"


# ── Pydantic models ────────────────────────────────────────────────────────────

class ContextRuleIn(BaseModel):
    condition_pattern: str
    rule_text: str
    reference: str = ""
    enabled: bool = True


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_id(condition_pattern: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", condition_pattern.lower()).split()[:5]
    slug = "-".join(words) or "rule"
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{suffix}"


def _doc_to_rule(doc: dict) -> dict:
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_rules():
    db = get_db()
    rules = [_doc_to_rule(d) for d in db[_COLL].find().sort("condition_pattern", 1)]
    return {"rules": rules}


@router.post("/", status_code=201)
def create_rule(body: ContextRuleIn):
    if not body.condition_pattern.strip():
        raise HTTPException(status_code=400, detail="condition_pattern must not be empty")
    if not body.rule_text.strip():
        raise HTTPException(status_code=400, detail="rule_text must not be empty")

    db = get_db()
    rule_id = _make_id(body.condition_pattern)
    # Ensure uniqueness
    while db[_COLL].find_one({"_id": rule_id}):
        rule_id = f"{rule_id}-{uuid.uuid4().hex[:4]}"

    now = datetime.now(timezone.utc)
    doc = {
        "_id":               rule_id,
        "condition_pattern": body.condition_pattern.strip().lower(),
        "rule_text":         body.rule_text.strip(),
        "reference":         body.reference.strip(),
        "enabled":           body.enabled,
        "created_at":        now,
        "updated_at":        now,
    }
    db[_COLL].insert_one(doc)
    log.info("Clinical context rule created: %s", rule_id)
    return {"rule": _doc_to_rule(doc)}


@router.put("/{rule_id}")
def update_rule(rule_id: str, body: ContextRuleIn):
    if not body.condition_pattern.strip():
        raise HTTPException(status_code=400, detail="condition_pattern must not be empty")
    if not body.rule_text.strip():
        raise HTTPException(status_code=400, detail="rule_text must not be empty")

    db = get_db()
    result = db[_COLL].find_one_and_update(
        {"_id": rule_id},
        {"$set": {
            "condition_pattern": body.condition_pattern.strip().lower(),
            "rule_text":         body.rule_text.strip(),
            "reference":         body.reference.strip(),
            "enabled":           body.enabled,
            "updated_at":        datetime.now(timezone.utc),
        }},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    log.info("Clinical context rule updated: %s", rule_id)
    return {"rule": _doc_to_rule(result)}


@router.delete("/{rule_id}")
def delete_rule(rule_id: str):
    db = get_db()
    result = db[_COLL].delete_one({"_id": rule_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    log.info("Clinical context rule deleted: %s", rule_id)
    return {"deleted": rule_id}
