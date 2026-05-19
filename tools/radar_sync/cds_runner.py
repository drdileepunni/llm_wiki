"""Run the wiki-grounded CDS pipeline for a live patient."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _format_vitals(vitals: list[dict]) -> str:
    if not vitals:
        return "No vitals available"
    lines = []
    for v in vitals[:6]:
        hr  = v.get("daysHR")
        bp  = v.get("daysBP")
        spo = v.get("daysSpO2")
        rr  = v.get("daysRR")
        map_= v.get("daysMAP")
        fio = v.get("daysFiO2")
        ts  = v.get("timestamp", "")
        parts = [f"HR={hr}", f"BP={bp}", f"MAP={map_}", f"SpO2={spo}", f"RR={rr}"]
        if fio:
            parts.append(f"FiO2={fio}")
        lines.append(f"  [{ts}] {' | '.join(p for p in parts if p.split('=')[1] is not None and p.split('=')[1] != 'None')}")
    return "\n".join(lines)


def _format_labs(chart: dict) -> str:
    docs = [d for d in (chart.get("documents") or []) if d.get("category") == "labs"]
    if not docs:
        return "No lab results"
    lines = []
    for doc in docs[-10:]:
        name  = doc.get("name", "")
        ts    = doc.get("reportedAt", "")
        attrs = doc.get("attributes") or {}
        vals  = ", ".join(
            f"{k}={v.get('value')}" for k, v in list(attrs.items())[:6]
            if isinstance(v, dict) and v.get("value") is not None
        )
        lines.append(f"  [{ts}] {name}: {vals}")
    return "\n".join(lines)


def _format_active_orders(chart: dict) -> str:
    orders = (chart.get("orders") or {}).get("active") or {}
    meds = orders.get("medications") or []
    labs = orders.get("labs") or []
    procs = orders.get("procedures") or []
    lines = []
    if meds:
        lines.append("  Medications: " + ", ".join(
            f"{m.get('name','?')} {m.get('quantity','')} {m.get('unit','')} {m.get('route','')} {m.get('frequency','')}"
            for m in meds[:10]
        ))
    if labs:
        lines.append("  Pending labs: " + ", ".join(l.get("investigation", "?") for l in labs[:8]))
    if procs:
        lines.append("  Procedures: " + ", ".join(p.get("pType") or p.get("name", "?") for p in procs[:5]))
    return "\n".join(lines) if lines else "  No active orders"


def _format_io(io_summary: dict) -> str:
    return (
        f"Intake: {io_summary.get('intake_ml', 0):.0f} ml | "
        f"Output: {io_summary.get('output_ml', 0):.0f} ml | "
        f"Balance: {io_summary.get('balance_ml', 0):+.0f} ml"
    )


def _build_question(running_summary: str, chart: dict, io_summary: dict) -> str:
    demos = chart
    age   = (chart.get("age") or {}).get("year", "?")
    sex   = chart.get("sex") or chart.get("gender") or "?"
    days_icu = ""
    admit_raw = chart.get("ICUAdmitDate")
    if admit_raw:
        try:
            import pandas as pd
            admit_ts = pd.to_datetime(admit_raw, utc=True)
            delta = datetime.now(timezone.utc) - admit_ts.to_pydatetime()
            days_icu = f"Day {delta.days + 1} of ICU admission"
        except Exception:
            pass

    return f"""Patient: {age}y {sex}, {days_icu}
CPMRN: {chart.get('CPMRN', '?')}  Unit: {chart.get('unitName', '?')}  Bed: {chart.get('bedNo', '?')}
Allergies: {', '.join(chart.get('allergies') or []) or 'NKDA'}
Chronic: {', '.join(chart.get('chronic') or []) or 'None documented'}
Home medications: {', '.join(chart.get('home_medications') or []) or 'None'}

--- LONGITUDINAL CONTEXT ---
{running_summary if running_summary else '(First assessment — no prior summary)'}

--- CURRENT VITALS ---
{_format_vitals(chart.get('vitals') or [])}

--- RECENT LABS ---
{_format_labs(chart)}

--- ACTIVE ORDERS ---
{_format_active_orders(chart)}

--- FLUID BALANCE (cumulative) ---
{_format_io(io_summary)}

Given this patient's current status, what are the immediate next steps?"""


def run_cds(cpmrn: str, encounter: int, running_summary: str, chart: dict, io_summary: dict) -> dict:
    """
    Call the wiki-grounded CDS pipeline for this patient.
    Returns the full run_chat result dict.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / "app" / ".env")
    from backend.config import _default_kb
    from backend.services.chat_pipeline import run_chat

    question = _build_question(running_summary, chart, io_summary)
    logger.info("run_cds: calling CDS for CPMRN=%s enc=%s", cpmrn, encounter)

    result = run_chat(
        question=question,
        kb=_default_kb(),
        mode="cds",
        include_patient_context=False,
        cpmrn=cpmrn,
    )
    return result
