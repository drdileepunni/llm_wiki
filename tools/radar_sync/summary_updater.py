"""Update the rolling patient summary using the LLM."""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Chart context formatters ──────────────────────────────────────────────────

def _format_demographics(chart: dict) -> str:
    age  = (chart.get("age") or {}).get("year", "?")
    sex  = chart.get("sex", "?")
    pmh  = ", ".join(chart.get("chronic") or []) or "none documented"
    alg  = ", ".join(chart.get("allergies") or []) or "none"
    unit = chart.get("unitName", "")
    bed  = chart.get("bedNo", "")
    return f"{age}y {sex}  |  PMH: {pmh}  |  Allergies: {alg}  |  {unit} Bed {bed}"


def _format_vitals_trend(chart: dict) -> str:
    vitals = (chart.get("vitals") or [])[:8]  # newest 8
    if not vitals:
        return "No vitals"
    lines = []
    for v in vitals:
        ts   = v.get("timestamp", "")
        hr   = v.get("daysHR")
        bp   = v.get("daysBP")
        map_ = v.get("daysMAP")
        spo2 = v.get("daysSpO2")
        rr   = v.get("daysRR")
        fio2 = v.get("daysFiO2")
        parts = [x for x in [
            f"HR={hr}", f"BP={bp}", f"MAP={map_}",
            f"SpO2={spo2}", f"RR={rr}", f"FiO2={fio2}" if fio2 else None,
        ] if x and "=None" not in x]
        lines.append(f"  [{ts}] {' | '.join(parts)}")
    return "\n".join(lines)


def _format_labs_full(chart: dict) -> str:
    docs = [d for d in (chart.get("documents") or []) if d.get("category") == "labs"]
    if not docs:
        return "No labs"
    lines = []
    for doc in docs[-12:]:  # newest 12 panels
        name  = doc.get("name", "")
        ts    = doc.get("reportedAt", "")
        attrs = doc.get("attributes") or {}
        vals  = ", ".join(
            f"{k}={v.get('value')} {v.get('unit', '')}".strip()
            for k, v in list(attrs.items())[:8]
            if isinstance(v, dict) and v.get("value") is not None
        )
        lines.append(f"  [{ts}] {name}: {vals}" if vals else f"  [{ts}] {name}")
    return "\n".join(lines)


def _format_active_meds(chart: dict) -> str:
    orders = chart.get("orders") or {}
    meds = (orders.get("active") or {}).get("medications") or []
    if not meds:
        return "None documented"
    return ", ".join(m.get("name", "") for m in meds if m.get("name"))


def _format_notes_full(chart: dict) -> str:
    """Extract all substantive note text for initial context."""
    notes_obj = chart.get("notes") or {}
    entries = []
    for note in (notes_obj.get("finalNotes") or []):
        for content in (note.get("content") or []):
            text_parts = []
            for comp in (content.get("components") or []):
                if isinstance(comp, dict) and comp.get("value"):
                    text_parts.append(re.sub(r"<[^>]+>", " ", comp["value"]).strip())
            text = " ".join(p for p in text_parts if p).strip()
            if len(text) < 80:
                continue
            ts     = content.get("timestamp") or note.get("createdTimestamp") or ""
            ntype  = f"{content.get('noteType', '')} / {content.get('noteSubType', '')}".strip(" /")
            author = ""
            if isinstance(content.get("author"), dict):
                author = content["author"].get("name", "")
            entries.append((str(ts), ntype, author, text))
    if not entries:
        return "No notes"
    lines = []
    for ts, ntype, author, text in entries:
        lines.append(f"[{ts} | {ntype} | {author}]\n{text[:600]}")
    return "\n\n".join(lines)


def _format_delta(delta: dict) -> str:
    """Format incremental delta for update prompts."""
    lines = []

    vitals = delta.get("new_vitals") or []
    if vitals:
        lines.append(f"New vitals ({len(vitals)} readings, newest first):")
        for v in vitals[:4]:
            hr   = v.get("daysHR")
            bp   = v.get("daysBP")
            spo2 = v.get("daysSpO2")
            rr   = v.get("daysRR")
            map_ = v.get("daysMAP")
            ts   = v.get("timestamp", "")
            parts = [x for x in [
                f"HR={hr}", f"BP={bp}", f"MAP={map_}",
                f"SpO2={spo2}", f"RR={rr}",
            ] if x and "=None" not in x]
            lines.append(f"  [{ts}] {' | '.join(parts)}")

    labs = delta.get("new_labs") or []
    if labs:
        lines.append(f"New labs ({len(labs)} panels):")
        for lab in labs[-5:]:
            name  = lab.get("name", "")
            ts    = lab.get("reportedAt", "")
            attrs = lab.get("attributes") or {}
            vals  = ", ".join(
                f"{k}={v.get('value')} {v.get('unit', '')}".strip()
                for k, v in list(attrs.items())[:6]
                if isinstance(v, dict) and v.get("value") is not None
            )
            lines.append(f"  [{ts}] {name}: {vals}" if vals else f"  [{ts}] {name}")

    notes = delta.get("new_notes") or []
    if notes:
        lines.append(f"New notes ({len(notes)}):")
        for n in notes:
            author = n.get("author", "clinician")
            ntype  = n.get("note_type", "note")
            text   = (n.get("text") or "")[:400]
            lines.append(f"  [{ntype} by {author}]: {text}")

    orders = delta.get("delta_orders") or {}
    active_meds = (orders.get("active") or {}).get("medications") or []
    if active_meds:
        names = [m.get("name", "") for m in active_meds if m.get("name")]
        lines.append(f"Active medications: {', '.join(names[:12])}")

    io = delta.get("io_last_24h") or {}
    if io.get("intake_ml") or io.get("output_ml"):
        lines.append(
            f"I/O: intake={io.get('intake_ml')} ml  output={io.get('output_ml')} ml  "
            f"balance={io.get('balance_ml')} ml"
        )

    return "\n".join(lines) if lines else "(no new events)"


# ── Main entry point ──────────────────────────────────────────────────────────

def update_summary(existing_summary: str, delta: dict, cpmrn: str, chart: dict | None = None) -> str:
    """
    Call the LLM to produce or update a problem-oriented rolling patient summary.
    On first run (no existing_summary), pass chart for full context.
    Returns the updated summary string.
    """
    if not existing_summary.strip():
        prompt = _initial_prompt(cpmrn, delta, chart)
    else:
        prompt = _update_prompt(cpmrn, existing_summary, delta)

    return _call_llm(prompt)


def _initial_prompt(cpmrn: str, delta: dict, chart: dict | None) -> str:
    if chart:
        demographics = _format_demographics(chart)
        vitals_text  = _format_vitals_trend(chart)
        labs_text    = _format_labs_full(chart)
        meds_text    = _format_active_meds(chart)
        notes_text   = _format_notes_full(chart)
        context = f"""DEMOGRAPHICS: {demographics}

VITALS TREND (newest first):
{vitals_text}

LABORATORY RESULTS:
{labs_text}

ACTIVE MEDICATIONS: {meds_text}

CLINICAL NOTES:
{notes_text}"""
    else:
        context = f"CLINICAL DATA:\n{_format_delta(delta)}"

    return f"""You are a senior ICU physician writing an initial problem-oriented patient summary.
Patient CPMRN: {cpmrn}

{context}

Write a structured problem-oriented ICU summary using EXACTLY this format:

**[Age]y [sex] with PMH of [conditions] presented with [chief complaint and key initial findings].**

**Active Problems:**
1. **[Problem name]:** [What was found/initial presentation] → [Workup done and key results] → [Current management] → [Current status with specific numbers]
2. **[Problem name]:** ...
(list all active clinical problems — aim for 4-6 problems)

**Resolved / Improving:**
- [Problem]: [Brief note on resolution or improvement]

Rules:
- Each problem gets ONE entry with the full arc: presentation → workup → management → current status
- Use specific numbers throughout (vitals, lab values, drug names and doses where available)
- Separate problems clearly — do not lump everything together
- If a problem has multiple components (e.g. hypertension led to pulmonary oedema), still list as separate problems
- Write as a handover note a covering physician would read"""


def _update_prompt(cpmrn: str, existing_summary: str, delta: dict) -> str:
    delta_text = _format_delta(delta)
    return f"""You are a senior ICU physician updating a problem-oriented patient summary.
Patient CPMRN: {cpmrn}

CURRENT SUMMARY:
{existing_summary}

NEW EVENTS IN THE LAST HOUR:
{delta_text}

Update the summary to reflect these new events. Keep the same problem-oriented structure:

**[demographics line]**

**Active Problems:**
1. **[Problem]:** ... → [updated current status]
...

**Resolved / Improving:**
- ...

Rules:
- Keep all existing problems; update their current status with new numbers
- If a problem has resolved or significantly improved, move it to Resolved/Improving
- Add new problems only if genuinely new issues have emerged
- Use specific numbers. Do not lose historical context (e.g. initial BP, presenting complaint).
- Write only the updated summary, nothing else."""


# ── LLM call ──────────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "app" / ".env")
    from backend.services.llm_client import get_llm_client
    llm = get_llm_client()
    resp = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        max_tokens=1024,
    )
    if hasattr(resp, "content"):
        content = resp.content
        if isinstance(content, list):
            return "".join(
                block.text if hasattr(block, "text") else str(block)
                for block in content
            ).strip()
        return str(content).strip()
    return str(resp).strip()
