"""
Viva clinical simulator.

After the student's orders are placed, this agent generates realistic clinical
responses (vitals changes, lab results, ECG notes, I/O) and writes them
directly to the dummy patient's MongoDB document.

It runs as a separate LLM call so the teacher can reference actual values
("VBG pH is now 7.34") rather than inventing them, and lab orders are
automatically completed so the agent won't re-order them next turn.

write_patient_state() is also used by viva.py to seed the initial patient
state produced by the teacher's first scenario.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from .llm_client import get_llm_client
from .emr import (
    VIVA_DUMMY_CPMRN,
    get_latest_vitals,
    push_vitals,
    push_lab_result,
    push_event_note,
    push_io_entry,
    complete_lab_orders,
    update_vent_flags,
    update_patient_history,
    place_home_meds_as_active_orders,
)

log = logging.getLogger("wiki.viva_sim")

# Minutes until a lab result is available after ordering.
# Matched case-insensitively against order name.
LAB_RESULT_DELAYS: dict[str, int] = {
    "lactate": 60,
    "vbg": 30,
    "abg": 30,
    "cbc": 90,
    "fbc": 90,
    "bmp": 90,
    "u&e": 90,
    "lft": 90,
    "troponin": 60,
    "bnp": 90,
    "coags": 60,
    "pt/inr": 60,
    "blood culture": 2880,
    "urine culture": 2880,
    "sputum culture": 2880,
    "chest x-ray": 45,
    "cxr": 45,
    "echo": 120,
    "echocardiogram": 120,
    "ecg": 15,
    "ct": 90,
    "mri": 180,
}
_DEFAULT_RESULT_DELAY = 60


def get_result_delay(order_name: str) -> int:
    """Return estimated result delay in minutes for a given order name."""
    return LAB_RESULT_DELAYS.get(order_name.lower().strip(), _DEFAULT_RESULT_DELAY)


_SYSTEM = (
    "You are a clinical simulation engine for a medical training viva. "
    "Generate realistic patient responses to clinical interventions. "
    "Output ONLY valid JSON — no markdown, no prose."
)

_PROMPT = """\
CASE TRAJECTORY (private — defines expected patient evolution):
{trajectory}

CURRENT PATIENT STATE (before these orders took effect):
{current_state}

ORDERS PLACED THIS TURN:
{orders_text}

CLINICAL CONTEXT THIS TURN:
{clinical_context}

Turn {turn_num} of {max_turns}. {time_delta_minutes} minutes of simulated time has passed.
Generate {n_snapshots} hourly vital snapshots for this interval.

LABS AVAILABLE THIS TURN (ordered in prior turns, results now due — include results in "labs"):
{available_labs_text}

LABS STILL PENDING (ordered but NOT yet resulted — do NOT generate results for these):
{pending_labs_text}

Your job: generate the patient's realistic response to these interventions.
Follow the trajectory's expected evolution if orders were appropriate.
If critical orders were missed, surface realistic consequences.

Return ONLY this JSON structure (omit any key that is not applicable):

{{
  "vital_timeline": [
    {{
      "offset_minutes": <60, 120, 180, ...>,
      "vitals": {{
        "BP": "<systolic/diastolic as string, e.g. 145/90>",
        "HR": <number>,
        "SpO2": <number, percent>,
        "RR": <number>,
        "Temperature": <number, Celsius>,
        "TherapyDevice": "<room air | oxygen mask | NIV | HFNC | ventilator | none>",
        "FiO2": <0.21–1.0>,
        "OxygenFlow": <L/min if applicable>,
        "VentMode": "<if intubated>",
        "VentPEEP": <if intubated>,
        "AVPU": "<A | V | P | U>"
      }},
      "io": {{
        "urine_ml": <number>,
        "intake_ml": <number>,
        "period_mins": 60
      }}
    }}
  ],
  "labs": [
    {{
      "name": "<test name exactly matching the order, e.g. VBG>",
      "attributes": {{
        "<param_key>": {{
          "name": "<display name>",
          "value": "<value as string>",
          "errorRange": {{"min": <normal_min>, "max": <normal_max>}}
        }}
      }}
    }}
  ],
  "notes": [
    {{"category": "event", "text": "<concise clinical note, e.g. ECG: sinus tachycardia at 96 bpm, no ST changes>"}}
  ],
  "complete_lab_orders": ["<lab names to mark as resulted and remove from active orders>"],
  "vent_flags": {{
    "isNIV": <true|false>,
    "isHFNC": <true|false>,
    "isIntubated": <true|false>
  }}
}}

Rules:
- vital_timeline MUST have exactly {n_snapshots} entries with offset_minutes 60, 120, ... ({time_delta_minutes} max)
- Each snapshot's vitals must show physiologically plausible evolution — not identical copies
- Only include "io" inside a snapshot if urine output monitoring is active
- "labs" must include results for all LABS AVAILABLE THIS TURN plus any fast-turnaround labs ordered this turn (e.g. ABG/VBG/ECG if ordered and delay < 60 min)
- Do NOT generate results for LABS STILL PENDING
- Only include "notes" if a monitoring order (ECG, arterial line, etc.) was placed
- Only include "vent_flags" if a respiratory support order was placed
- "complete_lab_orders" must list all labs whose results appear in "labs"
- Values must be physiologically realistic for the clinical scenario
"""


def write_patient_state(state: dict, cpmrn: str) -> str:
    """
    Write a patient_state dict to MongoDB.  Handles any subset of keys:
    vitals, labs, notes, io, complete_lab_orders, vent_flags.
    Returns a plain-text summary of what was written.
    Used for seeding initial patient state at session start.
    """
    summary_parts: list[str] = []

    if state.get("vitals"):
        push_vitals(cpmrn, state["vitals"])
        v = state["vitals"]
        vital_str = "  ".join(
            f"{k}: {v[k]}" for k in ("BP", "HR", "SpO2", "RR", "TherapyDevice", "FiO2")
            if k in v
        )
        summary_parts.append(f"Vitals: {vital_str}")

    for lab in state.get("labs", []):
        push_lab_result(cpmrn, lab["name"], lab.get("attributes", {}))
        attrs = lab.get("attributes", {})
        vals = ", ".join(
            f"{v.get('name', k)}: {v.get('value', '?')}"
            for k, v in attrs.items()
            if isinstance(v, dict)
        )
        summary_parts.append(f"{lab['name']}: {vals}")

    for note in state.get("notes", []):
        push_event_note(cpmrn, note.get("category", "event"), note["text"])
        summary_parts.append(note["text"])

    if state.get("io"):
        io = state["io"]
        push_io_entry(
            cpmrn,
            urine_ml=io.get("urine_ml", 0),
            intake_ml=io.get("intake_ml", 0),
            period_mins=io.get("period_mins", 60),
        )
        summary_parts.append(
            f"Urine output: {io.get('urine_ml', '?')} mL over {io.get('period_mins', 60)} min"
        )

    if state.get("complete_lab_orders"):
        complete_lab_orders(cpmrn, state["complete_lab_orders"])

    if state.get("vent_flags"):
        vf = state["vent_flags"]
        update_vent_flags(
            cpmrn,
            is_niv=vf.get("isNIV"),
            is_hfnc=vf.get("isHFNC"),
            is_intubated=vf.get("isIntubated"),
        )

    home_medications = state.get("home_medications")
    diagnoses = state.get("diagnoses")
    allergies = state.get("allergies")
    if home_medications is not None or diagnoses is not None or allergies is not None:
        update_patient_history(
            cpmrn,
            home_medications=home_medications,
            diagnoses=diagnoses,
            allergies=allergies,
        )
        if home_medications:
            summary_parts.append(f"Home meds: {', '.join(home_medications)}")
            # Place each home med as an active order so the LLM can reference them
            # by orderNo and properly stop/hold them without re-suggesting each turn.
            placed = place_home_meds_as_active_orders(home_medications, cpmrn)
            if placed:
                summary_parts.append(f"Placed {placed} home med(s) as active orders")

    return "\n".join(summary_parts) if summary_parts else "(no state data)"


def write_patient_timeline(sim: dict, cpmrn: str, base_time: datetime) -> str:
    """
    Write a time-aware simulation result to MongoDB.
    Each snapshot in vital_timeline is written with its own backdated timestamp.
    Labs, notes, and vent_flags are written at current time (they represent
    the final state, e.g. a resulted lab).
    Returns a plain-text summary of the last snapshot's vitals.
    """
    summary_parts: list[str] = []

    for snapshot in sim.get("vital_timeline", []):
        offset = snapshot.get("offset_minutes", 60)
        ts = (base_time + timedelta(minutes=offset)).isoformat()
        if snapshot.get("vitals"):
            push_vitals(cpmrn, snapshot["vitals"], timestamp=ts)
        if snapshot.get("io"):
            io = snapshot["io"]
            push_io_entry(
                cpmrn,
                urine_ml=io.get("urine_ml", 0),
                intake_ml=io.get("intake_ml", 0),
                period_mins=io.get("period_mins", 60),
                timestamp=ts,
            )

    # Report last snapshot vitals in summary
    timeline = sim.get("vital_timeline", [])
    if timeline:
        last = timeline[-1].get("vitals", {})
        vital_str = "  ".join(
            f"{k}: {last[k]}" for k in ("BP", "HR", "SpO2", "RR", "TherapyDevice", "FiO2")
            if k in last
        )
        if vital_str:
            summary_parts.append(f"Vitals: {vital_str}")

    for lab in sim.get("labs", []):
        push_lab_result(cpmrn, lab["name"], lab.get("attributes", {}))
        attrs = lab.get("attributes", {})
        vals = ", ".join(
            f"{v.get('name', k)}: {v.get('value', '?')}"
            for k, v in attrs.items()
            if isinstance(v, dict)
        )
        summary_parts.append(f"{lab['name']}: {vals}")

    for note in sim.get("notes", []):
        push_event_note(cpmrn, note.get("category", "event"), note["text"])
        summary_parts.append(note["text"])

    if sim.get("complete_lab_orders"):
        complete_lab_orders(cpmrn, sim["complete_lab_orders"])

    if sim.get("vent_flags"):
        vf = sim["vent_flags"]
        update_vent_flags(
            cpmrn,
            is_niv=vf.get("isNIV"),
            is_hfnc=vf.get("isHFNC"),
            is_intubated=vf.get("isIntubated"),
        )

    return "\n".join(summary_parts) if summary_parts else "(no simulation data)"


def _orders_to_text(orders: list[dict]) -> str:
    if not orders:
        return "(none)"
    lines = []
    for o in orders:
        t = o.get("order_type", "")
        name = o.get("orderable_name") or o.get("recommendation") or "—"
        details = o.get("order_details") or {}
        instr = details.get("instructions") or o.get("notes") or ""
        line = f"[{t.upper()}] {name}"
        if instr:
            line += f" — {instr}"
        lines.append(line)
    return "\n".join(lines)


def _current_state_text(cpmrn: str) -> str:
    try:
        v = get_latest_vitals(cpmrn)
        if v.get("vitals") is None and "BP" not in v:
            return "(no prior vitals recorded)"
        parts = []
        for label, key in [
            ("BP", "BP"), ("HR", "HR"), ("SpO2", "SpO2"), ("RR", "RR"),
            ("Temp", "Temperature"), ("Device", "TherapyDevice"), ("FiO2", "FiO2"),
        ]:
            val = v.get(key)
            if val is not None:
                parts.append(f"{label}: {val}")
        return "  ".join(parts) if parts else "(no prior vitals)"
    except Exception as exc:
        log.warning("Could not read current vitals for simulation: %s", exc)
        return "(vitals unavailable)"


def simulate_and_write(
    trajectory: str,
    orders: list[dict],
    cpmrn: str,
    clinical_context: str,
    turn_num: int,
    max_turns: int,
    model: str | None = None,
    time_delta_minutes: int = 60,
    available_labs: list[str] | None = None,
    pending_labs: list[str] | None = None,
) -> dict:
    """
    Run the simulator LLM, write results to MongoDB, and return a
    plain-text summary of what happened (for the teacher to reference).

    Returns a dict with keys: summary (str), raw (dict), vital_timeline (list).
    """
    llm = get_llm_client(model=model)
    current_state = _current_state_text(cpmrn)
    orders_text = _orders_to_text(orders)

    n_snapshots = max(1, min(time_delta_minutes // 60, 12))
    available_labs_text = ", ".join(available_labs) if available_labs else "(none)"
    pending_labs_text = ", ".join(pending_labs) if pending_labs else "(none)"

    prompt = _PROMPT.format(
        trajectory=trajectory,
        current_state=current_state,
        orders_text=orders_text,
        clinical_context=clinical_context,
        turn_num=turn_num,
        max_turns=max_turns,
        time_delta_minutes=time_delta_minutes,
        n_snapshots=n_snapshots,
        available_labs_text=available_labs_text,
        pending_labs_text=pending_labs_text,
    )

    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_SYSTEM,
        max_tokens=4000,
        force_tool=False,
    )

    raw_text = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    raw_text = re.sub(r"\s*```$", "", raw_text.strip())

    try:
        sim = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.warning("Simulator JSON parse error: %s\n%s", exc, raw_text[:300])
        return {"summary": "(simulation unavailable — parse error)", "raw": {}, "vital_timeline": []}

    vital_timeline = sim.get("vital_timeline", [])
    log.info(
        "Simulator turn %d: %d snapshots over %d min  labs=%d  notes=%d  available_labs=%s  pending_labs=%s",
        turn_num,
        len(vital_timeline),
        time_delta_minutes,
        len(sim.get("labs", [])),
        len(sim.get("notes", [])),
        available_labs_text,
        pending_labs_text,
    )

    # base_time is the simulated start of this interval (time_delta ago)
    base_time = datetime.utcnow() - timedelta(minutes=time_delta_minutes)
    summary = write_patient_timeline(sim, cpmrn, base_time)
    return {"summary": summary or "(no simulation data)", "raw": sim, "vital_timeline": vital_timeline}
