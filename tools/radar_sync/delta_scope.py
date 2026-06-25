"""
Delta blast-radius analysis for the CDS pipeline.

Classifies an extracted delta dict (from delta_extractor.extract_delta) into a
DeltaScope that downstream stages use to:
  A) Steer LLM attention (delta_summary_line)
  B) Filter rule-block injection (relevant_categories)
  C) Prune the problem list (blast_radius_problems)

Load-bearing safety rule: any delta containing new_notes or new_report_findings
is a WILDCARD — unstructured text can introduce new problems or plan changes that
keyword matching cannot anticipate. Wildcard deltas always produce full runs.

Reuses:
  clinical_rule_blocks._CATEGORIES  — keyword→block map (avoids duplication)
  clinical_rule_blocks._problem_text — haystack builder
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ── Cross-link augmentation map ────────────────────────────────────────────────
# Lab analytes whose blast radius spans categories that the primary keyword
# matching might miss. Biased toward inclusion — over-including costs minor
# attention; under-including risks missing a safety rule.
#
# Key: substring matched against lab document names (lowercased)
# Value: set of clinical_rule_blocks category names to add
_LAB_CROSSLINKS: dict[str, set[str]] = {
    "lactate":     {"respiratory", "renal", "causal"},  # perfusion
    "lactic":      {"respiratory", "renal", "causal"},
    "creatinine":  {"renal"},
    "urea":        {"renal"},
    "bun":         {"renal"},
    "potassium":   {"neuro"},        # cardiac arrhythmia / neuro effects
    "magnesium":   {"neuro"},
    "calcium":     {"neuro"},
    "ph":          {"respiratory", "renal"},
    "pco2":        {"respiratory"},
    "hco3":        {"renal", "respiratory"},
    "bicarbonate": {"renal", "respiratory"},
    "sodium":      {"neuro"},        # hypo/hypernatraemia → encephalopathy
    "glucose":     set(),            # handled by Gate 2.6; no additional category
}

# Vital parameter names → direct problem-name keyword matching.
# Used by blast_radius_problems to match problems like "Tachycardia" when HR is
# abnormal, independent of category-based rule injection.
# Biased toward inclusion — short, unambiguous root words only.
_VITAL_PROBLEM_KEYWORDS: dict[str, list[str]] = {
    "hr":   ["tachycardia", "bradycardia", "arrhythmia", "cardiac", "heart rate"],
    "spo2": ["desaturation", "hypoxia", "respiratory", "oxygen"],
    "rr":   ["respiratory", "tachypnea", "breathe", "ventilat"],
    "bp":   ["hypertension", "hypotension", "pressure"],
    "map":  ["hypotension", "pressure", "perfusion"],
    "temp": ["fever", "pyrexia", "hypothermia", "sepsis", "febrile"],
    "gcs":  ["gcs", "consciousness", "encephalopathy"],
    "fio2": ["respiratory", "oxygen", "ventilat"],
}

# Vital parameter names (from chart vital dicts) → category mappings
_VITAL_CROSSLINKS: dict[str, set[str]] = {
    "spo2":   {"respiratory"},
    "rr":     {"respiratory"},
    "fio2":   {"respiratory"},
    "gcs":    {"neuro"},
    "hr":     set(),     # tachycardia/bradycardia: handled by core _SYSTEM, no category block
    "map":    set(),
    "bp":     set(),
    "temp":   set(),
}


@dataclass
class DeltaScope:
    types: set[str] = field(default_factory=set)             # {"lab","vital","io","note","report"}
    is_wildcard: bool = False                                  # notes or report findings present
    lab_names: list[str] = field(default_factory=list)       # new_labs doc names
    _lab_attr_keys: list[list[str]] = field(default_factory=list)  # attribute keys per lab doc
    vital_params: set[str] = field(default_factory=set)      # vital field names present
    categories: set[str] = field(default_factory=set)        # matched category names ({"*"} = all)


def classify_delta(delta: dict) -> DeltaScope:
    """
    Analyse the delta dict and return a DeltaScope.

    delta keys: new_vitals, new_labs, new_notes, delta_orders,
                io_last_24h, new_report_findings
    """
    new_vitals  = delta.get("new_vitals")  or []
    new_labs    = delta.get("new_labs")    or []
    new_notes   = delta.get("new_notes")   or []
    new_rf      = delta.get("new_report_findings") or []
    io          = delta.get("io_last_24h") or {}

    scope = DeltaScope()

    if new_vitals:
        scope.types.add("vital")
        scope.vital_params = _vital_params_present(new_vitals)

    if new_labs:
        scope.types.add("lab")
        scope.lab_names = [d.get("name") or "" for d in new_labs]
        scope._lab_attr_keys = [list((d.get("attributes") or {}).keys()) for d in new_labs]

    if new_notes:
        scope.types.add("note")
        scope.is_wildcard = True

    if new_rf:
        scope.types.add("report")
        scope.is_wildcard = True

    if io and (io.get("intake_ml") or io.get("output_ml")):
        scope.types.add("io")

    if scope.is_wildcard:
        scope.categories = {"*"}
    else:
        scope.categories = _compute_categories(scope)

    return scope


def delta_summary_line(delta: dict) -> str:
    """
    One-line human-readable description of what changed this run.

    Prepended to tracker/classifier prompts as an attention header.
    e.g. "DELTA THIS RUN: 1 new lab (Potassium 5.8 mEq/L, Na 138 mEq/L).
    No new vitals or notes since last assessment."
    """
    scope = classify_delta(delta)

    if scope.is_wildcard:
        note_count  = len(delta.get("new_notes") or [])
        rf_count    = len(delta.get("new_report_findings") or [])
        parts = []
        if note_count:
            parts.append(f"{note_count} new note(s)")
        if rf_count:
            parts.append(f"{rf_count} new report finding(s)")
        other = []
        if scope.types & {"lab"}:
            other.append(f"{len(delta.get('new_labs') or [])} lab(s)")
        if scope.types & {"vital"}:
            other.append(f"{len(delta.get('new_vitals') or [])} vital(s)")
        if other:
            parts.append("also: " + ", ".join(other))
        return "DELTA THIS RUN (wildcard — full assessment): " + "; ".join(parts) + "."

    parts = []
    if scope.types & {"lab"}:
        lab_summary = _format_lab_summary(delta.get("new_labs") or [])
        parts.append(f"{len(scope.lab_names)} new lab(s) ({lab_summary})")
    if scope.types & {"vital"}:
        n = len(delta.get("new_vitals") or [])
        parts.append(f"{n} new vital reading(s)")
    if scope.types & {"io"}:
        io = delta.get("io_last_24h") or {}
        parts.append(f"IO updated (balance {io.get('balance_ml', 0):+.0f} mL/24h)")

    absent = []
    if "vital" not in scope.types:
        absent.append("vitals")
    if "lab" not in scope.types:
        absent.append("labs")
    if "note" not in scope.types:
        absent.append("notes")

    header = "DELTA THIS RUN: " + (", ".join(parts) if parts else "no new structured data") + "."
    if absent:
        header += f" No new {', '.join(absent)} since last assessment."
    header += (
        " Focus assessment on whether this delta changes any problem's status; "
        "do not re-litigate problems with no new evidence of their type."
    )
    return header


def relevant_categories(delta: dict) -> set[str]:
    """
    Return the clinical_rule_blocks category names relevant to this delta.

    Returns {"*"} for wildcard deltas (all categories apply).
    Returns a set of category names for scoped deltas.
    """
    return classify_delta(delta).categories


def blast_radius_problems(delta: dict, problems: list[dict]) -> list[dict]:
    """
    Return the subset of problems the delta could plausibly move.

    A problem is "hot" if:
      1. The delta is wildcard (always all problems), OR
      2. Its name/cause keywords match any relevant category, OR
      3. A new lab name directly maps to it via the _LAB_TO_PROBLEM index.

    Cold problems (not in the returned list) are untouched this run.
    Always returns all problems for wildcard deltas.
    """
    scope = classify_delta(delta)

    if scope.is_wildcard or not problems:
        return problems

    from tools.radar_sync.clinical_rule_blocks import _CATEGORIES, _problem_text

    cats = scope.categories  # set of category names

    # Build per-problem hot check
    # A problem is hot if its text intersects any keyword from any matched category
    hot_keywords: set[str] = set()
    for cat_name, (keywords, _) in _CATEGORIES.items():
        if cat_name in cats:
            hot_keywords.update(keywords)

    # Build a lab haystack (names + attribute keys) for direct problem matching.
    # The lab-problem map is keyed by analyte names ("glucose", "potassium", …);
    # we check whether any map key is a substring of the haystack.
    lab_problem_map = _build_lab_problem_map()
    lab_haystack = " ".join(scope.lab_names).lower()
    for attr_keys in scope._lab_attr_keys:
        lab_haystack += " " + " ".join(attr_keys).lower()

    # Which analyte keys are present in this delta's labs?
    present_analytes = {analyte for analyte in lab_problem_map if analyte in lab_haystack}

    hot: list[dict] = []
    for p in problems:
        ptext = _problem_text([p])
        pname_lc = (p.get("name") or "").lower()
        # Category keyword match
        if any(kw in ptext for kw in hot_keywords):
            hot.append(p)
            continue
        # Direct vital → problem name match (e.g. HR abnormal → "Tachycardia")
        for vp in scope.vital_params:
            vital_kws = _VITAL_PROBLEM_KEYWORDS.get(vp) or []
            if any(kw in pname_lc for kw in vital_kws):
                hot.append(p)
                break
        else:
            # Direct analyte → problem match (e.g. "glucose" in labs → "hyperglycemia" problem)
            if present_analytes:
                for analyte in present_analytes:
                    problem_kws = lab_problem_map.get(analyte) or set()
                    if any(kw in pname_lc for kw in problem_kws):
                        hot.append(p)
                        break

    return hot


def get_scoped_problem_names(delta: dict, problems: list[dict]) -> list[str] | None:
    """
    Return problem names to assess for this delta trigger, or None for a full run.

    Returns None when: delta is wildcard (has notes/reports), no problems exist,
    or blast_radius covers the full list (no meaningful pruning).
    """
    scope = classify_delta(delta)
    if scope.is_wildcard or not problems:
        return None
    hot = blast_radius_problems(delta, problems)
    if not hot or len(hot) >= len(problems):
        return None
    return [p["name"] for p in hot]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _vital_params_present(new_vitals: list[dict]) -> set[str]:
    """Return lowercase vital field names that have non-None values in the new readings."""
    _VITAL_FIELDS = ("spo2", "rr", "fio2", "gcs", "hr", "map", "bp", "temp")
    present: set[str] = set()
    for v in new_vitals:
        for f in _VITAL_FIELDS:
            val = v.get(f"days{f.upper()}")
            if val is not None:
                present.add(f)
        # BP and MAP stored under daysBP / daysMAP
        if v.get("daysBP"):
            present.add("bp")
        if v.get("daysMAP"):
            present.add("map")
        if v.get("daysSpO2"):
            present.add("spo2")
        if v.get("daysRR"):
            present.add("rr")
        if v.get("daysHR"):
            present.add("hr")
        if v.get("daysTemp"):
            present.add("temp")
        if v.get("daysGCS"):
            present.add("gcs")
        if v.get("daysFiO2"):
            present.add("fio2")
    return present


def _compute_categories(scope: DeltaScope) -> set[str]:
    """Map DeltaScope lab names + vital params to category names."""
    cats: set[str] = set()

    # Build one large haystack from lab doc names AND their attribute keys so we
    # catch analytes that live inside panels (e.g. "Creatinine" inside "Renal Function Test")
    lab_haystack = " ".join(scope.lab_names).lower()
    for lab_name, attr_keys in zip(scope.lab_names, scope._lab_attr_keys):
        lab_haystack += " " + " ".join(attr_keys).lower()

    for keyword, linked_cats in _LAB_CROSSLINKS.items():
        if keyword in lab_haystack:
            cats.update(linked_cats)

    # Vital-param matching
    for vp in scope.vital_params:
        for keyword, linked_cats in _VITAL_CROSSLINKS.items():
            if keyword in vp.lower():
                cats.update(linked_cats)

    # IO always touches renal
    if "io" in scope.types:
        cats.add("renal")

    return cats


def _format_lab_summary(labs: list[dict]) -> str:
    """Return a short comma-joined summary of lab names + first key value."""
    parts = []
    for lab in labs[:3]:  # cap at 3 for readability
        name = lab.get("name") or "lab"
        attrs = lab.get("attributes") or {}
        # Pick first valued attribute for a representative value
        for k, v in attrs.items():
            val = v.get("value") if isinstance(v, dict) else v
            unit = (v.get("unit") if isinstance(v, dict) else "") or ""
            if val is not None:
                parts.append(f"{name}: {k} {val}{' ' + unit if unit else ''}")
                break
        else:
            parts.append(name)
    if len(labs) > 3:
        parts.append(f"…+{len(labs)-3} more")
    return ", ".join(parts)


_lab_problem_map_cache: dict | None = None


def _build_lab_problem_map() -> dict[str, set[str]]:
    """
    Build an inverted map from lab key → set of problem name substrings.

    Derived from problem_tracker._NEW_PROBLEM_LAB (problem_kw → lab_key).
    Cached after first build.
    """
    global _lab_problem_map_cache
    if _lab_problem_map_cache is not None:
        return _lab_problem_map_cache

    _NEW_PROBLEM_LAB = {
        "hypokalemia":      "potassium",
        "hyperkalemia":     "potassium",
        "hyponatremia":     "sodium",
        "hypernatremia":    "sodium",
        "hypocalcemia":     "calcium",
        "hypercalcemia":    "calcium",
        "hypomagnesemia":   "magnesium",
        "hypophosphatemia": "phosphate",
        "hypoglycemia":     "glucose",
        "hyperglycemia":    "glucose",
        "anemia":           "hb",
        "thrombocytopenia": "platelets",
        "leukocytosis":     "wbc",
        "hypoalbuminemia":  "albumin",
        "aki":              "cr",
        "acute kidney":     "cr",
        "renal failure":    "cr",
        "lactic acidosis":  "lactate",
        "hyperlactatemia":  "lactate",
    }
    result: dict[str, set[str]] = {}
    for problem_kw, lab_key in _NEW_PROBLEM_LAB.items():
        result.setdefault(lab_key, set()).add(problem_kw)
    _lab_problem_map_cache = result
    return result
