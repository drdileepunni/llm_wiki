"""
Status classifier — reasoning model that verifies worsening/critical labels
by examining vital and lab trends across stored snapshots before the CDS gate fires.

Flow:
  1. summary_updater assigns draft statuses (single-snapshot, no trajectory context)
  2. classify_statuses() runs a reasoning model for each worsening/critical problem
  3. Model calls get_vital_trend / get_lab_trend / query_patient_notes to check trajectory
  4. Model calls set_problem_statuses once satisfied — final statuses replace the draft
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = "gemini-2.5-flash"  # reasoning model with thinking
_MAX_TOOL_ROUNDS  = 8                   # max tool calls before forcing final answer
_THINKING_BUDGET  = 8000               # tokens for Gemini thinking

_SYSTEM = """You are a senior ICU clinician reviewing automatically-assigned clinical problem statuses.

A prior model labelled each problem from a single snapshot. Your job is to VERIFY these labels by \
checking TREND DATA — not just the latest value. You have tools to pull vital and lab trends, \
and to search clinical notes.

STATUS CRITERIA (apply strictly):
• critical  — immediate life threat, RIGHT NOW:
    Vasopressors running AND haemodynamically unstable (MAP <60 despite treatment),
    SpO2 <88% on FiO2 >60% with no improvement trend,
    Serum K >6.5 mmol/L (not correcting), pH <7.1, active uncontrolled haemorrhage,
    GCS acutely falling, refractory arrhythmia.
• worsening — criteria differ by data type:
    VITALS: measurable deterioration across ≥2 consecutive readings.
      e.g. HR 90→105→118, SpO2 trending down on the SAME or increasing FiO2.
      A single abnormal vital is NOT worsening. A chronic stable abnormal vital is NOT worsening.
    LABS: a SINGLE clearly abnormal result is sufficient to call worsening — labs are drawn
      infrequently so you rarely have 2 consecutive panels to compare. Compare against the
      patient's prior lab values or documented baseline; a single value representing a
      significant change from their baseline IS worsening.
      e.g. creatinine 2.1 (baseline 0.9), K 6.1, Hb 6.2 post-op, lactate 3.8, WBC 18,000
      with new fever. If the current value is the first lab drawn, treat it as worsening if
      it is clearly outside the normal range for the clinical context.
• stable    — vitals within acceptable range, OR single abnormal vital without trend,
    OR known chronic baseline not changing. Labs at or near the patient's documented baseline.
• improving — measurable recovery: ≥2 improving vital readings, OR a lab value clearly
    moving toward normal compared to a prior result.
• resolved  — problem no longer active.

RULES:
- Always check at least vital trend OR lab trend before finalising worsening/critical.
- For labs: always pull the last 3-4 values to establish the patient's own baseline
  before deciding if the current result represents a change.
- For respiratory problems: NEVER judge SpO2 in isolation. get_vital_trend('SpO2') now
  returns the SF ratio (SpO2 / FiO2%) alongside each reading. Use the SF ratio trend,
  not raw SpO2, to assess oxygenation. If FiO2 was reduced and SF ratio is stable or
  improved, SpO2 dropping is planned weaning — mark as stable or improving, NOT worsening.
- A problem already on appropriate treatment with controlled values → stable, not worsening.
- Chronic hypertension at 150/90 with no recent change → stable.
- Tachycardia HR 105 if prior 4 readings were all 100-110 → stable (known baseline).
- If get_vital_trend returns "Unknown vital" or "No … readings found", the data is unavailable
  in stored snapshots. Do NOT conclude worsening based on clinical notes alone for a
  vital-sign-dependent problem (Fever, Tachycardia, Hypertension, Hypotension, etc.) —
  mark the problem as stable with reasoning "vital data unavailable — cannot confirm worsening".
- If a problem is already marked "resolved" by the prior stage, you need OBJECTIVE data showing
  clear deterioration (not just a note mentioning past treatment) to upgrade it to worsening.
  If vital trends are normal, keep the problem resolved or stable.
- I/O charting in ICUs is frequently incomplete or entered retrospectively. Zero urine output
  in the chart — even across several consecutive hours — does NOT reliably indicate true anuria
  or oliguria. Always treat recorded 0 ml output as "possible missed charting" unless ALL three
  of the following are true: (1) the daily total is also 0 ml, (2) clinical notes explicitly
  document anuria or oliguria, AND (3) creatinine is rising. Do NOT label AKI as worsening or
  critical on the basis of 0 ml charting alone.
- If your tool data directly contradicts the label assigned by the prior stage, TRUST YOUR
  TOOL DATA and override it. Examples:
  • Prior label "worsening" for AKI/oliguria, but get_io shows average UO > 50 ml/hr →
    downgrade to stable or improving; do NOT preserve "worsening" just because the summary says so.
  • Prior label "critical" but MAP is stable, vasopressors off, and labs improving → downgrade.
  • Prior label "worsening" for Tachycardia but HR trend is 105→98→91 → mark as improving.
  Your job is to VERIFY labels with real data — if the data contradicts the label, correct it.
- Call set_problem_statuses once — after you have gathered enough evidence."""


# ── Tool schemas (Anthropic format — GeminiLLMClient converts automatically) ──

_VITAL_TREND_TOOL = {
    "name": "get_vital_trend",
    "description": (
        "Get the last N vital-sign readings for this patient from stored snapshots. "
        "Use to assess trajectory rather than just the latest value. "
        "Returns readings newest-first with timestamps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vital_name": {
                "type": "string",
                "description": "One of: HR, BP, MAP, SpO2, RR, FiO2, Temp",
            },
            "n": {
                "type": "integer",
                "description": "Number of readings to return (default 8, max 20)",
            },
        },
        "required": ["vital_name"],
    },
}

_LAB_TREND_TOOL = {
    "name": "get_lab_trend",
    "description": (
        "Get recent values for a specific lab parameter across stored snapshots. "
        "Use to check if a value is rising, falling, or stable. "
        "Search by partial name — e.g. 'Hb', 'Creatinine', 'Potassium', 'Glucose', 'Lactate'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lab_name": {
                "type": "string",
                "description": "Lab parameter name (partial match OK): Hb, Creatinine, Potassium, Glucose, Lactate, Platelets, WBC, Sodium, Bilirubin, pH, PaO2, PaCO2",
            },
            "n": {
                "type": "integer",
                "description": "Number of recent values to return (default 6, max 12)",
            },
        },
        "required": ["lab_name"],
    },
}

_NOTES_TOOL = {
    "name": "query_patient_notes",
    "description": (
        "Semantic search over all clinician notes for this patient. "
        "Use to find clinical context about a problem's course, recent events, or treatment decisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Clinical question, e.g. 'tachycardia trend and management', 'renal function course', 'hemodynamic status'",
            },
        },
        "required": ["question"],
    },
}

_SET_STATUS_TOOL = {
    "name": "set_problem_statuses",
    "description": (
        "After reviewing trend evidence, set the final verified status for each problem. "
        "Call this ONCE when you are satisfied with the evidence gathered."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "problem_name": {
                            "type": "string",
                            "description": "Exact problem name as given",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["critical", "worsening", "stable", "improving", "resolved"],
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence: what trend data supported this status",
                        },
                    },
                    "required": ["problem_name", "status", "reasoning"],
                },
            },
        },
        "required": ["assessments"],
    },
}

_TOOLS = [_VITAL_TREND_TOOL, _LAB_TREND_TOOL, _NOTES_TOOL, _SET_STATUS_TOOL]

# ── Vital field map ────────────────────────────────────────────────────────────

_VITAL_FIELD = {
    "HR":          "daysHR",
    "BP":          "daysBP",
    "MAP":         "daysMAP",
    "SPO2":        "daysSpO2",
    "RR":          "daysRR",
    "FIO2":        "daysFiO2",
    "TEMP":        "daysTemp",
    "TEMPERATURE": "daysTemp",   # alias — model may say "Temperature"
}


# ── Tool implementations ───────────────────────────────────────────────────────

def _get_vital_trend(cpmrn: str, encounter: int, vital_name: str, n: int = 8) -> str:
    from backend.services.emr.db import get_db
    from tools.radar_sync.summary_updater import _to_ist

    n = min(max(n, 1), 20)
    field = _VITAL_FIELD.get(vital_name.strip().upper())
    if not field:
        return f"Unknown vital '{vital_name}'. Use one of: HR, BP, MAP, SpO2, RR, FiO2, Temp"

    db = get_db()
    snaps = db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.vitals": 1, "snapshot_at": 1},
    )
    snaps = sorted(snaps, key=lambda s: str(s.get("snapshot_at") or ""), reverse=True)[: n * 3]

    seen_ts = set()
    rows = []
    empty_count = 0
    for snap in snaps:
        vitals = (snap.get("chart") or {}).get("vitals") or []
        for v in vitals[:3]:  # newest readings in each snapshot
            ts_raw = v.get("timestamp")
            val = v.get(field)
            if val is None or ts_raw in seen_ts:
                continue
            seen_ts.add(ts_raw)
            # Skip placeholder readings where the monitor recorded no value
            val_str = str(val).strip()
            if val_str in ("", "/", "-", "null", "None"):
                empty_count += 1
                continue
            rows.append((ts_raw, val))
            if len(rows) >= n:
                break
        if len(rows) >= n:
            break

    # ── MAP gap-fill: derive MAP = (SBP + 2*DBP) / 3 for readings where BP was
    #    charted but MAP was not stored separately. Keeps the trend complete.
    is_map = vital_name.strip().upper() == "MAP"
    derived_bp: dict[str, str] = {}  # ts_raw → original "SBP/DBP" string
    if is_map:
        bp_field = _VITAL_FIELD["BP"]
        derived_rows: list[tuple] = []
        for snap in snaps:
            vitals = (snap.get("chart") or {}).get("vitals") or []
            for v in vitals[:3]:
                ts_raw = v.get("timestamp")
                if ts_raw in seen_ts:
                    continue  # already have a real MAP value for this timestamp
                bp_val = v.get(bp_field)
                if not bp_val:
                    continue
                bp_str = str(bp_val).strip()
                if "/" not in bp_str:
                    continue
                try:
                    sbp_s, dbp_s = bp_str.split("/", 1)
                    sbp, dbp = float(sbp_s.strip()), float(dbp_s.strip())
                    est_map = round((sbp + 2 * dbp) / 3, 1)
                    seen_ts.add(ts_raw)
                    derived_rows.append((ts_raw, est_map))
                    derived_bp[ts_raw] = bp_str
                except (ValueError, TypeError):
                    continue
        if derived_rows:
            # Merge with stored MAP rows, sort newest-first, cap at n
            combined = rows + derived_rows
            combined.sort(key=lambda x: x[0] if x[0] else "", reverse=True)
            rows = combined[:n]

    if not rows:
        gap_note = (
            f" ({empty_count} recent timestamp(s) had no recorded value — monitor may be disconnected)"
            if empty_count else ""
        )
        return f"No {vital_name} readings found in stored snapshots.{gap_note}"

    # For SpO2, fetch FiO2 at the same timestamps and compute SF ratio.
    # This prevents the model from calling SpO2 worsening when FiO2 was intentionally reduced.
    is_spo2 = vital_name.upper() in ("SPO2", "SPO2(FIO2)", "SP02")
    fio2_by_ts: dict = {}
    if is_spo2:
        fio2_field = _VITAL_FIELD["FIO2"]
        for snap in snaps:
            vitals = (snap.get("chart") or {}).get("vitals") or []
            for v in vitals[:3]:
                ts_raw_f = v.get("timestamp")
                fio2_val = v.get(fio2_field)
                if ts_raw_f and fio2_val is not None:
                    try:
                        fio2_by_ts[ts_raw_f] = float(fio2_val)
                    except (TypeError, ValueError):
                        pass

    lines = [f"SpO2 + FiO2 trend (newest first):" if is_spo2 else f"{vital_name} trend (newest first):"]
    for ts_raw, val in rows:
        if is_spo2:
            fio2 = fio2_by_ts.get(ts_raw)
            if fio2 and fio2 > 0:
                try:
                    sf = float(str(val)) / (fio2 / 100.0)
                    lines.append(f"  [{_to_ist(ts_raw)}] SpO2 {val}%  FiO2 {fio2:.0f}%  SF ratio {sf:.0f}")
                except (TypeError, ValueError):
                    lines.append(f"  [{_to_ist(ts_raw)}] SpO2 {val}%  FiO2 {fio2:.0f}%")
            else:
                lines.append(f"  [{_to_ist(ts_raw)}] SpO2 {val}%")
        elif is_map and ts_raw in derived_bp:
            # Derived from BP — annotate so the model knows it's estimated
            lines.append(f"  [{_to_ist(ts_raw)}] {val}  (est. from BP {derived_bp[ts_raw]})")
        else:
            lines.append(f"  [{_to_ist(ts_raw)}] {val}")

    if is_spo2:
        lines.append(
            "  Note: SF ratio = SpO2 / (FiO2/100). "
            "If FiO2 was reduced and SF ratio is maintained or improved, "
            "this is planned weaning — NOT worsening oxygenation."
        )
    if empty_count:
        lines.append(f"  Note: {empty_count} additional timestamp(s) had no recorded value (monitor gap).")
    return "\n".join(lines)


def _get_lab_trend(cpmrn: str, encounter: int, lab_name: str, n: int = 6) -> str:
    from backend.services.emr.db import get_db
    from tools.radar_sync.summary_updater import _to_ist

    n = min(max(n, 1), 12)
    db = get_db()
    snaps = db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.documents": 1, "snapshot_at": 1},
    )
    snaps = sorted(snaps, key=lambda s: str(s.get("snapshot_at") or ""), reverse=True)[: n * 4]

    search = lab_name.lower()
    # Lab attribute aliases for common short names
    _ATTR_ALIASES = {
        "hb": ["hb", "hemoglobin", "haemoglobin"],
        "k":  ["potassium", "k"],
        "na": ["sodium", "na"],
        "cr": ["creatinine", "cr"],
        "wbc": ["total count", "wbc", "white blood cell"],
        "plt": ["platelets", "plt"],
        # ABG panels store lactate as "Lactic" not "Lactate" — alias both directions
        "lactate": ["lactic", "lactate"],
        "lactic":  ["lactic", "lactate"],
        # "ABG"/"VBG" as a panel-level query: return pH as the representative value.
        # pH is in _BLOOD_GAS_ALLOWED so the gas-panel filter won't block it.
        # This allows next_check {lab_name: "ABG"} to auto-fetch and confirm the test was done.
        "abg": ["ph", "pao2", "paco2"],
        "vbg": ["ph", "paco2"],
    }
    aliases = _ATTR_ALIASES.get(search, [search])

    # Only a subset of attributes from ABG/VBG panels are clinically valid.
    # Everything else (ionized Ca, Na, K, glucose from point-of-care gas machines)
    # must be sourced from dedicated lab panels, not gas panels.
    # "Gas panel (BldA)" / "Gas panel (BldV)" are Radar's ABG/VBG panel names.
    _BLOOD_GAS_KEYWORDS = ("abg", "vbg", "arterial blood gas", "venous blood gas", "blood gas", "gas panel")
    _BLOOD_GAS_ALLOWED = {
        "ph", "pao2", "paco2", "bicarb", "hco3",
        "lactic", "lactate", "be", "base excess",
        "spo2", "fio2", "cso2",
    }
    _is_allowed_from_gas = bool(set(aliases) & _BLOOD_GAS_ALLOWED)

    seen_ts = set()
    rows = []
    for snap in snaps:
        docs = [(snap.get("chart") or {}).get("documents") or []]
        for doc_list in docs:
            for doc in doc_list:
                if doc.get("category") != "labs":
                    continue
                ts_raw = doc.get("reportedAt")
                if ts_raw in seen_ts:
                    continue
                # Search panel name
                panel_name = (doc.get("name") or "").lower()
                # For blood gas panels, only accept the allowed subset of attributes.
                # Ionized Ca, Na, K, glucose etc. must come from dedicated lab panels.
                if any(bg in panel_name for bg in _BLOOD_GAS_KEYWORDS) and not _is_allowed_from_gas:
                    continue
                attrs = doc.get("attributes") or {}
                # Look for matching attribute key
                for attr_key, attr_val in attrs.items():
                    if not isinstance(attr_val, dict):
                        continue
                    ak_lower = attr_key.lower()
                    # Word-start boundary prevents "ph" matching "neutrophils".
                    # Short aliases (≤ 2 chars, e.g. "ph", "hb") also require a word-end
                    # boundary so "ph" doesn't match "phosphate". Longer aliases only
                    # need the start boundary (e.g. "cr" correctly prefix-matches "creatinine").
                    def _al_matches(al: str, ak: str) -> bool:
                        pat = (r'(?<!\w)' + re.escape(al) + r'(?!\w)'
                               if len(al) <= 2 else
                               r'(?<!\w)' + re.escape(al))
                        return bool(re.search(pat, ak))
                    if any(_al_matches(al, ak_lower) for al in aliases):
                        val = attr_val.get("value")
                        unit = attr_val.get("unit", "")
                        if val is not None:
                            seen_ts.add(ts_raw)
                            rows.append((ts_raw, attr_key, val, unit))
                            break

    if not rows:
        return f"No '{lab_name}' values found in stored snapshots."

    rows_sorted = sorted(rows, key=lambda r: str(r[0]), reverse=True)[:n]
    lines = [f"{lab_name} trend (newest first):"]
    for ts_raw, attr_key, val, unit in rows_sorted:
        lines.append(f"  [{_to_ist(ts_raw)}] {attr_key} = {val} {unit}".rstrip())
    return "\n".join(lines)


def _amt(obj: Any) -> float:
    """Safely extract a numeric amount from an I/O sub-object or scalar."""
    if obj is None:
        return 0.0
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        try:
            return float(obj.get("amount") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(obj)
    except (TypeError, ValueError):
        return 0.0


def _get_latest_vital_ts(cpmrn: str, encounter: int):
    """
    Return the datetime of the most recent vital reading for this patient,
    or None if no vitals are found. Used for vital staleness checks.
    """
    from backend.services.emr.db import get_db
    import pandas as pd

    db = get_db()
    snaps = db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.vitals": 1, "snapshot_at": 1},
    )
    snaps = sorted(snaps, key=lambda s: str(s.get("snapshot_at") or ""), reverse=True)[:5]

    latest_ts = None
    for snap in snaps:
        vitals = (snap.get("chart") or {}).get("vitals") or []
        for v in vitals[:3]:
            ts_raw = v.get("timestamp")
            if not ts_raw:
                continue
            # Skip placeholder-only readings
            has_value = any(
                v.get(f) and str(v.get(f)).strip() not in ("", "/", "-", "null", "None")
                for f in ("daysHR", "daysBP", "daysMAP", "daysSpO2", "daysRR")
            )
            if not has_value:
                continue
            try:
                ts = pd.to_datetime(ts_raw, utc=True).to_pydatetime()
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
            except Exception:
                pass
        if latest_ts:
            break

    return latest_ts


def _get_io(cpmrn: str, encounter: int, n_hours: int = 12) -> str:
    """
    Return a per-hour fluid balance table from chart.io in the latest snapshot.

    Intake columns:  IV meds (infusion + bolus)  |  feeds  |  others
    Output columns:  urine  |  drain(s)  |  dialysis  |  other output
    Net = total intake − total output per hour.

    Stool is listed as episodes (not ml) so excluded from net balance.

    Returns newest-first, up to n_hours rows, plus a cumulative summary line.
    """
    from backend.services.emr.db import get_db

    n_hours = min(max(n_hours, 1), 48)
    db = get_db()
    snap = db.snapshots.find_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.io": 1, "snapshot_at": 1},
        sort=[("snapshot_at", -1)],
    )
    if not snap:
        return "No snapshot found for this patient."

    io_data = (snap.get("chart") or {}).get("io") or {}
    days: list = io_data.get("days") or []
    if not days:
        return "No fluid balance (I/O) data found in the latest snapshot."

    # ── Aggregate per (day_num, hour_name) ───────────────────────────────────
    HourRow = dict  # keys: day, hour, iv, feeds, in_other, urine, drain, dialysis, out_other, stool_ep

    hour_map: dict[tuple[int, int], HourRow] = {}

    for day in days:
        day_num = day.get("dayNumber", 0)
        for hour_obj in (day.get("hours") or []):
            hour_name = hour_obj.get("hourName", 0)
            key = (day_num, hour_name)
            if key not in hour_map:
                hour_map[key] = dict(
                    day=day_num, hour=hour_name,
                    iv=0.0, feeds=0.0, in_other=0.0,
                    urine=0.0, drain=0.0, dialysis=0.0, out_other=0.0,
                    stool_ep=0,
                    drain_names=[],
                )
            row = hour_map[key]

            for minute in (hour_obj.get("minutes") or []):
                intake = minute.get("intake") or {}
                output = minute.get("output") or {}

                # Intake: IV meds
                meds = intake.get("meds") or {}
                for inf in (meds.get("infusion") or []):
                    row["iv"] += _amt(inf)
                for bol in (meds.get("bolus") or []):
                    row["iv"] += _amt(bol)

                # Intake: feeds, others
                row["feeds"]    += _amt(intake.get("feeds"))
                row["in_other"] += _amt(intake.get("others"))

                # Output: urine
                row["urine"] += _amt(output.get("urine"))

                # Output: named drains
                for drain in (output.get("drain") or []):
                    a = _amt(drain)
                    row["drain"] += a
                    name = drain.get("name") or drain.get("site") or "drain"
                    if name and name not in row["drain_names"]:
                        row["drain_names"].append(name)

                # Output: dialysis
                for d in (output.get("dialysis") or []):
                    row["dialysis"] += _amt(d)

                # Output: other
                row["out_other"] += _amt(output.get("others"))

                # Stool — count episodes only (amount is a string like "3")
                stool = output.get("stool") or {}
                if isinstance(stool, dict) and stool.get("amount") not in (None, "", "0"):
                    try:
                        row["stool_ep"] += int(float(stool["amount"]))
                    except (TypeError, ValueError):
                        pass

    if not hour_map:
        return "No I/O entries found."

    # ── Per-day totals (always computed across ALL days, regardless of n_hours) ──
    day_totals: dict[int, dict] = {}
    for (day_num, _), row in hour_map.items():
        if day_num not in day_totals:
            day_totals[day_num] = dict(iv=0.0, feeds=0.0, in_other=0.0,
                                       urine=0.0, drain=0.0, dialysis=0.0,
                                       out_other=0.0, hours_with_data=0)
        dt = day_totals[day_num]
        t_in  = row["iv"] + row["feeds"] + row["in_other"]
        t_out = row["urine"] + row["drain"] + row["dialysis"] + row["out_other"]
        dt["iv"]        += row["iv"]
        dt["feeds"]     += row["feeds"]
        dt["in_other"]  += row["in_other"]
        dt["urine"]     += row["urine"]
        dt["drain"]     += row["drain"]
        dt["dialysis"]  += row["dialysis"]
        dt["out_other"] += row["out_other"]
        if t_in > 0 or t_out > 0:
            dt["hours_with_data"] += 1

    lines = ["Fluid balance (I/O):"]

    # Daily summary section — always shown first
    lines.append("  ── Daily totals (all ICU days) ──")
    for day_num in sorted(day_totals.keys(), reverse=True):
        dt = day_totals[day_num]
        d_in  = dt["iv"] + dt["feeds"] + dt["in_other"]
        d_out = dt["urine"] + dt["drain"] + dt["dialysis"] + dt["out_other"]
        d_net = d_in - d_out
        n_hrs = dt["hours_with_data"]

        # Detect daily-batch-charting: day has meaningful volume but only 1-2 hours
        # recorded — i.e. a nurse entered a cumulative total rather than hourly values
        batch_note = ""
        total_hrs_in_day = sum(1 for (d, _) in hour_map if d == day_num)
        if d_out > 0 and n_hrs <= 2 and total_hrs_in_day > 4:
            batch_note = "  ⚠ likely daily batch entry"

        lines.append(
            f"  Day {day_num}:  In {d_in:.0f} ml  |  UO {dt['urine']:.0f} ml"
            + (f"  Drain {dt['drain']:.0f} ml" if dt["drain"] else "")
            + (f"  Dialysis {dt['dialysis']:.0f} ml" if dt["dialysis"] else "")
            + f"  |  Net {d_net:+.0f} ml"
            + batch_note
        )

    # ── Hourly breakdown for the requested window ─────────────────────────────
    sorted_rows = sorted(hour_map.values(), key=lambda r: (r["day"], r["hour"]), reverse=True)
    recent = sorted_rows[:n_hours]

    lines.append(f"\n  ── Hourly detail — last {len(recent)} hour(s), newest first ──")
    lines.append(
        f"  {'Hour':<12}  {'IN: IV':>8}  {'Feeds':>6}  {'Other':>6}  "
        f"{'OUT: Urine':>10}  {'Drain':>7}  {'Dialysis':>8}  {'Other':>6}  {'Net':>7}"
    )
    lines.append("  " + "-" * 86)

    total_in = total_out = 0.0
    for r in recent:
        t_in  = r["iv"] + r["feeds"] + r["in_other"]
        t_out = r["urine"] + r["drain"] + r["dialysis"] + r["out_other"]
        net   = t_in - t_out
        total_in  += t_in
        total_out += t_out

        extras = []
        if r["drain_names"]:
            extras.append(f"drain: {', '.join(r['drain_names'])}")
        if r["stool_ep"]:
            extras.append(f"stool ×{r['stool_ep']}")
        extra_str = f"  ({'; '.join(extras)})" if extras else ""

        lines.append(
            f"  Day{r['day']} Hr{r['hour']:02d}:00"
            f"  {r['iv']:>8.0f}  {r['feeds']:>6.0f}  {r['in_other']:>6.0f}"
            f"  {r['urine']:>10.0f}  {r['drain']:>7.0f}  {r['dialysis']:>8.0f}  {r['out_other']:>6.0f}"
            f"  {net:>+7.0f}"
            + extra_str
        )

    net_total = total_in - total_out
    uo_hours  = [r["urine"] for r in recent]
    avg_uo    = sum(uo_hours) / len(uo_hours) if uo_hours else 0
    zero_uo_h = sum(1 for u in uo_hours if u == 0)

    lines.append("  " + "-" * 86)
    lines.append(
        f"  TOTAL ({len(recent)} hr)"
        f"  In: {total_in:.0f} ml  |  Out: {total_out:.0f} ml  |  Net: {net_total:+.0f} ml"
        f"  |  Avg UO: {avg_uo:.1f} ml/hr"
    )

    # Batch-charting warning — suppress false anuria when hourly window shows
    # zeros but a recent day's total shows meaningful output.
    # Look for the most recent day that actually has recorded UO (may not be today).
    most_recent_day_with_uo = next(
        (d for d in sorted(day_totals.keys(), reverse=True) if day_totals[d]["urine"] > 0),
        None,
    )
    if zero_uo_h == len(recent) and most_recent_day_with_uo:
        day_uo = day_totals[most_recent_day_with_uo]["urine"]
        lines.append(
            f"  ℹ Hourly window shows 0 ml UO but Day {most_recent_day_with_uo} total = {day_uo:.0f} ml. "
            f"I/O is likely charted as a daily batch total — do NOT interpret as anuria."
        )
    elif zero_uo_h:
        lines.append(f"  ⚠ {zero_uo_h} hour(s) with 0 ml urine recorded.")

    return "\n".join(lines)


def _run_tool(name: str, args: dict, cpmrn: str, encounter: int) -> str:
    try:
        if name == "get_vital_trend":
            return _get_vital_trend(cpmrn, encounter, args["vital_name"], int(args.get("n", 8)))
        if name == "get_lab_trend":
            return _get_lab_trend(cpmrn, encounter, args["lab_name"], int(args.get("n", 6)))
        if name == "get_io":
            return _get_io(cpmrn, encounter, int(args.get("n_hours", 12)))
        if name == "query_patient_notes":
            from tools.radar_sync.query_notes import query_patient_notes
            return query_patient_notes(cpmrn, encounter, args["question"])
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception("status_classifier: tool %s failed", name)
        return f"Tool error: {e}"


# ── Main entry point ───────────────────────────────────────────────────────────

def classify_statuses(cpmrn: str, encounter: int, structured_summary: dict) -> dict:
    """
    Run a reasoning model over the draft PatientSummary.
    Only problems currently labelled worsening/critical are reviewed.
    Returns an updated structured_summary with verified statuses.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    problems: list[dict] = structured_summary.get("problems", [])
    candidates = [p for p in problems if p.get("status") in ("worsening", "critical")]

    if not candidates:
        logger.info("status_classifier: no worsening/critical problems to review for %s", cpmrn)
        return structured_summary

    logger.info(
        "status_classifier: reviewing %d problem(s) for %s — %s",
        len(candidates), cpmrn, [p["name"] for p in candidates],
    )

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from backend.services.emr.db import get_db as _get_db
    from tools.radar_sync.react_tracer import ReActTracer

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_CLASSIFIER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="status_classifier", db=_get_db())

    # ── Pre-fetch trend data for worsening/critical problems (Fix 4) ────────────
    # Inject vital/lab trends upfront so the model can verify statuses in fewer
    # rounds without calling get_vital_trend / get_lab_trend for common cases.
    prefetch_lines: list[str] = [
        "== PRE-FETCHED TREND DATA ==",
        "Use this data to verify problem statuses. Only call tools for additional readings.",
        "",
    ]
    # Vital keys commonly relevant per problem type (best-effort heuristic)
    _PROBLEM_VITALS = {
        "tachycardia": ["HR"], "bradycardia": ["HR"],
        "hypertension": ["BP", "MAP"], "hypotension": ["BP", "MAP"],
        "respiratory": ["SpO2", "RR", "FiO2"], "ards": ["SpO2", "FiO2"],
        "sepsis": ["HR", "BP", "MAP", "Temp"], "shock": ["HR", "MAP", "BP"],
        "fever": ["Temp"], "oliguria": ["HR"], "aki": ["HR"],
    }
    fetched_vitals: set[str] = set()
    for p in candidates:
        name_lower = p["name"].lower()
        vitals_to_fetch = []
        for kw, vitals in _PROBLEM_VITALS.items():
            if kw in name_lower:
                vitals_to_fetch.extend(vitals)
        # Always fetch HR and BP for any worsening/critical problem
        vitals_to_fetch = list(dict.fromkeys(["HR", "BP"] + vitals_to_fetch))

        prefetch_lines.append(f"Problem: {p['name']} [{p['status'].upper()}]")
        for vital in vitals_to_fetch[:4]:  # cap at 4 vitals per problem
            if vital not in fetched_vitals:
                try:
                    trend = _get_vital_trend(cpmrn, encounter, vital, n=8)
                    prefetch_lines.append(f"  {trend}")
                    fetched_vitals.add(vital)
                except Exception:
                    pass

        # Fetch relevant labs based on problem keywords
        labs_to_fetch: list[str] = []
        if any(kw in name_lower for kw in ("aki", "renal", "kidney", "creatinine")):
            labs_to_fetch.append("Creatinine")
        if any(kw in name_lower for kw in ("hypokal", "hyperkal", "potassium", "k+")):
            labs_to_fetch.append("Potassium")
        if any(kw in name_lower for kw in ("anemia", "anaemia", "transfusion", "hb", "haemoglobin")):
            labs_to_fetch.append("Hb")
        if any(kw in name_lower for kw in ("lactate", "shock", "sepsis")):
            labs_to_fetch.append("Lactate")
        if any(kw in name_lower for kw in ("sodium", "hyponatr", "hypernatr")):
            labs_to_fetch.append("Sodium")
        if any(kw in name_lower for kw in ("glucose", "hyperglycemia", "hypoglycemia")):
            labs_to_fetch.append("Glucose")
        for lab in labs_to_fetch[:3]:
            try:
                trend = _get_lab_trend(cpmrn, encounter, lab, n=4)
                prefetch_lines.append(f"  {trend}")
            except Exception:
                pass
        prefetch_lines.append("")

    prefetch_block = "\n".join(prefetch_lines)

    # Build the initial user message
    problem_block = "\n".join(
        f"  {i+1}. {p['name']} [{p['status'].upper()}]\n"
        f"     Current state: {p.get('current_state', 'not specified')}\n"
        f"     Management: {p.get('management', 'not specified')}"
        for i, p in enumerate(candidates)
    )

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n\n"
        f"The following problems were preliminarily labelled WORSENING or CRITICAL "
        f"from a single chart snapshot. Please verify each by checking trend data.\n\n"
        f"{prefetch_block}\n"
        f"Problems to verify:\n{problem_block}\n\n"
        f"The pre-fetched data above covers common vitals/labs. Use get_vital_trend, "
        f"get_lab_trend, or query_patient_notes for anything not already shown. "
        f"Then call set_problem_statuses with your verified assessments."
    )

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    final_assessments: list[dict] = []

    for round_num in range(_MAX_TOOL_ROUNDS):
        tracer.start_round(round_num)
        try:
            force = round_num < _MAX_TOOL_ROUNDS - 1  # last round: don't force tool
            resp = client.create_message(
                messages=messages,
                tools=_TOOLS,
                system=_SYSTEM,
                max_tokens=4096,
                force_tool=force,
                thinking_budget=_THINKING_BUDGET,
            )
        except Exception:
            logger.exception("status_classifier: LLM call failed on round %d for %s", round_num, cpmrn)
            tracer.end_round()
            break

        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.thinking_tokens)

        # Collect assistant content for history
        assistant_parts: list[dict] = []
        tool_calls: list[dict] = []

        for block in resp.content:
            if block.type == "thinking":
                tracer.log_thinking(block.text)
                # thinking blocks are not added to message history
            elif block.type == "tool_use":
                tool_calls.append(block)
                tracer.log_tool_call(block.name, block.input)
                part: dict = {
                    "type": "tool_use",
                    "name": block.name,
                    "input": block.input,
                    "id": block.id,
                }
                if block.thought_signature:
                    part["thought_signature"] = block.thought_signature
                assistant_parts.append(part)
            else:
                tracer.log_text(block.text)
                assistant_parts.append({"type": "text", "text": block.text})

        if assistant_parts:
            messages.append({"role": "assistant", "content": assistant_parts})

        tracer.end_round()

        if not tool_calls:
            break

        # Execute tools
        tool_results: list[dict] = []
        for tc in tool_calls:
            if tc.name == "set_problem_statuses":
                final_assessments = tc.input.get("assessments", [])
                logger.info(
                    "status_classifier: final statuses for %s: %s",
                    cpmrn,
                    [(a["problem_name"], a["status"]) for a in final_assessments],
                )
                tool_results.append({
                    "type": "tool_result",
                    "name": tc.name,
                    "id": tc.id,
                    "content": "Status assessments recorded.",
                })
            else:
                result_text = _run_tool(tc.name, tc.input, cpmrn, encounter)
                tracer.log_tool_result(tc.name, result_text)
                logger.info("status_classifier: tool %s → %d chars", tc.name, len(result_text))
                tool_results.append({
                    "type": "tool_result",
                    "name": tc.name,
                    "id": tc.id,
                    "content": result_text,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Stop once set_problem_statuses was called
        if final_assessments:
            break

    # Apply verified statuses back to the summary
    if not final_assessments:
        logger.warning("status_classifier: no assessments returned for %s — keeping draft statuses", cpmrn)
        tracer.save(final_output={"error": "no_assessments"})
        return structured_summary

    name_to_assessment = {a["problem_name"]: a for a in final_assessments}
    updated_problems = []
    for p in problems:
        assessment = name_to_assessment.get(p["name"])
        if assessment:
            old_status = p["status"]
            new_status = assessment["status"]
            if old_status != new_status:
                logger.info(
                    "status_classifier: %s '%s' %s → %s (%s)",
                    cpmrn, p["name"], old_status, new_status, assessment.get("reasoning", ""),
                )
            p = {**p, "status": new_status, "classifier_reasoning": assessment.get("reasoning", "")}
        updated_problems.append(p)

    result = {**structured_summary, "problems": updated_problems}
    tracer.save(final_output={
        "assessments": [(a["problem_name"], a["status"]) for a in final_assessments],
    })
    return result
