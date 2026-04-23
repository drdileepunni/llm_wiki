"""
Clinical case assessment pipeline.

Loads pre-built snapshot cases from the data-pipeline-v2 repo, sends each
snapshot (CSV timeline + clinical context) to the LLM with a fixed clinical
reasoning question, and stores agent answers for side-by-side human review.

No KG registration, no wiki RAG — pure LLM clinical reasoning test.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from ..config import MODEL, KBConfig

TIMELINES_BASE = Path("/Users/drdileepunni/github_/llm_wiki/timelines")
from .llm_client import get_llm_client
from .token_tracker import calculate_cost, log_call

log = logging.getLogger("wiki.clinical_assess")

_SYSTEM_PROMPT = """\
You are an experienced ICU physician reviewing a patient event timeline.
Read the timeline carefully and answer the clinical question with:
1. Your recommended immediate action (1-2 sentences)
2. Your clinical reasoning (3-5 sentences explaining the key findings driving your decision)

Be concise and specific. Reference the actual values and events from the timeline."""


def _parse_answer_txt(text: str) -> dict:
    """Extract structured fields from answer.txt."""
    def _extract(label: str, next_labels: list[str]) -> str:
        pattern = rf"{re.escape(label)}\s*\n(.*?)(?=\n(?:{'|'.join(re.escape(l) for l in next_labels)})|$)"
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    phase = ""
    difficulty = ""
    header_m = re.match(r"PHASE:\s*(\w+)\s*\|\s*DIFFICULTY:\s*(\w+)", text)
    if header_m:
        phase = header_m.group(1)
        difficulty = header_m.group(2)

    all_labels = [
        "CLINICAL CONTEXT:", "EXPECTED NEXT ACTION:", "IMMEDIATE ACTION", "CLINICAL REASONING"
    ]

    clinical_context   = _extract("CLINICAL CONTEXT:",    ["EXPECTED NEXT ACTION:", "IMMEDIATE ACTION", "CLINICAL REASONING"])
    expected_action    = _extract("EXPECTED NEXT ACTION:", ["IMMEDIATE ACTION", "CLINICAL REASONING"])
    immediate_action   = _extract("IMMEDIATE ACTION",      ["CLINICAL REASONING"])
    clinical_reasoning = _extract("CLINICAL REASONING",   [])

    return {
        "phase": phase,
        "difficulty": difficulty,
        "clinical_context": clinical_context,
        "expected_next_action": expected_action,
        "immediate_action": immediate_action,
        "clinical_reasoning": clinical_reasoning,
    }


def load_case(patient_dir: str) -> dict:
    """Read all snapshot_N/ subdirectories and return a structured case dict."""
    base = Path(patient_dir)
    if not base.exists():
        raise FileNotFoundError(f"Patient directory not found: {patient_dir}")

    patient_id = base.name
    snapshots = []

    for snap_dir in sorted(base.glob("snapshot_*")):
        if not snap_dir.is_dir():
            continue

        num_m = re.search(r"snapshot_(\d+)", snap_dir.name)
        snap_num = int(num_m.group(1)) if num_m else len(snapshots) + 1

        csv_path      = snap_dir / "snapshot.csv"
        question_path = snap_dir / "question.txt"
        answer_path   = snap_dir / "answer.txt"

        if not csv_path.exists():
            log.warning("Missing snapshot.csv in %s — skipping", snap_dir)
            continue

        csv_content  = csv_path.read_text(encoding="utf-8")
        question     = question_path.read_text(encoding="utf-8").strip() if question_path.exists() else ""
        answer_raw   = answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
        parsed       = _parse_answer_txt(answer_raw)

        snapshots.append({
            "snapshot_num":        snap_num,
            "csv_content":         csv_content,
            "question":            question,
            **parsed,
            # agent_answer filled in during run
            "agent_answer":        None,
            "tokens_in":           0,
            "tokens_out":          0,
            "cost_usd":            0.0,
        })

    if not snapshots:
        raise ValueError(f"No snapshot_N/ directories found in {patient_dir}")

    return {"patient_id": patient_id, "patient_dir": str(base), "snapshots": snapshots}


def list_available_patients() -> list[str]:
    """Return sorted list of patient slugs (subdirectory names) in TIMELINES_BASE."""
    if not TIMELINES_BASE.exists():
        return []
    return sorted(p.name for p in TIMELINES_BASE.iterdir() if p.is_dir())


def _clinical_assess_dir(patient_id: str, kb: KBConfig) -> Path:
    return kb.wiki_root / "assessments" / "clinical" / patient_id


def _clinical_assess_path(patient_id: str, run_id: str, kb: KBConfig) -> Path:
    return _clinical_assess_dir(patient_id, kb) / f"{run_id}.json"


def load_clinical_assessment(patient_id: str, run_id: str, kb: KBConfig) -> dict:
    p = _clinical_assess_path(patient_id, run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No assessment {run_id!r} for {patient_id!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_snapshot_rating(
    patient_id: str,
    run_id: str,
    snapshot_num: int,
    rating: int | None,
    kb: KBConfig,
    knowledge_gaps: list | None = None,
) -> dict:
    """Persist rating and/or knowledge_gaps for a single snapshot."""
    p = _clinical_assess_path(patient_id, run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No assessment {run_id!r} for {patient_id!r}")
    data = json.loads(p.read_text(encoding="utf-8"))
    for snap in data.get("snapshots", []):
        if snap["snapshot_num"] == snapshot_num:
            if rating is not None:
                snap["rating"] = rating
            if knowledge_gaps is not None:
                snap["knowledge_gaps"] = knowledge_gaps
            break
    else:
        raise ValueError(f"Snapshot {snapshot_num} not found for {patient_id!r}")
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data


def list_clinical_assessments(kb: KBConfig) -> list[dict]:
    """Return all runs across all patients, newest first."""
    clinical_dir = kb.wiki_root / "assessments" / "clinical"
    if not clinical_dir.exists():
        return []
    results = []
    for patient_dir in sorted(clinical_dir.iterdir()):
        if not patient_dir.is_dir():
            continue
        for f in sorted(patient_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append({
                    "patient_id":     data["patient_id"],
                    "run_id":         data["run_id"],
                    "run_at":         data.get("run_at"),
                    "snapshot_count": len(data.get("snapshots", [])),
                })
            except Exception as exc:
                log.warning("Skipping corrupt clinical assessment %s: %s", f.name, exc)
    results.sort(key=lambda r: r.get("run_at") or "", reverse=True)
    return results


def run_clinical_assessment(patient_id: str, kb: KBConfig) -> dict:
    """
    Run the clinical case assessment for all snapshots for patient_id.
    Each call creates a new timestamped run under assessments/clinical/{patient_id}/.
    """
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    patient_dir = str(TIMELINES_BASE / patient_id)
    case = load_case(patient_dir)
    client = get_llm_client()

    for snap in case["snapshots"]:
        user_message = (
            f"{snap['csv_content']}\n\n"
            f"CLINICAL CONTEXT:\n{snap['clinical_context']}\n\n"
            f"Question: {snap['question']}"
        )

        log.info(
            "Clinical assess: patient=%s snapshot=%d run=%s",
            case["patient_id"], snap["snapshot_num"], run_id,
        )

        response = client.create_message(
            messages=[{"role": "user", "content": user_message}],
            tools=[],
            system=_SYSTEM_PROMPT,
            max_tokens=1000,
            force_tool=False,
        )

        answer_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                answer_text += block.text

        tokens_in  = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost       = calculate_cost(tokens_in, tokens_out, MODEL)

        log_call(
            operation="clinical_assess",
            source_name=f"{case['patient_id']}/snapshot_{snap['snapshot_num']}",
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            model=MODEL,
            kb_name=kb.name,
        )

        snap["agent_answer"] = answer_text
        snap["tokens_in"]    = tokens_in
        snap["tokens_out"]   = tokens_out
        snap["cost_usd"]     = cost

    result = {
        "patient_id": case["patient_id"],
        "run_id":     run_id,
        "patient_dir": case["patient_dir"],
        "run_at": datetime.utcnow().isoformat(),
        "snapshots": case["snapshots"],
    }

    out_path = _clinical_assess_path(case["patient_id"], run_id, kb)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Clinical assessment saved: %s", out_path)

    return result
