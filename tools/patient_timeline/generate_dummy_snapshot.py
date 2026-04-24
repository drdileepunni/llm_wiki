"""
Generate a synthetic dummy_snapshot for a given patient timeline.

Reads the real timeline CSV, asks Gemini to:
  1. Identify the primary diagnosis and key clinical features
  2. Generate a synthetic patient case with the same diagnosis but different
     enough to serve as a validation timeline
  3. Produce a q_and_a.json for a key clinical decision point in that case

Output: {timeline_dir}/dummy_snapshot/
          ├── snapshot.csv   – synthetic patient timeline
          └── q_and_a.json   – question + model answer for that snapshot

Usage:
  python -m tools.patient_timeline.generate_dummy_snapshot \\
      /path/to/SLUG_timeline_truncated.csv

  # or via the convenience wrapper:
  python -m tools.patient_timeline.generate_dummy_snapshot CPMRN/encounter
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

QUESTION_TEXT = (
    "Above is a focused window of patient events around a key clinical moment. "
    "What do you think is the most appropriate next action by a clinician?"
)

COLUMNS = [
    "timestamp_ist",
    "event_category",
    "event_type",
    "actor_name",
    "actor_role",
    "summary",
]

# ── Gemini helpers ─────────────────────────────────────────────────────────────

def _gemini_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def _call(client, prompt: str, *, max_tokens: int = 8192, thinking: bool = True, retries: int = 3) -> str:
    from google.genai import types
    config_kwargs: dict = dict(temperature=0, max_output_tokens=max_tokens)
    if not thinking:
        try:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass
    config = types.GenerateContentConfig(**config_kwargs)
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
                config=config,
            )
            return (resp.text or "").strip()
        except Exception as exc:
            if attempt < retries - 1:
                logger.warning("    [retry %d] %s", attempt + 1, exc)
                time.sleep(2)
            else:
                raise


def _call_json(client, prompt: str, *, max_tokens: int = 8192, thinking: bool = True) -> dict | list:
    """Call Gemini and parse JSON response."""
    raw = _call(client, prompt, max_tokens=max_tokens, thinking=thinking)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import re
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        raise ValueError(f"Could not parse JSON from response:\n{raw[:500]}")


# ── Step 1: Identify diagnosis from real timeline ──────────────────────────────

def _identify_case(timeline_text: str, client) -> dict:
    """Ask Gemini what this case is about."""
    prompt = f"""You are reviewing an ICU patient timeline. Identify the primary diagnosis and key clinical features.

TIMELINE:
{timeline_text}

Respond with ONLY valid JSON:
{{
  "primary_diagnosis": "e.g. Acute pancreatitis",
  "severity": "mild|moderate|severe",
  "key_features": ["feature 1", "feature 2", "feature 3"],
  "complications": ["complication 1 if any"],
  "typical_management": ["key management step 1", "key management step 2"]
}}"""

    logger.info("  Identifying diagnosis from real timeline …")
    return _call_json(client, prompt)


# ── Step 2: Generate synthetic timeline CSV ────────────────────────────────────

def _generate_synthetic_timeline(case: dict, real_timeline_text: str, client) -> str:
    """Generate a synthetic patient timeline CSV for the same diagnosis."""
    prompt = f"""You are generating a synthetic ICU patient timeline for clinical AI evaluation.

The real case is: {case['primary_diagnosis']} (severity: {case['severity']})
Key features from the real case: {', '.join(case['key_features'])}

Generate a DIFFERENT synthetic patient with the SAME primary diagnosis ({case['primary_diagnosis']}) but:
- Different demographics (age, gender)
- Different specific lab values (still abnormal in the expected direction for this disease)
- Different specific vital sign values
- Different medications/doses where clinically appropriate
- A different clinical trajectory (e.g. if the real case improved, this one may deteriorate slightly, or vice versa)
- Different comorbidities

The real timeline format for reference:
{real_timeline_text[:2000]}

Generate a synthetic timeline of 40-60 rows in the EXACT same CSV format with these columns:
timestamp_ist,event_category,event_type,actor_name,actor_role,summary

Rules:
- timestamp_ist: ISO format with IST offset (+05:30), starting from a plausible ICU admission time
- event_category: LAB | ORDER | NOTE | VITAL | TASK | CHAT
- event_type: lab_result | medication_placed | medication_given | procedure_placed | note_event | vital_signs | task_completed | chat_message
- actor_name: use generic names like "Dr. S. Mehta", "Nurse R. Patel", "System"
- actor_role: Clinician | Nurse | System (RADAR) | Physician
- summary: realistic clinical summary text matching event_type format from the real timeline

Include realistic:
- Lab results for this diagnosis (amylase, lipase, LFT, CBC, etc. as appropriate)
- Vitals every 2-4 hours showing clinical evolution
- Medication orders with doses and routes
- Nursing notes and clinical observations

Output ONLY the CSV content (no markdown, no explanation, just header + rows).
Start with: timestamp_ist,event_category,event_type,actor_name,actor_role,summary"""

    logger.info("  Generating synthetic timeline …")
    return _call(client, prompt, max_tokens=8192)


# ── Step 3: Pick a clinical decision window ────────────────────────────────────

def _pick_decision_window(synthetic_csv: str, case: dict, client) -> dict:
    """Ask Gemini to identify the best clinical decision point in the synthetic timeline."""
    prompt = f"""You are selecting ONE key clinical decision moment from this synthetic ICU timeline.

Primary diagnosis: {case['primary_diagnosis']}

SYNTHETIC TIMELINE:
{synthetic_csv}

Pick ONE window of 15-30 rows that represents the most important clinical decision point for this case.

Respond with ONLY valid JSON:
{{
  "start_row": <0-indexed row number where window starts>,
  "end_row": <0-indexed row number where window ends>,
  "phase": "<admission|evolving|deterioration|management|late>",
  "difficulty": "<easy|medium|hard>",
  "clinical_context": "One sentence describing the clinical situation at this moment",
  "expected_next_action": "The specific clinical action that should be taken"
}}"""

    logger.info("  Selecting clinical decision window …")
    return _call_json(client, prompt, thinking=False)


# ── Step 4: Generate structured answer ────────────────────────────────────────

def _generate_answer(synthetic_csv: str, window: dict, case: dict, client) -> dict:
    """Generate the model answer for the selected window."""
    rows = synthetic_csv.strip().splitlines()
    header = rows[0]
    start = window["start_row"]
    end = window["end_row"]
    snap_rows = rows[1:][start: end + 1]
    snap_text = "\n".join([header] + snap_rows)

    # Reference context: rows after end
    after_rows = rows[1:][end + 1: end + 16]
    after_text = "\n".join([header] + after_rows) if after_rows else "(end of timeline)"

    prompt = f"""You are writing a model answer for an ICU clinical decision-making evaluation.

Primary diagnosis context: {case['primary_diagnosis']}

A medical AI agent has been shown this timeline window and asked:
"{QUESTION_TEXT}"

--- PATIENT TIMELINE (what the agent sees) ---
{snap_text}
--- END ---

For reference only (do NOT reveal these in your answer):
--- SUBSEQUENT EVENTS ---
{after_text}
--- END ---

Clinical situation: {window['clinical_context']}
Expected action:   {window['expected_next_action']}
Difficulty:        {window['difficulty']}

Respond with ONLY valid JSON (no markdown, no code blocks, just raw JSON).
Keep each item to ONE concise sentence:
{{
  "immediate_actions": ["action 1 with drug/dose/route", "action 2"],
  "clinical_reasoning": ["finding 1 driving decision", "physiological rationale", "priority rationale"],
  "monitoring_followup": ["parameter + threshold + timeframe", "second parameter"],
  "alternative_considerations": ["edge case 1", "edge case 2 if applicable"]
}}

Each array item must be ONE sentence. Maximum 3 items per array. Be specific with numbers."""

    logger.info("  Generating model answer …")
    return _call_json(client, prompt, max_tokens=4096, thinking=False)


# ── Assemble and save ──────────────────────────────────────────────────────────

def generate_dummy_snapshot(timeline_csv_path: str | Path) -> Path:
    """
    Main entry point. Reads a real timeline CSV and produces:
      {timeline_dir}/dummy_snapshot/snapshot.csv
      {timeline_dir}/dummy_snapshot/q_and_a.json

    Returns the dummy_snapshot directory path.
    """
    from . import config

    csv_path = Path(timeline_csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Timeline not found: {csv_path}")

    timeline_dir = csv_path.parent
    out_dir = timeline_dir / "dummy_snapshot"
    out_dir.mkdir(exist_ok=True)

    api_key = config.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY not set — check app/.env")

    client = _gemini_client(api_key)
    real_timeline_text = csv_path.read_text(encoding="utf-8")

    logger.info("Generating dummy snapshot for: %s", csv_path.name)

    # Step 1: Identify the case
    case = _identify_case(real_timeline_text, client)
    logger.info("  Diagnosis: %s (%s)", case["primary_diagnosis"], case["severity"])

    # Step 2: Generate synthetic timeline
    synthetic_csv = _generate_synthetic_timeline(case, real_timeline_text, client)

    # Write the synthetic timeline CSV
    snap_csv_path = out_dir / "snapshot.csv"
    snap_csv_path.write_text(synthetic_csv, encoding="utf-8")
    row_count = len(synthetic_csv.strip().splitlines()) - 1  # minus header
    logger.info("  Synthetic timeline: %d rows → %s", row_count, snap_csv_path)

    # Step 3: Pick the decision window
    window = _pick_decision_window(synthetic_csv, case, client)
    # Clamp indices to actual row count
    window["start_row"] = max(0, min(window["start_row"], row_count - 2))
    window["end_row"]   = max(window["start_row"] + 1, min(window["end_row"], row_count - 1))
    logger.info(
        "  Decision window: rows %d-%d (%s, %s)",
        window["start_row"], window["end_row"], window["phase"], window["difficulty"]
    )

    # Step 4: Generate structured answer
    answer = _generate_answer(synthetic_csv, window, case, client)

    # Assemble q_and_a.json
    q_and_a = {
        "question": QUESTION_TEXT,
        "synthetic_case": {
            "primary_diagnosis": case["primary_diagnosis"],
            "severity": case["severity"],
            "note": "Synthetic patient — same diagnosis as real case, different clinical details",
        },
        "window": {
            "start_row": window["start_row"],
            "end_row": window["end_row"],
            "row_count": window["end_row"] - window["start_row"] + 1,
        },
        "phase": window["phase"],
        "difficulty": window["difficulty"],
        "clinical_context": window["clinical_context"],
        "expected_next_action": window["expected_next_action"],
        "immediate_actions": answer.get("immediate_actions", []),
        "clinical_reasoning": answer.get("clinical_reasoning", []),
        "monitoring_followup": answer.get("monitoring_followup", []),
        "alternative_considerations": answer.get("alternative_considerations", []),
    }

    qa_path = out_dir / "q_and_a.json"
    qa_path.write_text(json.dumps(q_and_a, indent=2), encoding="utf-8")
    logger.info("  q_and_a.json written → %s", qa_path)
    logger.info("Done → %s", out_dir)

    return out_dir


# ── CLI ────────────────────────────────────────────────────────────────────────

def _resolve_path(arg: str) -> Path:
    """Accept either a file path or a CPMRN/encounter slug."""
    p = Path(arg)
    if p.exists():
        return p

    # Try CPMRN/encounter form → timelines/{slug}/{slug}_timeline_truncated.csv
    from . import config
    slug = arg.replace("/", "_")
    candidate = config.REPO_ROOT / "timelines" / slug / f"{slug}_timeline_truncated.csv"
    if candidate.exists():
        return candidate

    raise FileNotFoundError(
        f"Cannot find timeline for {arg!r}.\n"
        f"Tried: {p} and {candidate}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a synthetic dummy_snapshot for a given patient timeline."
    )
    parser.add_argument(
        "timeline",
        help="Path to *_timeline_truncated.csv or CPMRN/encounter (e.g. INGJNADAAS1193/1)",
    )
    args = parser.parse_args()

    try:
        csv_path = _resolve_path(args.timeline)
        out = generate_dummy_snapshot(csv_path)
        print(f"✓ dummy_snapshot → {out}")
    except Exception as exc:
        logger.error("✗ %s", exc)
        sys.exit(1)
