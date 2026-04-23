"""
Step 3 – Generate five clinical decision-making evaluation snapshots.

Two-phase design (Gemini calls are entirely separate from file I/O):

  Phase A – ask_gemini(df, api_key)
      › One Gemini call: identify 5 cut-point rows (plain-text key-value format,
        no JSON schema to avoid SDK truncation bugs).
      › Five Gemini calls: generate a model answer per cut-point.
      Returns a list of 5 snapshot dicts.

  Phase B – save_snapshots(df, snapshots, patient_dir)
      › Pure file I/O, no API calls.
      › Writes snapshot_1/ … snapshot_5/, each with:
          snapshot.csv   – timeline rows 0 … row_index (inclusive)
          question.txt   – fixed question shown to the evaluating agent
          answer.txt     – model answer with phase/difficulty header
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

QUESTION_TEXT = (
    "Above is part of a patient event timeline. "
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
    from google.genai import types
    config_kwargs: dict = dict(temperature=0, max_output_tokens=max_tokens)
    if not thinking:
        # Disable thinking budget — saves tokens for structured extraction tasks.
        try:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass  # older SDK versions don't have ThinkingConfig; ignore
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


# ── Compact timeline representation (avoids JSON-breaking characters) ──────────

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

def _parse_cut_points(raw: str, n: int) -> list[dict]:
    """
    Parse Gemini's plain-text cut-point response line by line.

    Looks for KEY_N: value lines grouped by block number.  Tolerates
    missing values, extra blank lines, and minor formatting variations.
    Returns a list of dicts (one per cut point found).
    """
    blocks: dict[int, dict] = {}
    key_re = re.compile(
        r"^(CUT_POINT|PHASE|DIFFICULTY|CONTEXT|ACTION)_(\d+):\s*(.*)", re.IGNORECASE
    )
    current_block: int | None = None
    current_key:   str | None = None

    for line in raw.splitlines():
        m = key_re.match(line.strip())
        if m:
            field, num_str, value = m.group(1).upper(), int(m.group(2)), m.group(3).strip()
            if field == "CUT_POINT":
                current_block = num_str
                current_key   = None
                blocks.setdefault(current_block, {})["row_index"] = value
            else:
                if current_block is not None:
                    blocks.setdefault(current_block, {})[field] = value
                    current_key = field
        elif current_key == "ACTION" and current_block is not None and line.strip():
            # ACTION can span multiple lines — append continuation
            blocks[current_block]["ACTION"] = (
                blocks[current_block].get("ACTION", "") + " " + line.strip()
            )

    pts = []
    for num in sorted(blocks):
        b = blocks[num]
        try:
            row = max(15, min(int(b.get("row_index", 15)), n - 5))
        except (ValueError, TypeError):
            continue
        pts.append({
            "row_index":   row,
            "phase":       b.get("PHASE", "unknown").lower(),
            "difficulty":  b.get("DIFFICULTY", "medium").lower(),
            "context":     b.get("CONTEXT", ""),
            "next_action": b.get("ACTION", ""),
        })
    return pts


def _get_cut_points(df: pd.DataFrame, client) -> list[dict]:
    """Single Gemini call — plain-text key-value format, no JSON schema."""
    n  = len(df)
    tl = _compact(df)

    prompt = f"""You are picking 5 clinical decision cut-points from an ICU timeline.

TIMELINE ({n} rows, 0-indexed):
{tl}

Reply with EXACTLY this format and nothing else:

CUT_POINT_1: <row_index>
PHASE_1: <admission|evolving|deterioration|management|late>
DIFFICULTY_1: <easy|medium|hard>
CONTEXT_1: <one sentence about patient state at this cut>
ACTION_1: <specific next clinical action>

CUT_POINT_2: <row_index>
PHASE_2: ...
DIFFICULTY_2: ...
CONTEXT_2: ...
ACTION_2: ...

CUT_POINT_3: <row_index>
PHASE_3: ...
DIFFICULTY_3: ...
CONTEXT_3: ...
ACTION_3: ...

CUT_POINT_4: <row_index>
PHASE_4: ...
DIFFICULTY_4: ...
CONTEXT_4: ...
ACTION_4: ...

CUT_POINT_5: <row_index>
PHASE_5: ...
DIFFICULTY_5: ...
CONTEXT_5: ...
ACTION_5: ...

Rules: row indices between 15 and {n - 5}, \
spread across the stay, no two within 20 rows of each other."""

    raw = _call(client, prompt, max_tokens=8192, thinking=False)
    pts = _parse_cut_points(raw, n)

    if len(pts) < 5:
        raise ValueError(
            f"Only parsed {len(pts)}/5 cut points from Gemini response:\n{raw[:600]}"
        )

    pts = pts[:5]
    pts.sort(key=lambda x: x["row_index"])
    return pts


def _get_answer(
    snapshot_df: pd.DataFrame,
    cut_point: dict,
    full_df: pd.DataFrame,
    client,
) -> str:
    """One Gemini call per snapshot — plain text, no JSON."""
    snap_text  = _compact(snapshot_df)
    cut        = cut_point["row_index"]
    after_text = _compact(full_df.iloc[cut + 1: cut + 16])

    prompt = f"""You are writing a model answer for an ICU clinical decision-making exam.

A medical AI agent has been shown the timeline below and asked:
"{QUESTION_TEXT}"

--- PATIENT TIMELINE (what the agent sees) ---
{snap_text}
--- END ---

For your reference ONLY (do NOT reveal these rows in your answer):
--- SUBSEQUENT EVENTS ---
{after_text}
--- END ---

Clinical situation: {cut_point['context']}
Expected action:   {cut_point['next_action']}
Difficulty:        {cut_point['difficulty']}

Write the model answer in this exact structure:

**IMMEDIATE ACTION**
One sentence: the specific next action (include drug / dose / route / procedure).

**CLINICAL REASONING**
2-3 paragraphs:
- Which timeline findings drive this decision (cite specific values or events)
- Physiological or pharmacological rationale
- Why this takes priority over other possible actions

**MONITORING & FOLLOW-UP**
Bullet list: what to check after the action, with thresholds and timeframes.

**ALTERNATIVE CONSIDERATIONS**
1-2 sentences: edge cases or situations where the answer would differ.

Write at senior-resident level. Be specific with numbers."""

    return _call(client, prompt, max_tokens=2000)


def ask_gemini(df: pd.DataFrame, api_key: str) -> list[dict]:
    """
    Phase A — all Gemini interactions for one timeline.

    Returns a list of 5 dicts:
      {row_index, phase, difficulty, context, next_action, answer}
    """
    client = _gemini_client(api_key)

    logger.info("    Identifying 5 cut points …")
    cut_points = _get_cut_points(df, client)
    logger.info("    Cut-point rows: %s", [p["row_index"] for p in cut_points])

    results = []
    for i, cp in enumerate(cut_points, 1):
        logger.info(
            "    Generating answer %d/5 (row %d, %s, %s) …",
            i, cp["row_index"], cp["phase"], cp["difficulty"],
        )
        snap_df = df.iloc[: cp["row_index"] + 1]
        answer  = _get_answer(snap_df, cp, df, client)
        results.append({**cp, "answer": answer})

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
    """
    for i, snap in enumerate(snapshots, 1):
        snap_dir = patient_dir / f"snapshot_{i}"
        snap_dir.mkdir(exist_ok=True)

        cut = snap["row_index"]
        df.iloc[: cut + 1].to_csv(snap_dir / "snapshot.csv", index=False)

        (snap_dir / "question.txt").write_text(QUESTION_TEXT + "\n")

        answer_file = (
            f"PHASE: {snap['phase'].upper()} | DIFFICULTY: {snap['difficulty'].upper()}\n\n"
            f"CLINICAL CONTEXT:\n{snap['context']}\n\n"
            f"EXPECTED NEXT ACTION:\n{snap['next_action']}\n\n"
            f"{'=' * 60}\n\n"
            f"{snap['answer']}\n"
        )
        (snap_dir / "answer.txt").write_text(answer_file)

    logger.info("    Saved 5 snapshot folders → %s", patient_dir)
