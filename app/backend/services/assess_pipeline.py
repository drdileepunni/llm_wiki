import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from ..config import MODEL, KBConfig, _default_kb
from .llm_client import get_llm_client
from .token_tracker import log_call
from .chat_pipeline import run_chat

log = logging.getLogger("wiki.assess_pipeline")

_GENERATE_SYSTEM = """\
You are writing medical education exam questions for a knowledge base wiki.
You will be given an ingest summary and a list of knowledge gaps (KGs) — these tell you WHAT TOPICS the wiki now covers.
Respond with ONLY a valid JSON array — no markdown fences, no explanation, just the JSON.

Generate exactly 10 question objects. Keep each field CONCISE:
  id        — integer 1-10
  question  — the question text (see style rules below)
  linked_kgs — array of gap file paths this question probes (e.g. ["wiki/gaps/aki.md"]). Empty array if none.
  rationale — ≤15 words on why this topic matters clinically

QUESTION STYLE — write like a medical school OSCE or shelf exam:
- Questions must be GENERAL medical knowledge, NOT about the specific patient in the case
- Use fictional vignette format where helpful: "A 65-year-old man in the ICU develops..."
- Do NOT mention Patient A, the timeline, or any patient-specific details
- The case is only used to identify WHICH TOPICS to test — the questions themselves are general
- Mix: 3 vignette-based clinical reasoning + 4 direct knowledge ("What are the indications for X?", "How is Y managed?") + 3 "what would you expect / what is missing" questions
- At least 6 questions must reference one or more linked_kgs (topics with gaps the wiki can't yet answer well)
- Questions linked to KGs should be harder — the wiki likely can't answer them yet until gaps are resolved
- Questions with no linked_kgs should be answerable from existing wiki pages
- Do not ask yes/no questions; do not repeat the same topic twice
- Keep question text ≤35 words
"""


def _assessment_path(source_slug: str, kb: KBConfig) -> Path:
    return kb.wiki_root / "assessments" / f"{source_slug}.json"


def load_assessment(source_slug: str, kb: KBConfig) -> dict:
    p = _assessment_path(source_slug, kb)
    if not p.exists():
        raise FileNotFoundError(f"No assessment found for {source_slug!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_assessments(kb: KBConfig) -> list[dict]:
    assess_dir = kb.wiki_root / "assessments"
    if not assess_dir.exists():
        return []
    results = []
    for f in sorted(assess_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # Compute summary stats for list view
            qs = data.get("questions", [])
            latest_new_kgs = sum(
                len(q["runs"][-1]["new_kgs_registered"])
                for q in qs
                if q.get("runs")
            )
            results.append({
                "source_slug":     data["source_slug"],
                "title":           data["title"],
                "status":          data["status"],
                "created":         data["created"],
                "last_run":        data.get("last_run"),
                "question_count":  len(qs),
                "latest_new_kgs":  latest_new_kgs,
            })
        except Exception as exc:
            log.warning("Skipping corrupt assessment %s: %s", f.name, exc)
    return results


def generate_assessment(
    source_slug: str,
    source_name: str,
    ingest_summary: str,
    knowledge_gaps: list[dict],
    files_written: list[str],
    kb: KBConfig | None = None,
) -> dict:
    if kb is None:
        kb = _default_kb()

    log.info("=== ASSESS GENERATE  slug=%r  kb=%s ===", source_slug, kb.name)

    # Build context for question generation
    kg_lines = "\n".join(
        f"  - {g['page']}  →  missing: {', '.join(g.get('missing_sections', []))}"
        for g in knowledge_gaps
    ) or "  (none identified)"

    entity_lines = "\n".join(
        f"  - {p}" for p in files_written
        if "/entities/" in p or "/concepts/" in p or "/sources/" in p
    ) or "  (none)"

    user_msg = f"""\
Case summary: {ingest_summary}

Knowledge Gaps identified during ingest ({len(knowledge_gaps)} gaps):
{kg_lines}

Wiki pages created/updated during ingest:
{entity_lines}

Generate 10 assessment questions for this case."""

    llm  = get_llm_client()
    # No tool — ask for raw JSON text to avoid Gemini MALFORMED_FUNCTION_CALL issues
    resp = llm.create_message(
        messages=[{"role": "user", "content": user_msg}],
        tools=[],
        system=_GENERATE_SYSTEM,
        max_tokens=8000,
        force_tool=False,
    )

    log.info("generate_questions response: stop=%s  in=%d  out=%d",
             resp.stop_reason, resp.usage.input_tokens, resp.usage.output_tokens)

    raw_text = next((b.text for b in resp.content if b.type == "text" and b.text), "")
    log.debug("Raw LLM output (first 200): %r", raw_text[:200])

    # Strip optional markdown fences, then parse
    clean = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    clean = re.sub(r"\s*```$", "", clean)
    try:
        raw_questions = json.loads(clean)
        if not isinstance(raw_questions, list):
            raise ValueError(f"Expected JSON array, got {type(raw_questions)}")
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("Failed to parse JSON from LLM text: %s\nRaw: %r", exc, raw_text[:400])
        raw_questions = []

    questions = [
        {
            "id":         q["id"],
            "question":   q["question"],
            "linked_kgs": q.get("linked_kgs", []),
            "rationale":  q.get("rationale", ""),
            "runs":       [],
        }
        for q in raw_questions
    ]

    assessment = {
        "title":        f"Assessment — {source_name}",
        "source_slug":  source_slug,
        "created":      date.today().isoformat(),
        "status":       "pending",
        "last_run":     None,
        "questions":    questions,
    }

    assess_dir = kb.wiki_root / "assessments"
    assess_dir.mkdir(parents=True, exist_ok=True)
    _assessment_path(source_slug, kb).write_text(
        json.dumps(assessment, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log_call(
        operation="assess_generate",
        source_name=source_name[:80],
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        model=MODEL,
        kb_name=kb.name,
    )

    if not questions:
        log.warning("Assessment written with 0 questions — LLM output may have been empty or unparseable")
    log.info("Assessment written: %s  (%d questions)", source_slug, len(questions))
    return assessment


def run_assessment(source_slug: str, kb: KBConfig | None = None) -> dict:
    if kb is None:
        kb = _default_kb()

    log.info("=== ASSESS RUN  slug=%r  kb=%s ===", source_slug, kb.name)

    assessment = load_assessment(source_slug, kb)
    assessment["status"] = "in_progress"
    assessment["last_run"] = datetime.utcnow().isoformat()

    for q in assessment["questions"]:
        log.info("Running Q%d: %r", q["id"], q["question"][:80])
        try:
            result = run_chat(q["question"], kb)
            new_kgs = [result["gap_registered"]] if result.get("gap_registered") else []
            run_record = {
                "timestamp":        datetime.utcnow().isoformat(),
                "answer":           result.get("answer", ""),
                "new_kgs_registered": new_kgs,
                "user_rating":      None,
                "input_tokens":     result.get("input_tokens", 0),
                "output_tokens":    result.get("output_tokens", 0),
                "cost_usd":         result.get("cost_usd", 0.0),
            }
        except Exception as exc:
            log.error("Q%d failed: %s", q["id"], exc)
            run_record = {
                "timestamp":        datetime.utcnow().isoformat(),
                "answer":           f"[Error: {exc}]",
                "new_kgs_registered": [],
                "user_rating":      None,
                "input_tokens":     0,
                "output_tokens":    0,
                "cost_usd":         0.0,
            }

        q["runs"].append(run_record)

    # Recompute status: passing only if all latest runs have no new KGs
    all_passing = all(
        len(q["runs"][-1]["new_kgs_registered"]) == 0
        for q in assessment["questions"]
        if q["runs"]
    )
    assessment["status"] = "passing" if all_passing else "in_progress"

    _assessment_path(source_slug, kb).write_text(
        json.dumps(assessment, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info("=== ASSESS RUN END  slug=%r  status=%s ===", source_slug, assessment["status"])
    return assessment


def rate_question(
    source_slug: str,
    question_id: int,
    rating: bool | None,
    kb: KBConfig | None = None,
) -> dict:
    if kb is None:
        kb = _default_kb()

    assessment = load_assessment(source_slug, kb)
    for q in assessment["questions"]:
        if q["id"] == question_id:
            if not q["runs"]:
                raise ValueError(f"Question {question_id} has no runs yet")
            q["runs"][-1]["user_rating"] = rating
            break
    else:
        raise ValueError(f"Question {question_id} not found in assessment {source_slug!r}")

    _assessment_path(source_slug, kb).write_text(
        json.dumps(assessment, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return assessment
