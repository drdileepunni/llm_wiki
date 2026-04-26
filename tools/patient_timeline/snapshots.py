"""
Step 3 – Generate five clinical decision-making evaluation snapshots.

Event-window design with Google structured output:

  Phase A – ask_gemini(df, api_key)
      › One Gemini call: identify 5 event windows (start_row, end_row, phase, etc.)
      › Five Gemini calls: generate structured answers per event window.
      Returns a list of 5 snapshot dicts with start_row, end_row, and structured answer.

  Phase B – save_snapshots(df, snapshots, patient_dir)
      › Pure file I/O, no API calls.
      › Writes snapshot_1/ … snapshot_5/, each with:
          snapshot.csv   – timeline rows start_row … end_row (the event window)
          q_and_a.json   – single JSON file with question, metadata, and structured answer
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

QUESTION_TEXT = (
    "Above is a focused window of patient events around a key clinical moment. "
    "What do you think is the most appropriate next action by a clinician?"
)


# ── Gemini client ──────────────────────────────────────────────────────────────

def _gemini_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def _call(
    client,
    prompt: str,
    *,
    max_tokens: int = 8192,
    thinking: bool = True,
    retries: int = 3,
) -> str:
    """Call Gemini and return text response."""
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


# ── Compact timeline representation ────────────────────────────────────────────

def _compact(df: pd.DataFrame, max_summary: int = 90) -> str:
    rows = ["row|time|category|event_type|summary"]
    for i, r in df.iterrows():
        ts  = str(r.get("timestamp_ist", ""))[:16]
        cat = str(r.get("event_category", ""))
        et  = str(r.get("event_type", ""))
        s   = str(r.get("summary", ""))[:max_summary].replace("|", "/").replace("\n", " ")
        rows.append(f"{i}|{ts}|{cat}|{et}|{s}")
    return "\n".join(rows)


# ── Phase A: Gemini calls ──────────────────────────────────────────────────────

def _get_event_windows(df: pd.DataFrame, client, num_snapshots: int = 5) -> list[dict]:
    """
    One Gemini call — identify event-focused windows (start_row, end_row).
    Each window ends at a trigger event: abnormal lab, abnormal vital, or SBAR raised.
    Returns list of dicts with: start_row, end_row, phase, difficulty, context, next_action
    """
    n = len(df)
    tl = _compact(df)

    prompt = f"""You are identifying {num_snapshots} clinically significant event windows from an ICU timeline.

TIMELINE ({n} rows, 0-indexed):
{tl}

For each window, identify:
- START_ROW: where the build-up to the trigger event begins
- END_ROW: the row of the trigger event itself (see rules below)
- The clinical phase and severity

Reply with EXACTLY this format and nothing else:

WINDOW_1: <start_row>-<end_row>
PHASE_1: <admission|evolving|deterioration|management|late>
DIFFICULTY_1: <easy|medium|hard>
CONTEXT_1: <one sentence about what's happening in this window>
ACTION_1: <specific clinical action that should be taken>

WINDOW_2: <start_row>-<end_row>
PHASE_2: ...
(continue for all {num_snapshots} windows)

Rules:
- END_ROW must be one of these trigger event types:
    1. An abnormal lab result (e.g. rising creatinine, high lactate, low haemoglobin, abnormal electrolytes)
    2. An abnormal vital sign (e.g. tachycardia, hypotension, desaturation, fever, high RR)
    3. An SBAR raised by any clinician or nurse for any reason
  Do NOT end a window on a medication order, nursing task, normal result, or administrative event.
- Each window 15-40 rows (sufficient context leading up to the trigger)
- Windows must not overlap
- Spread across the entire stay
- Pick triggers from different clinical domains where possible (e.g. not all vitals)
"""

    raw = _call(client, prompt, max_tokens=2000, thinking=False)
    return _parse_event_windows(raw, n, num_snapshots)


def _parse_event_windows(raw: str, n: int, num_snapshots: int = 5) -> list[dict]:
    """Parse Gemini's event window response."""
    blocks: dict[int, dict] = {}
    key_re = re.compile(
        r"^(WINDOW|PHASE|DIFFICULTY|CONTEXT|ACTION)_(\d+):\s*(.*)", re.IGNORECASE
    )
    current_block: int | None = None

    for line in raw.splitlines():
        m = key_re.match(line.strip())
        if m:
            field, num_str, value = m.group(1).upper(), int(m.group(2)), m.group(3).strip()
            if field == "WINDOW":
                current_block = num_str
                blocks.setdefault(current_block, {})
                # Parse start-end format
                if "-" in value:
                    try:
                        start, end = value.split("-")
                        blocks[current_block]["start_row"] = int(start)
                        blocks[current_block]["end_row"] = int(end)
                    except (ValueError, IndexError):
                        pass
            else:
                if current_block is not None:
                    blocks.setdefault(current_block, {})[field] = value
        elif current_block is not None and line.strip():
            # Handle multi-line ACTION fields
            if "ACTION" in blocks[current_block]:
                blocks[current_block]["ACTION"] = (
                    blocks[current_block]["ACTION"] + " " + line.strip()
                )

    windows = []
    for num in sorted(blocks):
        b = blocks[num]
        try:
            start = max(0, min(int(b.get("start_row", 0)), n - 20))
            end = max(start + 15, min(int(b.get("end_row", start + 20)), n - 1))
        except (ValueError, TypeError):
            continue

        windows.append({
            "start_row": start,
            "end_row": end,
            "phase": b.get("PHASE", "unknown").lower(),
            "difficulty": b.get("DIFFICULTY", "medium").lower(),
            "context": b.get("CONTEXT", ""),
            "next_action": b.get("ACTION", ""),
        })

    return windows[:num_snapshots]


def _get_structured_answer(
    snapshot_df: pd.DataFrame,
    window: dict,
    full_df: pd.DataFrame,
    client,
) -> dict:
    """One Gemini call per snapshot — returns structured answer dict as JSON."""
    snap_text = _compact(snapshot_df)
    end = window["end_row"]
    after_text = _compact(full_df.iloc[end + 1: min(end + 16, len(full_df))])

    prompt = f"""You are writing a model answer for an ICU clinical decision-making exam.

A medical AI agent has been shown this timeline window and asked:
"{QUESTION_TEXT}"

--- PATIENT TIMELINE (what the agent sees) ---
{snap_text}
--- END ---

For reference only (do NOT reveal these in your answer):
--- SUBSEQUENT EVENTS ---
{after_text}
--- END ---

Clinical situation: {window['context']}
Expected action:   {window['next_action']}
Difficulty:        {window['difficulty']}

Respond with ONLY valid JSON (no markdown, no code blocks, just raw JSON):
{{
  "immediate_actions": ["action 1 with specific drug/dose/route if applicable", "action 2 if multiple"],
  "clinical_reasoning": ["timeline finding that drives this decision", "physiological rationale", "why this takes priority"],
  "monitoring_followup": ["what to monitor and timeframe", "second parameter to track"],
  "alternative_considerations": ["edge case where answer differs", "alternative consideration if applicable"]
}}

Write at senior-resident level. Be specific with numbers and drug names."""

    result = _call(client, prompt, max_tokens=2000, thinking=False)

    # Parse JSON response
    if isinstance(result, dict):
        return result

    try:
        # Try to extract JSON from response
        parsed = json.loads(result)
        return parsed
    except (json.JSONDecodeError, TypeError) as e:
        # Try to extract JSON from markdown code block if present
        import re as regex
        json_match = regex.search(r'```(?:json)?\s*(.*?)\s*```', result, regex.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(1))
                return parsed
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse JSON response: %s", str(e)[:100])
        logger.debug("Raw response: %s", result[:500])
        return {
            "immediate_actions": [],
            "clinical_reasoning": [],
            "monitoring_followup": [],
            "alternative_considerations": []
        }


def ask_gemini(df: pd.DataFrame, api_key: str, num_snapshots: int = 5) -> list[dict]:
    """
    Phase A — all Gemini interactions for one timeline.

    Returns a list of num_snapshots dicts:
      {start_row, end_row, phase, difficulty, context, next_action, answer_items}
      where answer_items contains structured fields directly from Gemini
    """
    client = _gemini_client(api_key)

    logger.info("    Identifying %d event windows …", num_snapshots)
    windows = _get_event_windows(df, client, num_snapshots)
    logger.info(
        "    Event windows: %s",
        [f"[{w['start_row']}-{w['end_row']}]" for w in windows]
    )

    total = len(windows)
    results = []
    for i, window in enumerate(windows, 1):
        logger.info(
            "    Generating answer %d/%d (rows %d-%d, %s, %s) …",
            i, total, window["start_row"], window["end_row"], window["phase"], window["difficulty"],
        )
        snap_df = df.iloc[window["start_row"]: window["end_row"] + 1]
        answer_items = _get_structured_answer(snap_df, window, df, client)
        results.append({**window, "answer_items": answer_items})

    return results


# ── Phase B: File I/O ──────────────────────────────────────────────────────────

def save_snapshots(
    df: pd.DataFrame,
    snapshots: list[dict],
    patient_dir: Path,
) -> None:
    """
    Phase B — write snapshot_1/ … snapshot_5/ inside patient_dir.
    No API calls.

    Each snapshot_i/ contains:
      - snapshot.csv: timeline rows start_row…end_row (the event window)
      - q_and_a.json: question, answer items, metadata
    """
    for i, snap in enumerate(snapshots, 1):
        snap_dir = patient_dir / f"snapshot_{i}"
        snap_dir.mkdir(exist_ok=True)

        start = snap["start_row"]
        end = snap["end_row"]
        df.iloc[start: end + 1].to_csv(snap_dir / "snapshot.csv", index=False)

        q_and_a = {
            "question": QUESTION_TEXT,
            "window": {
                "start_row": start,
                "end_row": end,
                "row_count": end - start + 1,
            },
            "phase": snap["phase"],
            "difficulty": snap["difficulty"],
            "clinical_context": snap["context"],
            "expected_next_action": snap["next_action"],
            **snap["answer_items"],  # Flattens immediate_actions, clinical_reasoning, etc.
        }

        (snap_dir / "q_and_a.json").write_text(json.dumps(q_and_a, indent=2))

    logger.info("    Saved 5 snapshot folders → %s", patient_dir)
