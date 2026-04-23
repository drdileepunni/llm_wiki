"""
Clinical case assessment pipeline.

Loads pre-built snapshot cases from the data-pipeline-v2 repo, sends each
snapshot (CSV timeline + clinical context) to the wiki chat pipeline, which
vector-searches the agent_school wiki for relevant pages before answering.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from ..config import MODEL, KBConfig

TIMELINES_BASE = Path("/Users/drdileepunni/github_/llm_wiki/timelines")
from .llm_client import get_llm_client
from .token_tracker import log_call, calculate_cost
from .chat_pipeline import run_chat

log = logging.getLogger("wiki.clinical_assess")

_SUMMARIZER_SYSTEM = (
    "You are a senior ICU consultant writing a concise handover brief. "
    "Think like an expert clinician: synthesise, don't transcribe. "
    "Your job is to give the receiving physician the clinical picture in as few words as possible "
    "while preserving every clinically actionable fact."
)

_SUMMARIZER_PROMPT = """\
Write a concise ICU handover brief from this patient timeline CSV. \
Think through the data carefully, identify the dominant clinical syndrome, \
and present only what matters for clinical decision-making.

Rules:
- Synthesise trends, don't list every row. E.g. "progressive tachycardia 140→150 bpm over 45 min" \
  not six separate HR entries.
- For labs: group by system, report the most recent and most abnormal value with date. \
  Skip tests that are normal unless they change the picture.
- For medications: list drug, dose, route. Group antibiotics, pressors, supportives.
- Omit workflow events (chat messages, SBAR alerts, admission triggers) unless clinically significant.
- Be precise with numbers. Never omit a critical value (lactate, pH, Hb, pressors).
- Target length: 200–400 words total.

Structure (use these exact headings):

**Primary Syndrome**
One or two sentences naming the dominant clinical problem and its severity.

**Key Abnormalities**
Bulleted, system-by-system. Each bullet ≤ 15 words with the defining value(s) and date.

**Active Medications & Interventions**
Bulleted list. Group: antibiotics | pressors | supportive.

**Current Status**
2–3 sentences: haemodynamics, trajectory, most urgent concern.

PATIENT TIMELINE (CSV):
{csv_text}"""


def _summarize_timeline(csv_text: str, model: str | None = None) -> tuple[str, int, int]:
    """
    Summarise a patient timeline CSV into a clinical brief.
    Returns (summary_text, input_tokens, output_tokens).
    """
    llm = get_llm_client(model=model)
    prompt = _SUMMARIZER_PROMPT.format(csv_text=csv_text)
    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_SUMMARIZER_SYSTEM,
        max_tokens=4000,
        force_tool=False,
        thinking_budget=1024,
    )
    summary = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    return summary, response.usage.input_tokens, response.usage.output_tokens



def _load_qa_json(snap_dir: Path) -> dict:
    """Load and parse q_and_a.json from snapshot directory."""
    qa_path = snap_dir / "q_and_a.json"
    if not qa_path.exists():
        return {}

    try:
        data = json.loads(qa_path.read_text(encoding="utf-8"))
        return {
            "question": data.get("question", ""),
            "phase": data.get("phase", ""),
            "difficulty": data.get("difficulty", ""),
            "clinical_context": data.get("clinical_context", ""),
            "expected_next_action": data.get("expected_next_action", ""),
            "immediate_actions": data.get("immediate_actions", []),
            "clinical_reasoning": data.get("clinical_reasoning", []),
            "monitoring_followup": data.get("monitoring_followup", []),
            "alternative_considerations": data.get("alternative_considerations", []),
        }
    except (json.JSONDecodeError, IOError) as e:
        log.warning("Error parsing q_and_a.json in %s: %s", snap_dir, e)
        return {}


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

        csv_path = snap_dir / "snapshot.csv"
        if not csv_path.exists():
            log.warning("Missing snapshot.csv in %s — skipping", snap_dir)
            continue

        csv_content = csv_path.read_text(encoding="utf-8")
        qa_data = _load_qa_json(snap_dir)

        snapshots.append({
            "snapshot_num":        snap_num,
            "csv_content":         csv_content,
            "question":            qa_data.get("question", ""),
            "phase":               qa_data.get("phase", ""),
            "difficulty":          qa_data.get("difficulty", ""),
            "clinical_context":    qa_data.get("clinical_context", ""),
            "expected_next_action": qa_data.get("expected_next_action", ""),
            "immediate_actions":   qa_data.get("immediate_actions", []),
            "clinical_reasoning":  qa_data.get("clinical_reasoning", []),
            "monitoring_followup": qa_data.get("monitoring_followup", []),
            "alternative_considerations": qa_data.get("alternative_considerations", []),
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


def delete_clinical_assessment(patient_id: str, run_id: str, kb: KBConfig) -> None:
    p = _clinical_assess_path(patient_id, run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No assessment {run_id!r} for {patient_id!r}")
    p.unlink()


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


def save_run_comment(patient_id: str, run_id: str, comment: str, kb: KBConfig) -> dict:
    p = _clinical_assess_path(patient_id, run_id, kb)
    if not p.exists():
        raise FileNotFoundError(f"No assessment {run_id!r} for {patient_id!r}")
    data = json.loads(p.read_text(encoding="utf-8"))
    data["comment"] = comment
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
                snapshots = data.get("snapshots", [])
                ratings = [s["rating"] for s in snapshots if s.get("rating") is not None]
                avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None
                results.append({
                    "patient_id":     data["patient_id"],
                    "run_id":         data["run_id"],
                    "run_at":         data.get("run_at"),
                    "snapshot_count": len(snapshots),
                    "model":          data.get("model"),
                    "avg_rating":     avg_rating,
                    "comment":        data.get("comment", ""),
                })
            except Exception as exc:
                log.warning("Skipping corrupt clinical assessment %s: %s", f.name, exc)
    results.sort(key=lambda r: r.get("run_at") or "", reverse=True)
    return results


def run_clinical_assessment(patient_id: str, kb: KBConfig, model: str | None = None, snapshot_num: int | None = None, use_patient_context: bool = False) -> dict:
    """
    Run the clinical case assessment for all snapshots for patient_id.
    Each call creates a new timestamped run under assessments/clinical/{patient_id}/.
    Patient-specific wiki pages are excluded from vector search so the agent
    uses only general clinical knowledge from the wiki.
    """
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    patient_dir = str(TIMELINES_BASE / patient_id)
    case = load_case(patient_dir)
    run_model = None

    # Strip trailing _N suffix so "INTSNLG2851387_1" → "intsnlg2851387"
    # matching page names like "intsnlg2851387_2026_04_04.md"
    exclude_pat = re.sub(r'_\d+$', '', patient_id).lower()

    snapshots_to_run = (
        [s for s in case["snapshots"] if s["snapshot_num"] == snapshot_num]
        if snapshot_num is not None else case["snapshots"]
    )

    for snap in snapshots_to_run:
        log.info(
            "Clinical assess: patient=%s snapshot=%d run=%s model=%s",
            case["patient_id"], snap["snapshot_num"], run_id, model or "default",
        )

        # ── Step 1: Summarise raw timeline into a clinical brief ──────────────
        log.info("  Summarising timeline (%d chars)…", len(snap["csv_content"]))
        summary, sum_in, sum_out = _summarize_timeline(snap["csv_content"], model=model)
        sum_cost = calculate_cost(sum_in, sum_out, model or MODEL)
        log.info("  Summary: %d chars  in=%d out=%d cost=$%.4f", len(summary), sum_in, sum_out, sum_cost)

        # ── Step 2: Pass summary + question to wiki chat (CDS mode) ──────────
        question = f"{summary}\n\nQuestion: {snap['question']}"
        result = run_chat(question=question, kb=kb, model=model, exclude_pattern=exclude_pat, mode="cds", include_patient_context=use_patient_context)
        if run_model is None:
            run_model = result["model"]

        snap["timeline_summary"]          = summary
        snap["agent_answer"]              = result["answer"]
        snap["immediate_actions"]         = result.get("immediate_actions", [])
        snap["agent_clinical_reasoning"]  = result.get("clinical_reasoning", [])
        snap["monitoring_followup"]       = result.get("monitoring_followup", [])
        snap["alternative_considerations"]= result.get("alternative_considerations", [])
        snap["pages_consulted"]           = result["pages_consulted"]
        snap["wiki_links"]                = result["wiki_links"]
        snap["tokens_in"]                 = sum_in + result["input_tokens"]
        snap["tokens_out"]                = sum_out + result["output_tokens"]
        snap["cost_usd"]                  = sum_cost + result["cost_usd"]

    result = {
        "patient_id": case["patient_id"],
        "run_id":     run_id,
        "patient_dir": case["patient_dir"],
        "run_at": datetime.utcnow().isoformat(),
        "model": run_model,
        "snapshots": snapshots_to_run,
    }

    out_path = _clinical_assess_path(case["patient_id"], run_id, kb)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Clinical assessment saved: %s", out_path)

    return result
