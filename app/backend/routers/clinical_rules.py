"""
Clinical Rules CRUD — guardrails injected into CDS Step 1 reasoning.

GET    /api/clinical-rules/            — list all rules for the active KB
POST   /api/clinical-rules/            — add a new rule
PUT    /api/clinical-rules/{rule_id}   — replace a rule by id
DELETE /api/clinical-rules/{rule_id}   — remove a rule by id

Rules are stored in kbs/{kb_name}/clinical_rules.yaml at kb.wiki_root level.
"""

import logging
import re
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb

router = APIRouter(prefix="/api/clinical-rules", tags=["clinical-rules"])
log = logging.getLogger("wiki.clinical_rules")


# ── Pydantic models ────────────────────────────────────────────────────────────

class RuleIn(BaseModel):
    enabled: bool = True
    triggers: list[str] = []
    rule: str
    id: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rules_path(kb: KBConfig):
    return kb.wiki_root / "clinical_rules.yaml"


def _load_rules(kb: KBConfig) -> list[dict]:
    path = _rules_path(kb)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, list) else []
    except Exception as exc:
        log.warning("Failed to read clinical_rules.yaml for %s: %s", kb.name, exc)
        raise HTTPException(status_code=500, detail=f"Failed to read rules file: {exc}")


def _save_rules(kb: KBConfig, rules: list[dict]) -> None:
    path = _rules_path(kb)
    try:
        with path.open("w", encoding="utf-8") as fh:
            yaml.dump(rules, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as exc:
        log.error("Failed to write clinical_rules.yaml for %s: %s", kb.name, exc)
        raise HTTPException(status_code=500, detail=f"Failed to write rules file: {exc}")


def _make_id(rule_text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", "", rule_text.lower()).split()[:5]
    slug = "-".join(words) or "rule"
    suffix = uuid.uuid4().hex[:6]
    return f"{slug}-{suffix}"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_rules(kb: KBConfig = Depends(resolve_kb)):
    return {"rules": _load_rules(kb)}


@router.post("/", status_code=201)
def create_rule(body: RuleIn, kb: KBConfig = Depends(resolve_kb)):
    if not body.rule.strip():
        raise HTTPException(status_code=400, detail="rule text must not be empty")
    rules = _load_rules(kb)
    new_id = body.id or _make_id(body.rule)
    # Ensure uniqueness
    if any(r.get("id") == new_id for r in rules):
        new_id = f"{new_id}-{uuid.uuid4().hex[:4]}"
    new_rule = {
        "id": new_id,
        "enabled": body.enabled,
        "triggers": body.triggers,
        "rule": body.rule.strip(),
    }
    rules.append(new_rule)
    _save_rules(kb, rules)
    log.info("Clinical rule created: %s in KB %s", new_id, kb.name)
    return {"rule": new_rule}


@router.put("/{rule_id}")
def update_rule(rule_id: str, body: RuleIn, kb: KBConfig = Depends(resolve_kb)):
    rules = _load_rules(kb)
    idx = next((i for i, r in enumerate(rules) if r.get("id") == rule_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    updated = {
        "id": rule_id,
        "enabled": body.enabled,
        "triggers": body.triggers,
        "rule": body.rule.strip(),
    }
    rules[idx] = updated
    _save_rules(kb, rules)
    log.info("Clinical rule updated: %s in KB %s", rule_id, kb.name)
    return {"rule": updated}


@router.delete("/{rule_id}")
def delete_rule(rule_id: str, kb: KBConfig = Depends(resolve_kb)):
    rules = _load_rules(kb)
    before = len(rules)
    rules = [r for r in rules if r.get("id") != rule_id]
    if len(rules) == before:
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    _save_rules(kb, rules)
    log.info("Clinical rule deleted: %s from KB %s", rule_id, kb.name)
    return {"deleted": rule_id}
