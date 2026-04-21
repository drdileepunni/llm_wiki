import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ..config import get_kb, list_kbs, KB_DIR

router = APIRouter(prefix="/api/kbs", tags=["kbs"])
log = logging.getLogger("wiki.kbs")

STARTER_CLAUDE_MD = """\
# CLAUDE.md — Knowledge Base Instructions

You are the maintainer of this knowledge base. Your job is to write and maintain
all files in the `wiki/` directory. You read from `raw/` but never modify it.

## Persona
[Describe how you want the AI to approach this knowledge base.
For example: "Act as a medical intern — prioritize clinical utility and practical
decision-making."
Or: "Analyze beyond literal meaning — explore philosophical tensions and deeper
implications."]

## Core Principle: Ingest Everything
Accept ALL source types without exception. Extract whatever knowledge can be
inferred from the source and build it into the wiki. Never decline. Never ask
for permission to proceed.

## Ingest Rules
- Minimum plan: 1 source page + at least 3 entity/concept pages + log.md append
  + index.md update.
- Use [[wiki links]] for all cross-references.
- Flag contradictions with a ## Contradictions section.

## Page Structure
Frontmatter on every page:
---
title: Page Title
type: source | entity | concept | query | overview
tags: [tag1, tag2]
created: YYYY-MM-DD
updated: YYYY-MM-DD
sources: [slug]
---

Page naming:
- sources/: author-year-keyword.md
- entities/: lowercase-hyphenated.md
- concepts/: lowercase-hyphenated.md
- queries/: YYYY-MM-DD-short-slug.md
"""


@router.get("/")
def get_kbs():
    return {"kbs": list_kbs()}


class CreateKBRequest(BaseModel):
    name: str


@router.post("/")
def create_kb(req: CreateKBRequest):
    name = req.name.strip().lower().replace(" ", "-")
    if not name or name == "default":
        raise HTTPException(status_code=400, detail="Invalid KB name")

    kb_root = KB_DIR / name
    if kb_root.exists():
        raise HTTPException(status_code=409, detail=f"KB '{name}' already exists")

    for subdir in ["wiki/sources", "wiki/entities", "wiki/concepts", "wiki/queries",
                   "raw", ".cache"]:
        (kb_root / subdir).mkdir(parents=True)

    (kb_root / "CLAUDE.md").write_text(STARTER_CLAUDE_MD)
    (kb_root / "wiki" / "index.md").write_text(
        f"# Wiki Index — {name}\n\n"
        "## Sources\n(none yet)\n\n"
        "## Entities\n(none yet)\n\n"
        "## Concepts\n(none yet)\n\n"
        "## Queries\n(none yet)\n"
    )
    (kb_root / "wiki" / "log.md").write_text(f"# Log — {name}\n\n")

    log.info("Created KB: %s at %s", name, kb_root)
    return {"created": True, "name": name}


@router.get("/{name}/prompt")
def get_prompt(name: str):
    try:
        kb = get_kb(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not kb.claude_md.exists():
        raise HTTPException(status_code=404, detail="CLAUDE.md not found for this KB")
    return {"name": name, "content": kb.claude_md.read_text()}


class UpdatePromptRequest(BaseModel):
    content: str


@router.put("/{name}/prompt")
def update_prompt(name: str, req: UpdatePromptRequest):
    try:
        kb = get_kb(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    kb.claude_md.write_text(req.content)
    log.info("Updated CLAUDE.md for KB: %s", name)
    return {"saved": True, "name": name}
