"""
Clinical case assessment pipeline.

Loads pre-built snapshot cases from the data-pipeline-v2 repo, sends each
snapshot (CSV timeline + clinical context) to the wiki chat pipeline, which
vector-searches the agent_school wiki for relevant pages before answering.
"""

import json
import logging
import re
import uuid
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
    "You work strictly from the data provided — you do not infer, estimate, or complete "
    "missing values from clinical intuition or prior knowledge. "
    "If a value is not explicitly recorded in the timeline, it does not exist for this summary."
)

_SUMMARIZER_PROMPT = """\
Write a concise ICU handover brief from this patient timeline CSV.

STRICT GROUNDING RULES — read these first:
- Every numerical value (vital sign, lab result, dose) you write MUST appear explicitly \
  in the timeline below. If you cannot point to the exact row it came from, do not include it.
- A blood gas showing only pH means only pH is known. Do not infer or complete pCO2, pO2, \
  HCO3, or lactate from context or clinical pattern-matching.
- A vital sign row reporting only HR means only HR is known at that time. \
  Do not add RR, SpO2, or BP that are not in that row.
- Do not extrapolate trends using assumed start points. \
  E.g. if only one HR value exists, do not write "progressive tachycardia X→Y".
- Cite the timestamp in parentheses for every value: e.g. "pH 7.24 (08:30)".

SUMMARY RULES:
- Synthesise trends where multiple data points exist in the timeline — do not list every row.
- For medications: list drug, dose, route as recorded. Do not add inferred doses.
- Omit workflow events (chat messages, SBAR alerts) unless clinically significant.
- Target length: 200–400 words total.

Structure (use these exact headings):

**Primary Syndrome**
One or two sentences naming the dominant clinical problem and its severity.

**Key Abnormalities**
Bulleted, system-by-system. Each bullet ≤ 15 words with the value and timestamp. \
Only include values explicitly present in the timeline.

**Active Medications & Interventions**
Bulleted list. Group: antibiotics | pressors | supportive. Only what is recorded.

**Current Status**
2–3 sentences: trajectory and most urgent concern, based only on recorded data.

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
        temperature=0,
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


def _iter_snapshot_dirs(base: Path):
    """
    Yield (snap_num, snap_dir) for every *snapshot* directory under base.
    Handles both numbered (snapshot_1, snapshot_2 …) and named (dummy_snapshot).
    dummy_snapshot gets num=0 so it sorts before numbered snapshots.
    """
    dirs = sorted(base.glob("*snapshot*"))
    for snap_dir in dirs:
        if not snap_dir.is_dir():
            continue
        m = re.search(r"(\d+)", snap_dir.name)
        num = int(m.group(1)) if m else 0
        yield num, snap_dir


def list_patient_snapshot_info(patient_id: str) -> list[dict]:
    """Return available snapshot info for a patient (for the UI dropdown)."""
    base = TIMELINES_BASE / patient_id
    if not base.exists():
        return []
    result = []
    for num, snap_dir in _iter_snapshot_dirs(base):
        if (snap_dir / "snapshot.csv").exists():
            label = snap_dir.name.replace("_", " ").title()
            result.append({"num": num, "label": label, "dir": snap_dir.name})
    return result


def load_case(patient_dir: str) -> dict:
    """Read all snapshot directories and return a structured case dict."""
    base = Path(patient_dir)
    if not base.exists():
        raise FileNotFoundError(f"Patient directory not found: {patient_dir}")

    patient_id = base.name
    snapshots = []

    for snap_num, snap_dir in _iter_snapshot_dirs(base):
        csv_path = snap_dir / "snapshot.csv"
        if not csv_path.exists():
            log.warning("Missing snapshot.csv in %s — skipping", snap_dir)
            continue

        csv_content = csv_path.read_text(encoding="utf-8")
        qa_data = _load_qa_json(snap_dir)

        snapshots.append({
            "snapshot_num":        snap_num,
            "snapshot_dir":        snap_dir.name,
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


def run_clinical_assessment(patient_id: str, kb: KBConfig, model: str | None = None, snapshot_num: int | None = None, use_patient_context: bool = False, reasoning_model: str | None = None, overwrite_run_id: str | None = None) -> dict:
    """
    Run the clinical case assessment for all snapshots for patient_id.
    If overwrite_run_id is provided, that run's file is overwritten in place
    (preserving user-added ratings, comments, knowledge_gaps).
    Otherwise a new timestamped run is created.
    """
    # Scenarios have no timelines directory — detect and delegate to custom runner
    if not (TIMELINES_BASE / patient_id).exists():
        if overwrite_run_id:
            stored = load_clinical_assessment(patient_id, overwrite_run_id, kb)
            src = stored["snapshots"][0] if stored.get("snapshots") else {}
            snapshot_data = {
                "clinical_context": src.get("clinical_context", ""),
                "csv_content": src.get("csv_content", ""),
                "question": src.get("question", ""),
                "phase": src.get("phase", ""),
                "difficulty": src.get("difficulty", ""),
            }
            return run_custom_snapshot(
                snapshot_data, kb, model=model, reasoning_model=reasoning_model,
                overwrite_run_id=overwrite_run_id, overwrite_patient_id=patient_id,
            )
        raise FileNotFoundError(f"Patient directory not found: {TIMELINES_BASE / patient_id}")

    run_id = overwrite_run_id if overwrite_run_id else datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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
        result = run_chat(question=question, kb=kb, model=model, exclude_pattern=exclude_pat, mode="cds", include_patient_context=use_patient_context, reasoning_model=reasoning_model)
        if run_model is None:
            run_model = result["model"]

        snap["timeline_summary"]          = summary
        snap["agent_answer"]              = result["answer"]
        snap["immediate_next_steps"]      = result.get("immediate_next_steps", [])
        snap["clinical_direction"]        = result.get("clinical_direction", [])
        snap["immediate_actions"]         = result.get("clinical_direction", [])  # backward compat
        snap["specific_parameters"]       = result.get("specific_parameters", [])
        snap["agent_clinical_reasoning"]  = result.get("clinical_reasoning", [])
        snap["monitoring_followup"]       = result.get("monitoring_followup", [])
        snap["alternative_considerations"]= result.get("alternative_considerations", [])
        snap["pages_consulted"]           = result["pages_consulted"]
        snap["wiki_links"]                = result["wiki_links"]
        snap["tokens_in"]                 = sum_in + result["input_tokens"]
        snap["tokens_out"]                = sum_out + result["output_tokens"]
        snap["cost_usd"]                  = sum_cost + result["cost_usd"]
        snap["gap_registered"]            = result.get("gap_registered") or None
        snap["gap_entity"]                = result.get("gap_entity") or None
        snap["gap_sections"]              = result.get("gap_sections") or []

    # Restore user-authored fields when overwriting an existing run
    if overwrite_run_id:
        old_path = _clinical_assess_path(case["patient_id"], run_id, kb)
        if old_path.exists():
            try:
                old_data = json.loads(old_path.read_text(encoding="utf-8"))
                old_snaps = {s["snapshot_num"]: s for s in old_data.get("snapshots", [])}
                for snap in snapshots_to_run:
                    old = old_snaps.get(snap["snapshot_num"], {})
                    if old.get("rating") is not None:
                        snap["rating"] = old["rating"]
                    if old.get("knowledge_gaps"):
                        snap["knowledge_gaps"] = old["knowledge_gaps"]
                comment_to_keep = old_data.get("comment", "")
            except Exception:
                comment_to_keep = ""
        else:
            comment_to_keep = ""
    else:
        comment_to_keep = ""

    result = {
        "patient_id": case["patient_id"],
        "run_id":     run_id,
        "patient_dir": case["patient_dir"],
        "run_at": datetime.utcnow().isoformat(),
        "model": run_model,
        "snapshots": snapshots_to_run,
        "comment": comment_to_keep,
    }

    out_path = _clinical_assess_path(case["patient_id"], run_id, kb)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Clinical assessment saved: %s", out_path)

    return result


# ── Scenario generation ───────────────────────────────────────────────────────

_SCENARIO_GEN_SYSTEM = (
    "You are a clinical scenario generator for ICU training. "
    "You create realistic but entirely synthetic patient cases for educational use. "
    "Output valid JSON only — no prose, no markdown fences."
)

_SCENARIO_GEN_PROMPT = """\
Generate a synthetic ICU/hospital patient scenario for: "{description}"

Output ONLY a valid JSON object with these exact keys:
{{
  "clinical_context": "2-3 sentences describing the patient presentation, relevant history, and current status.",
  "question": "A specific focused clinical question about the most appropriate next action.",
  "phase": "one of exactly: EVOLVING | ESCALATION | DETERIORATION | MANAGEMENT | LATE",
  "difficulty": "one of exactly: EASY | MEDIUM | HARD",
  "csv_content": "<10-row CSV — see rules below>"
}}

csv_content rules:
- First line is the header: timestamp_ist,event_category,event_type,actor_name,actor_role,summary
- Exactly 10 data rows after the header
- event_category must be one of: LAB, VITAL, TASK, CHAT
- Timestamps start at 2024-01-15 08:00:00, span 4-8 hours in IST format
- Include realistic clinical values that match the scenario
- Rows should build a coherent clinical narrative leading to the question
- Quote any summary field that contains a comma
"""


def generate_scenario(description: str, model: str | None = None) -> dict:
    """Use an LLM to produce a synthetic clinical snapshot from a free-text description."""
    llm = get_llm_client(model=model)
    prompt = _SCENARIO_GEN_PROMPT.format(description=description)
    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_SCENARIO_GEN_SYSTEM,
        max_tokens=3000,
        force_tool=False,
        thinking_budget=0,
    )
    raw = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    # Strip markdown fences if the model wraps its output
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON: {exc}\n---\n{raw}") from exc

    for key in ("clinical_context", "question", "phase", "difficulty", "csv_content"):
        if key not in data:
            raise ValueError(f"LLM response missing key: {key!r}")

    return {
        "clinical_context": data["clinical_context"],
        "question": data["question"],
        "phase": data.get("phase", "EVOLVING").upper(),
        "difficulty": data.get("difficulty", "MEDIUM").upper(),
        "csv_content": data["csv_content"],
        "tokens_in": response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
    }


def run_custom_snapshot(
    snapshot_data: dict,
    kb: KBConfig,
    model: str | None = None,
    reasoning_model: str | None = None,
    overwrite_run_id: str | None = None,
    overwrite_patient_id: str | None = None,
) -> dict:
    """Run the clinical assessment pipeline on a user-provided custom snapshot."""
    run_id = overwrite_run_id if overwrite_run_id else datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    patient_id = overwrite_patient_id if overwrite_patient_id else f"scenario_{uuid.uuid4().hex[:8]}"

    snap: dict = {
        "snapshot_num": 1,
        "snapshot_dir": "custom",
        "csv_content": snapshot_data.get("csv_content", ""),
        "question": snapshot_data.get("question", ""),
        "phase": snapshot_data.get("phase", ""),
        "difficulty": snapshot_data.get("difficulty", ""),
        "clinical_context": snapshot_data.get("clinical_context", ""),
        "expected_next_action": "",
        "immediate_actions": [],
        "clinical_reasoning": [],
        "monitoring_followup": [],
        "alternative_considerations": [],
        "agent_answer": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }

    summary, sum_in, sum_out = _summarize_timeline(snap["csv_content"], model=model)
    sum_cost = calculate_cost(sum_in, sum_out, model or MODEL)

    question = f"{summary}\n\nQuestion: {snap['question']}"
    result = run_chat(
        question=question,
        kb=kb,
        model=model,
        mode="cds",
        reasoning_model=reasoning_model,
    )

    snap["timeline_summary"] = summary
    snap["agent_answer"] = result["answer"]
    snap["immediate_next_steps"] = result.get("immediate_next_steps", [])
    snap["clinical_direction"] = result.get("clinical_direction", [])
    snap["immediate_actions"] = result.get("clinical_direction", [])
    snap["specific_parameters"] = result.get("specific_parameters", [])
    snap["agent_clinical_reasoning"] = result.get("clinical_reasoning", [])
    snap["monitoring_followup"] = result.get("monitoring_followup", [])
    snap["alternative_considerations"] = result.get("alternative_considerations", [])
    snap["pages_consulted"] = result["pages_consulted"]
    snap["wiki_links"] = result["wiki_links"]
    snap["tokens_in"] = sum_in + result["input_tokens"]
    snap["tokens_out"] = sum_out + result["output_tokens"]
    snap["cost_usd"] = sum_cost + result["cost_usd"]
    snap["gap_registered"] = result.get("gap_registered") or None
    snap["gap_entity"] = result.get("gap_entity") or None
    snap["gap_sections"] = result.get("gap_sections") or []

    # Restore user-authored fields when overwriting
    comment_to_keep = ""
    if overwrite_run_id:
        old_path = _clinical_assess_path(patient_id, run_id, kb)
        if old_path.exists():
            try:
                old_data = json.loads(old_path.read_text(encoding="utf-8"))
                old_snap = next((s for s in old_data.get("snapshots", []) if s.get("snapshot_num") == snap["snapshot_num"]), {})
                if old_snap.get("rating") is not None:
                    snap["rating"] = old_snap["rating"]
                if old_snap.get("knowledge_gaps"):
                    snap["knowledge_gaps"] = old_snap["knowledge_gaps"]
                comment_to_keep = old_data.get("comment", "")
            except Exception:
                pass

    run_result = {
        "patient_id": patient_id,
        "run_id": run_id,
        "patient_dir": "",
        "run_at": datetime.utcnow().isoformat(),
        "model": result.get("model"),
        "snapshots": [snap],
        "comment": comment_to_keep,
    }

    out_path = _clinical_assess_path(patient_id, run_id, kb)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(run_result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Custom scenario assessment saved: %s", out_path)

    return run_result
