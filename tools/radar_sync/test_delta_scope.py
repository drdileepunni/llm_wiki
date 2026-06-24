"""
Unit tests for delta_scope.py

Usage (from repo root):
    source .venv/bin/activate && python -m tools.radar_sync.test_delta_scope
"""
from __future__ import annotations
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.radar_sync.delta_scope import (
    classify_delta,
    delta_summary_line,
    relevant_categories,
    blast_radius_problems,
)

_PASS = 0
_FAIL = 0


def _check(label: str, actual, expected):
    global _PASS, _FAIL
    if actual == expected:
        print(f"  PASS  {label}")
        _PASS += 1
    else:
        print(f"  FAIL  {label}")
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")
        _FAIL += 1


def _check_in(label: str, needle, container):
    global _PASS, _FAIL
    if needle in container:
        print(f"  PASS  {label}")
        _PASS += 1
    else:
        print(f"  FAIL  {label}")
        print(f"        '{needle}' not in {container!r}")
        _FAIL += 1


def _check_not_in(label: str, needle, container):
    global _PASS, _FAIL
    if needle not in container:
        print(f"  PASS  {label}")
        _PASS += 1
    else:
        print(f"  FAIL  {label}")
        print(f"        '{needle}' unexpectedly in {container!r}")
        _FAIL += 1


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _lab(name, **attrs):
    return {"name": name, "category": "labs", "reportedAt": "2026-06-24T10:00:00Z",
            "attributes": {k: {"value": v, "unit": ""} for k, v in attrs.items()}}

def _vital(**kw):
    return {"timestamp": "2026-06-24T10:00:00Z", **{f"days{k.upper()}": v for k, v in kw.items()}}

def _note(text="Plan: continue current management."):
    return {"timestamp": "2026-06-24T10:00:00Z", "note_type": "progress", "author": "Dr X", "text": text}


GLUCOSE_DELTA = {
    "new_labs": [_lab("Random Blood Glucose", Glucose=280)],
    "new_vitals": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

SODIUM_DELTA = {
    "new_labs": [_lab("Serum Electrolytes", Sodium=128, Potassium=4.2)],
    "new_vitals": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

CREATININE_DELTA = {
    "new_labs": [_lab("Renal Function Test", Creatinine=3.2, Urea=80)],
    "new_vitals": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

LACTATE_DELTA = {
    "new_labs": [_lab("Lactate", Lactate=4.8)],
    "new_vitals": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

VITALS_ONLY_DELTA = {
    "new_vitals": [_vital(SpO2=84, RR=26, HR=110)],
    "new_labs": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

NOTE_DELTA = {
    "new_notes": [_note()],
    "new_vitals": [], "new_labs": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

REPORT_DELTA = {
    "new_report_findings": [{"label": "Cardiomegaly", "severity": "notable"}],
    "new_vitals": [], "new_labs": [], "new_notes": [], "delta_orders": {}, "io_last_24h": {},
}

PANEL_DELTA = {
    "new_labs": [_lab("ABG", pH=7.28, pCO2=55, HCO3=18, Lactate=3.1)],
    "new_vitals": [_vital(SpO2=88, RR=28)],
    "new_notes": [], "delta_orders": {}, "io_last_24h": {}, "new_report_findings": [],
}

EMPTY_DELTA = {
    "new_vitals": [], "new_labs": [], "new_notes": [], "delta_orders": {},
    "io_last_24h": {}, "new_report_findings": [],
}

PROBLEMS = [
    {"name": "Hypotension",           "status": "worsening", "current_state": "MAP 58", "management": "vasopressors"},
    {"name": "AKI",                   "status": "worsening", "current_state": "Cr 3.2", "management": "fluids"},
    {"name": "Respiratory Failure",   "status": "critical",  "current_state": "SpO2 84%", "management": "O2 therapy"},
    {"name": "Hyperglycemia",         "status": "worsening", "current_state": "Glucose 280", "management": "sliding scale"},
    {"name": "Altered Consciousness", "status": "stable",    "current_state": "GCS 14", "management": "monitoring"},
]


# ── Tests: classify_delta ─────────────────────────────────────────────────────

print("\n=== classify_delta ===")

scope = classify_delta(GLUCOSE_DELTA)
_check("glucose-only: types",       scope.types,       {"lab"})
_check("glucose-only: is_wildcard", scope.is_wildcard, False)
_check("glucose-only: lab_names",   scope.lab_names,   ["Random Blood Glucose"])

scope = classify_delta(NOTE_DELTA)
_check("note: is_wildcard",         scope.is_wildcard, True)
_check("note: types includes note", "note" in scope.types, True)
_check("note: categories=*",        scope.categories,  {"*"})

scope = classify_delta(REPORT_DELTA)
_check("report: is_wildcard",       scope.is_wildcard, True)
_check("report: categories=*",      scope.categories,  {"*"})

scope = classify_delta(VITALS_ONLY_DELTA)
_check("vitals: types",             scope.types,       {"vital"})
_check("vitals: is_wildcard",       scope.is_wildcard, False)
_check("vitals: spo2 in params",    "spo2" in scope.vital_params, True)

scope = classify_delta(EMPTY_DELTA)
_check("empty: types",              scope.types,       set())
_check("empty: is_wildcard",        scope.is_wildcard, False)


# ── Tests: relevant_categories ────────────────────────────────────────────────

print("\n=== relevant_categories ===")

_check("glucose: no categories",    relevant_categories(GLUCOSE_DELTA),  set())
_check_in("sodium: neuro",          "neuro",       relevant_categories(SODIUM_DELTA))
_check_in("creatinine: renal",      "renal",       relevant_categories(CREATININE_DELTA))
_check_in("lactate: respiratory",   "respiratory", relevant_categories(LACTATE_DELTA))
_check_in("lactate: renal",         "renal",       relevant_categories(LACTATE_DELTA))
_check_in("lactate: causal",        "causal",      relevant_categories(LACTATE_DELTA))
_check_in("vitals SpO2: resp",      "respiratory", relevant_categories(VITALS_ONLY_DELTA))
_check("note: wildcard *",          relevant_categories(NOTE_DELTA),      {"*"})
_check("report: wildcard *",        relevant_categories(REPORT_DELTA),    {"*"})
_check_in("panel (ABG): resp",      "respiratory", relevant_categories(PANEL_DELTA))
_check_in("panel (ABG): renal",     "renal",       relevant_categories(PANEL_DELTA))


# ── Tests: delta_summary_line ─────────────────────────────────────────────────

print("\n=== delta_summary_line ===")

line = delta_summary_line(GLUCOSE_DELTA)
_check_in("glucose: DELTA THIS RUN",        "DELTA THIS RUN",             line)
_check_in("glucose: focus instruction",     "Focus assessment",           line)
_check_not_in("glucose: not wildcard",      "wildcard",                   line)

line = delta_summary_line(NOTE_DELTA)
_check_in("note: wildcard label",           "wildcard",                   line)

line = delta_summary_line(CREATININE_DELTA)
_check_in("creatinine: lab count",          "1 new lab",                  line)
_check_in("creatinine: no new vitals",      "No new vitals",              line)

line = delta_summary_line(EMPTY_DELTA)
_check_in("empty: no new structured data",  "no new structured data",     line)


# ── Tests: blast_radius_problems ──────────────────────────────────────────────

print("\n=== blast_radius_problems ===")

# Creatinine → renal → AKI is hot; respiratory failure is cold; hyperglycemia cold
hot = blast_radius_problems(CREATININE_DELTA, PROBLEMS)
hot_names = {p["name"] for p in hot}
_check_in("creatinine: AKI is hot",                 "AKI",                 hot_names)
_check_not_in("creatinine: Respiratory cold",        "Respiratory Failure", hot_names)
_check_not_in("creatinine: Hyperglycemia cold",      "Hyperglycemia",       hot_names)

# SpO2 vitals → respiratory → Respiratory Failure is hot; AKI cold
hot = blast_radius_problems(VITALS_ONLY_DELTA, PROBLEMS)
hot_names = {p["name"] for p in hot}
_check_in("vitals SpO2: Resp Failure hot",           "Respiratory Failure", hot_names)
_check_not_in("vitals SpO2: AKI cold",               "AKI",                 hot_names)

# Glucose lab → Hyperglycemia is hot via lab-problem map
hot = blast_radius_problems(GLUCOSE_DELTA, PROBLEMS)
hot_names = {p["name"] for p in hot}
_check_in("glucose: Hyperglycemia hot",              "Hyperglycemia",       hot_names)

# Note (wildcard) → all problems
hot = blast_radius_problems(NOTE_DELTA, PROBLEMS)
_check("note wildcard: all problems returned", len(hot), len(PROBLEMS))

# Panel with lactate → respiratory + renal + causal → most hot except Hypotension (hemodynamic)
hot = blast_radius_problems(PANEL_DELTA, PROBLEMS)
hot_names = {p["name"] for p in hot}
_check_in("panel lactate: AKI hot",                  "AKI",                 hot_names)
_check_in("panel ABG: Resp Failure hot",             "Respiratory Failure", hot_names)

# Empty delta → no hot problems
hot = blast_radius_problems(EMPTY_DELTA, PROBLEMS)
_check("empty delta: no hot problems",               len(hot), 0)


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"Results: {_PASS} passed, {_FAIL} failed")
if _FAIL:
    sys.exit(1)
