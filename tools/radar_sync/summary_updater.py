"""
Update the rolling patient summary using Gemini structured output.

update_summary() returns a PatientSummary dict (see patient_summary_schema.py).
The caller stores structured_summary=<dict> and running_summary=<dict["narrative"]>.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SUMMARY_MODEL = "gemini-3.1-flash-lite"

# ── Timezone helper ───────────────────────────────────────────────────────────

from datetime import timedelta

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _to_ist(ts_val: Any) -> str:
    """
    Convert a timestamp value (datetime object or ISO string, assumed UTC when
    naive) to an IST-labelled string like '2026-05-20 06:40 IST'.
    Returns the raw value as a string if parsing fails.
    """
    if ts_val is None:
        return ""
    try:
        import pandas as pd
        from datetime import timezone as _tz
        dt = pd.to_datetime(ts_val, utc=True).to_pydatetime()
        # pd.to_datetime treats naive strings as UTC when utc=True
        ist = dt + _IST_OFFSET
        return ist.strftime("%Y-%m-%d %H:%M IST")
    except Exception:
        return str(ts_val)


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
    # Vitals are pre-filtered at ingestion time (chart_puller._filter_vitals)
    vitals = (chart.get("vitals") or [])[:8]
    if not vitals:
        return "No vitals"
    lines = []
    for v in vitals:
        ts   = _to_ist(v.get("timestamp"))
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
    for doc in docs[-12:]:
        name  = doc.get("name", "")
        ts    = _to_ist(doc.get("reportedAt"))
        attrs = doc.get("attributes") or {}
        valued = [(k, v) for k, v in attrs.items()
                  if isinstance(v, dict) and v.get("value") not in (None, "")]
        vals  = ", ".join(
            f"{k}={v.get('value')} {v.get('unit', '')}".strip()
            for k, v in valued[:8]
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
            ts     = _to_ist(content.get("timestamp") or note.get("createdTimestamp"))
            ntype  = f"{content.get('noteType', '')} / {content.get('noteSubType', '')}".strip(" /")
            author = ""
            if isinstance(content.get("author"), dict):
                author = content["author"].get("name", "")
            entries.append((ts, ntype, author, text))
    if not entries:
        return "No notes"
    lines = []
    for ts, ntype, author, text in entries:
        lines.append(f"[{ts} | {ntype} | {author}]\n{text[:600]}")
    return "\n\n".join(lines)


def _format_delta(delta: dict) -> str:
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
            ts   = _to_ist(v.get("timestamp"))
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
            ts    = _to_ist(lab.get("reportedAt"))
            attrs = lab.get("attributes") or {}
            valued = [(k, v) for k, v in attrs.items()
                      if isinstance(v, dict) and v.get("value") not in (None, "")]
            vals  = ", ".join(
                f"{k}={v.get('value')} {v.get('unit', '')}".strip()
                for k, v in valued[:6]
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


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a senior ICU physician writing concise, problem-oriented patient summaries. "
    "Use specific numbers (vitals, lab values, drug names and doses). "
    "Be factual — do not invent findings not present in the data."
)

def _initial_prompt(cpmrn: str, delta: dict, chart: dict | None) -> str:
    if chart:
        context = f"""DEMOGRAPHICS: {_format_demographics(chart)}

VITALS TREND (newest first):
{_format_vitals_trend(chart)}

LABORATORY RESULTS:
{_format_labs_full(chart)}

ACTIVE MEDICATIONS: {_format_active_meds(chart)}

CLINICAL NOTES:
{_format_notes_full(chart)}"""
    else:
        context = f"CLINICAL DATA:\n{_format_delta(delta)}"

    return f"""Patient CPMRN: {cpmrn}

{context}

Write a structured problem-oriented ICU summary.

For each active clinical problem include:
- name: concise problem name (e.g. "Septic shock", "AKI")
- status: one of critical / worsening / stable / improving / resolved
- presenting_features: how it first appeared with specific values
- workup: investigations done and key results
- management: current treatment with drug names and doses
- current_state: latest status with specific numbers
- plan_changing_event: if a specific investigation result or event changed the care plan, describe it; otherwise null

admission_narrative: one sentence — age, sex, PMH, presenting complaint.
resolved_problems: list of strings for problems that have resolved.
narrative: a 4-6 sentence paragraph covering the full picture (this is the human-readable summary).
suggested_actions: list of 3-5 specific, actionable clinical suggestions for the most critical active problems — e.g. "Start vancomycin 25 mg/kg IV q12h for MRSA coverage", "Check serum lactate and repeat in 2h", "Noradrenaline 0.1 mcg/kg/min — consider uptitration if MAP <65". Be specific with drug names, doses, and routes. Do not suggest actions already reflected in current management."""


def _update_prompt(cpmrn: str, existing_summary: dict, delta: dict) -> str:
    delta_text = _format_delta(delta)
    existing_narrative = existing_summary.get("narrative", "") if isinstance(existing_summary, dict) else existing_summary

    # Enumerate current problem names so the model reuses them exactly
    existing_problems = existing_summary.get("problems", []) if isinstance(existing_summary, dict) else []
    if existing_problems:
        names_block = "\n".join(f"  - {p['name']}" for p in existing_problems)
        names_instruction = (
            f"\nEXISTING PROBLEM NAMES — use these EXACT strings if the problem is the same. "
            f"Do NOT rename, split, or create synonyms:\n{names_block}\n"
        )
    else:
        names_instruction = ""

    return f"""Patient CPMRN: {cpmrn}

CURRENT SUMMARY:
{existing_narrative}
{names_instruction}
NEW EVENTS IN THE LAST HOUR:
{delta_text}

Update the structured summary to reflect these new events.

Rules:
- Keep all existing problems; update their current_state with new numbers
- Use the EXACT same problem name strings listed above — never rename or create synonyms (e.g. do not add "Respiratory Failure" if "Acute Respiratory Failure" already exists)
- If a problem has resolved or significantly improved, move it to resolved_problems
- Add new problems ONLY if a genuinely NEW clinical issue emerged that is not already listed
- Set plan_changing_event if a new investigation result changed the management plan
- Do not lose historical context (initial presentation, key prior results)
- narrative: updated 4-6 sentence paragraph covering the current picture
- suggested_actions: 3-5 specific actionable suggestions based on new events (drug names, doses, routes, specific labs). Do not repeat actions already in current management."""


# ── Main entry point ──────────────────────────────────────────────────────────

def update_summary(existing_summary: str | dict, delta: dict, cpmrn: str, chart: dict | None = None) -> dict:
    """
    Produce or update a structured PatientSummary dict.

    existing_summary: either the previous structured dict or the legacy narrative string.
                      Pass "" or {} for the first snapshot.
    Returns a PatientSummary dict with keys:
        admission_narrative, problems, resolved_problems, narrative
    """
    # Normalise seed
    if isinstance(existing_summary, dict):
        seed_text = existing_summary.get("narrative", "")
        seed_dict = existing_summary
    else:
        seed_text = existing_summary or ""
        seed_dict = {}

    if not seed_text.strip():
        prompt = _initial_prompt(cpmrn, delta, chart)
    else:
        prompt = _update_prompt(cpmrn, seed_dict, delta)

    return _call_gemini_structured(prompt)


# ── Gemini structured call ────────────────────────────────────────────────────

def _call_gemini_structured(prompt: str) -> dict:
    _root = Path(__file__).resolve().parents[2]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from tools.radar_sync.patient_summary_schema import PatientSummary

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_SUMMARY_MODEL)
    return client.generate_json(prompt=prompt, schema=PatientSummary, system=_SYSTEM, max_tokens=2048)
