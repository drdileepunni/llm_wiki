"""
Section-level quality scorer for wiki entity/concept pages.

After each ingest or fill_sections run, score_page() is called on the written
content before it is saved. Scores are injected into the YAML frontmatter under
section_quality:, letting the pipeline prioritise gap fills and the UI show
quality badges.

Score tiers:
  0 = Stub     — no real content (placeholder or < 60 chars)
  1 = Sparse   — content present but very generic; most rubric checks fail
  2 = Partial  — some rubric elements present; key ones still missing
  3 = Adequate — most elements present; minor gaps
  4 = Complete — all required elements + well cross-linked

Required checks gate the score; preferred checks add nuance.
Scoring formula (per section):
  combined = 0.7 * (required_passed / total_required)
           + 0.3 * (preferred_passed / total_preferred)
  combined ≥ 0.85 → 4, ≥ 0.65 → 3, ≥ 0.35 → 2, else → 1
"""

import re
import logging
from datetime import date
from typing import Optional

log = logging.getLogger("wiki.quality_scorer")

# ── Stub detection ────────────────────────────────────────────────────────────

_STUB_PHRASES = ["(stub", "*(stub", "stub —", "stub—", "referenced from other wiki pages"]


def _is_stub(text: str) -> bool:
    t = text.lower().strip()
    if len(t) < 60:
        return True
    return any(p in t for p in _STUB_PHRASES)


# ── Section name normalisation ────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Lowercase, strip wiki links and parentheticals for rubric lookup."""
    s = re.sub(r'\[\[.*?\]\]', '', s)
    s = re.sub(r'\s*\(.*?\)', '', s)
    return s.lower().strip()


# ── Individual check functions ────────────────────────────────────────────────

def _has_specific_doses(text: str) -> bool:
    return bool(re.search(
        r'\d+(?:\.\d+)?\s*(?:mg|mcg|µg|μg|mL|ml|mmol|nmol|units?|U|g|ng|IU)'
        r'(?:/(?:kg|day|dose|hr|hour|min|L|dL|mL|m[²2]))?',
        text, re.IGNORECASE,
    ))


def _has_named_drugs(text: str) -> bool:
    return bool(re.search(
        r'\b[A-Z][a-z]+(?:mab|nib|zumab|olol|pril|sartan|statin|mycin|cillin|'
        r'azole|ine|ide|ate|one)\b'
        r'|\b(?:noradrenaline|norepinephrine|adrenaline|epinephrine|dopamine|'
        r'dobutamine|vasopressin|terlipressin|phenylephrine|esmolol|landiolol|'
        r'metoprolol|meropenem|vancomycin|piperacillin|tazobactam|ceftriaxone|'
        r'cefazolin|furosemide|frusemide|spironolactone|atorvastatin|dexamethasone|'
        r'hydrocortisone|midazolam|fentanyl|propofol|ketamine|morphine|enoxaparin|'
        r'heparin|insulin|dextrose|albumin|metformin|warfarin|aspirin|clopidogrel|'
        r'ticagrelor|chlordiazepoxide|ivabradine|trimetazidine|febuxostat)\b',
        text, re.IGNORECASE,
    ))


def _has_routes(text: str) -> bool:
    return bool(re.search(
        r'\b(?:IV|IM|SC|SQ|PO|oral(?:ly)?|intravenous(?:ly)?|intramuscular(?:ly)?|'
        r'subcutaneous(?:ly)?|enteral(?:ly)?|NGT|nasogastric(?:ally)?|'
        r'infusion|continuous\s+infusion|bolus)\b',
        text, re.IGNORECASE,
    ))


def _has_frequency_or_rate(text: str) -> bool:
    return bool(re.search(
        r'\b(?:q\d+h|q\s*\d+\s*h(?:ours?)?|once\s+daily|twice\s+daily|BD|TDS|QDS|'
        r'every\s+\d+\s*h(?:ours?)?|per\s+hour|per\s+minute|/h(?:our)?|/min|'
        r'continuous(?:ly)?|hourly|\d+[-\s]hourly|daily|'
        r'μg/kg/min|mcg/kg/min)\b',
        text, re.IGNORECASE,
    ))


def _has_monitoring_targets(text: str) -> bool:
    has_target_word = bool(re.search(
        r'\b(?:target|goal|aim|maintain(?:ing)?|keep(?:ing)?|titrat(?:e|ed|ing)|'
        r'achieve|≥|≤|>|<|between\s+\d+\s+and\s+\d+)\b',
        text, re.IGNORECASE,
    ))
    return has_target_word and bool(re.search(r'\d', text))


def _has_wiki_links(text: str) -> bool:
    return bool(re.search(r'\[\[.+?\]\]', text))


def _has_escalation(text: str) -> bool:
    return bool(re.search(
        r'\b(?:if\s+(?:no\s+)?(?:response|improvement|failure)|escalat|step.?up|'
        r'refractory|despite|not\s+respond(?:ing)?|switch\s+to|second.?line|'
        r'progress(?:es)?\s+to|when\s+.{1,40}fail)\b',
        text, re.IGNORECASE,
    ))


def _has_consult_mention(text: str) -> bool:
    return bool(re.search(
        r'\b(?:consult|refer(?:ral)?|specialist|surgical\s+(?:review|opinion)|'
        r'cardiology|pulmonology|nephrology|gastroenterology|haematology|'
        r'endocrinology|neurology|intensivist|critical\s+care)\b',
        text, re.IGNORECASE,
    ))


def _has_specific_parameters(text: str) -> bool:
    return bool(re.search(
        r'\b(?:heart\s+rate|HR|blood\s+pressure|BP|MAP|SpO2|saturation|oxygen|'
        r'lactate|creatinine|sodium|potassium|glucose|temperature|urine\s+output|'
        r'GCS|RASS|troponin|BNP|NT-proBNP|haemoglobin|hemoglobin|WBC|platelets|'
        r'INR|APTT|CVP|cardiac\s+output|SVR|pH|pCO2|pO2|bicarbonate|base\s+excess|'
        r'triglyceride|lipase|amylase|bilirubin|ALT|AST|albumin)\b',
        text, re.IGNORECASE,
    ))


def _has_frequency(text: str) -> bool:
    return bool(re.search(
        r'\b(?:every\s+\d+\s*h(?:ours?)?|q\d+h|hourly|daily|twice|BD|TDS|'
        r'continuously|periodic(?:ally)?|regularly|\d+[-\s]hourly)\b',
        text, re.IGNORECASE,
    ))


def _has_trigger_values(text: str) -> bool:
    return bool(re.search(r'\b(?:if|when)\b.{1,80}\d', text, re.IGNORECASE))


def _has_specific_tests(text: str) -> bool:
    return bool(re.search(
        r'\b(?:CBC|FBC|full\s+blood\s+count|CRP|procalcitonin|lactate|creatinine|'
        r'amylase|lipase|liver\s+function|LFT|troponin|BNP|ECG|EKG|'
        r'CT\b|MRI\b|ultrasound|X-ray|chest\s+X|blood\s+cultur|urine\s+cultur|'
        r'ABG|arterial\s+blood\s+gas|blood\s+gas|INR|APTT|coagulation|'
        r'electrolytes|lipid\s+panel|HbA1c|thyroid|TFT|'
        r'serum\s+(?:calcium|magnesium|phosphate|albumin|sodium|potassium))\b',
        text, re.IGNORECASE,
    ))


def _has_thresholds(text: str) -> bool:
    return bool(re.search(
        r'\d+(?:\.\d+)?\s*'
        r'(?:mmHg|mmol/?[Ll]|mg/?d[Ll]|mg/?[Ll]|°C|°F|%|bpm|IU/?[Ll]|U/?[Ll]|'
        r'nmol/?[Ll]|μmol/?[Ll]|g/?d[Ll]|g/?[Ll])',
        text, re.IGNORECASE,
    ))


def _has_timing(text: str) -> bool:
    return bool(re.search(
        r'\b(?:at\s+admission|on\s+arrival|within\s+\d+|repeat(?:ed)?\s+(?:at|after|every)|'
        r'follow.?up|baseline|serial|prior\s+to|before|after|on\s+day|'
        r'\d+\s*h(?:ours?)?\s+(?:after|later|post))\b',
        text, re.IGNORECASE,
    ))


def _has_sufficient_length(text: str) -> bool:
    return len(text.strip()) > 150


def _has_titration_logic(text: str) -> bool:
    return bool(re.search(
        r'\b(?:titrat|increas(?:e|ed|ing)\s+(?:by|to)|adjust(?:ed|ing)|'
        r'up-?titrat|start(?:ing)?\s+at|maximum\s+(?:dose|of)|maximum\s+\d|'
        r'minimum\s+\d|starting\s+dose)\b',
        text, re.IGNORECASE,
    ))


def _has_loading_maintenance(text: str) -> bool:
    return bool(re.search(
        r'\b(?:loading\s+dose|maintenance\s+dose|initial\s+dose|then|followed\s+by|'
        r'bolus\s+then)\b',
        text, re.IGNORECASE,
    ))


def _has_specific_effects(text: str) -> bool:
    return bool(re.search(
        r'\b(?:hypotension|bradycardia|tachycardia|arrhythmia|nausea|vomiting|'
        r'nephrotox|hepatotox|neurotox|thrombocytopenia|neutropenia|'
        r'hypoglycaemia|hypoglycemia|hyperglycaemia|hyperglycemia|'
        r'rash|allerg|anaphylaxis|QT\s+prolongation|seizure|confusion|'
        r'electrolyte|hypokalaemia|hyponatraemia)\b',
        text, re.IGNORECASE,
    ))


def _has_specific_criteria(text: str) -> bool:
    return bool(re.search(
        r'\b(?:criterion|criteria|defined\s+as|diagnosis\s+(?:requires?|is\s+made)|'
        r'at\s+least\s+\d|two\s+(?:of|out\s+of)|three\s+(?:of|out\s+of)|'
        r'Berlin\s+definition|SOFA|qSOFA|APACHE|SAPS|Ranson|Marshall|AKIN|KDIGO|'
        r'\d+\s*(?:mmHg|mmol|mg))\b',
        text, re.IGNORECASE,
    ))


# ── Check function registry ───────────────────────────────────────────────────

CHECK_FUNCTIONS: dict = {
    "has_specific_doses":       _has_specific_doses,
    "has_named_drugs":          _has_named_drugs,
    "has_routes":               _has_routes,
    "has_frequency_or_rate":    _has_frequency_or_rate,
    "has_monitoring_targets":   _has_monitoring_targets,
    "has_wiki_links":           _has_wiki_links,
    "has_escalation":           _has_escalation,
    "has_consult_mention":      _has_consult_mention,
    "has_specific_parameters":  _has_specific_parameters,
    "has_frequency":            _has_frequency,
    "has_trigger_values":       _has_trigger_values,
    "has_specific_tests":       _has_specific_tests,
    "has_thresholds":           _has_thresholds,
    "has_timing":               _has_timing,
    "has_sufficient_length":    _has_sufficient_length,
    "has_titration_logic":      _has_titration_logic,
    "has_loading_maintenance":  _has_loading_maintenance,
    "has_specific_effects":     _has_specific_effects,
    "has_specific_criteria":    _has_specific_criteria,
}

# Human-readable flag labels (what is MISSING when the check fails)
FLAG_LABELS: dict[str, str] = {
    "has_specific_doses":       "missing_specific_doses",
    "has_named_drugs":          "missing_named_drugs",
    "has_routes":               "missing_administration_route",
    "has_frequency_or_rate":    "missing_frequency_or_rate",
    "has_monitoring_targets":   "missing_monitoring_targets",
    "has_wiki_links":           "no_wiki_links",
    "has_escalation":           "no_escalation_criteria",
    "has_consult_mention":      "no_consult_criteria",
    "has_specific_parameters":  "missing_specific_parameters",
    "has_frequency":            "missing_monitoring_frequency",
    "has_trigger_values":       "no_trigger_values",
    "has_specific_tests":       "missing_specific_tests",
    "has_thresholds":           "missing_reference_values",
    "has_timing":               "missing_investigation_timing",
    "has_sufficient_length":    "insufficient_content",
    "has_titration_logic":      "no_titration_logic",
    "has_loading_maintenance":  "missing_loading_vs_maintenance",
    "has_specific_effects":     "missing_specific_adverse_effects",
    "has_specific_criteria":    "missing_diagnostic_criteria",
}

# ── Quality rubrics ───────────────────────────────────────────────────────────
# Keys are (subtype, normalised_section_name).
# required checks → drive the score and generate flags.
# preferred checks → contribute 30% weight; flags only reported when score ≤ 2.

RUBRICS: dict[tuple, dict] = {
    # ── condition ──────────────────────────────────────────────────────────
    ("condition", "management"): {
        "required":  ["has_specific_doses", "has_named_drugs", "has_routes"],
        "preferred": ["has_monitoring_targets", "has_wiki_links",
                      "has_escalation", "has_consult_mention"],
    },
    ("condition", "monitoring"): {
        "required":  ["has_specific_parameters", "has_frequency"],
        "preferred": ["has_trigger_values", "has_wiki_links"],
    },
    ("condition", "investigations"): {
        "required":  ["has_specific_tests"],
        "preferred": ["has_thresholds", "has_timing", "has_wiki_links"],
    },
    ("condition", "definition and diagnostic criteria"): {
        "required":  ["has_specific_criteria", "has_sufficient_length"],
        "preferred": ["has_thresholds"],
    },
    ("condition", "aetiology"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("condition", "clinical features"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_specific_parameters", "has_thresholds"],
    },
    ("condition", "complications"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("condition", "prognosis"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_thresholds"],
    },
    ("condition", "dosing and administration protocols"): {
        "required":  ["has_specific_doses", "has_routes"],
        "preferred": ["has_frequency_or_rate", "has_titration_logic"],
    },

    # ── medication ─────────────────────────────────────────────────────────
    ("medication", "dosing"): {
        "required":  ["has_specific_doses", "has_routes", "has_frequency_or_rate"],
        "preferred": ["has_titration_logic", "has_loading_maintenance"],
    },
    ("medication", "monitoring parameters"): {
        "required":  ["has_specific_parameters", "has_frequency"],
        "preferred": ["has_trigger_values"],
    },
    ("medication", "adverse effects"): {
        "required":  ["has_specific_effects"],
        "preferred": ["has_thresholds"],
    },
    ("medication", "mechanism of action"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },
    ("medication", "indications"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("medication", "contraindications"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_thresholds"],
    },
    ("medication", "renal / hepatic dose adjustment"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_specific_doses", "has_thresholds"],
    },
    ("medication", "drug interactions"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_named_drugs"],
    },

    # ── investigation ──────────────────────────────────────────────────────
    ("investigation", "reference range"): {
        "required":  ["has_thresholds"],
        "preferred": ["has_specific_parameters"],
    },
    ("investigation", "clinical significance"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("investigation", "interpretation in icu"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_thresholds", "has_specific_parameters"],
    },
    ("investigation", "common causes of abnormal values"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("investigation", "limitations"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },

    # ── procedure ──────────────────────────────────────────────────────────
    ("procedure", "technique"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },
    ("procedure", "indications"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("procedure", "contraindications"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },
    ("procedure", "complications"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },
    ("procedure", "post-procedure monitoring"): {
        "required":  ["has_specific_parameters", "has_frequency"],
        "preferred": ["has_trigger_values"],
    },

    # ── default ────────────────────────────────────────────────────────────
    ("default", "definition"): {
        "required":  ["has_sufficient_length"],
        "preferred": [],
    },
    ("default", "clinical significance"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links"],
    },
    ("default", "management"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_wiki_links", "has_monitoring_targets"],
    },
    ("default", "monitoring"): {
        "required":  ["has_sufficient_length"],
        "preferred": ["has_specific_parameters"],
    },
}

# ── Section-level scorer ──────────────────────────────────────────────────────

def score_section(section_name: str, body: str, subtype: str) -> Optional[dict]:
    """
    Score one section against its rubric.
    Returns None if no rubric applies (section is skipped from quality map).
    Returns {"score": 0-4, "flags": [...missing items...]} otherwise.
    """
    if _is_stub(body):
        return {"score": 0, "flags": ["stub_or_empty"]}

    norm = _norm(section_name)
    rubric = RUBRICS.get((subtype, norm)) or RUBRICS.get(("default", norm))

    if rubric is None:
        # No rubric for this section — basic length-based fallback
        length = len(body.strip())
        if length > 150:
            return {"score": 3, "flags": []}
        elif length > 50:
            return {"score": 2, "flags": ["insufficient_content"]}
        else:
            return {"score": 1, "flags": ["insufficient_content"]}

    required  = rubric.get("required", [])
    preferred = rubric.get("preferred", [])

    failed_req  = [c for c in required  if not CHECK_FUNCTIONS[c](body)]
    failed_pref = [c for c in preferred if not CHECK_FUNCTIONS[c](body)]

    total_req  = len(required)
    total_pref = len(preferred)
    req_ratio  = (total_req  - len(failed_req))  / total_req  if total_req  else 1.0
    pref_ratio = (total_pref - len(failed_pref)) / total_pref if total_pref else 1.0

    combined = 0.7 * req_ratio + 0.3 * pref_ratio

    if combined >= 0.85:
        score = 4
    elif combined >= 0.65:
        score = 3
    elif combined >= 0.35:
        score = 2
    else:
        score = 1

    # Always report failed required checks as flags.
    # Report failed preferred checks only when score ≤ 2 (they pulled it down).
    flags = [FLAG_LABELS.get(c, c) for c in failed_req]
    if score <= 2:
        flags += [FLAG_LABELS.get(c, c) for c in failed_pref]

    return {"score": score, "flags": flags}


# ── Page-level scorer ─────────────────────────────────────────────────────────

def _parse_subtype(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("subtype:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return "default"


def _parse_sections(content: str) -> dict:
    """Return {section_heading: body_text} for every ## section."""
    lines = content.splitlines()
    fm_end = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i + 1
                break

    sections: dict = {}
    current: Optional[str] = None
    body: list = []

    for line in lines[fm_end:]:
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(body).strip()
            current = line[3:].strip()
            body = []
        elif current is not None:
            body.append(line)

    if current is not None:
        sections[current] = "\n".join(body).strip()

    return sections


# Sections that carry no clinical knowledge and should not be scored
_SKIP_SECTIONS = {
    "citation", "abstract", "key findings", "entities mentioned",
    "concepts mentioned", "open questions", "suggested sources",
    "missing sections", "resolution question", "contradictions",
}


def score_page(content: str) -> dict:
    """
    Score all scorable sections of a wiki page.
    Returns {section_name: {"score": int, "flags": list[str]}}.
    Skips source/gap/query pages (only entity/concept pages are scored).
    """
    # Skip non-entity/concept page types
    for line in content.splitlines():
        if line.startswith("type:"):
            ptype = line.split(":", 1)[1].strip().strip('"')
            if ptype not in ("entity", "concept"):
                return {}
            break

    subtype  = _parse_subtype(content)
    sections = _parse_sections(content)
    scores: dict = {}

    for name, body in sections.items():
        if _norm(name) in _SKIP_SECTIONS:
            continue
        result = score_section(name, body, subtype)
        if result is not None:
            scores[name] = result

    log.debug("score_page subtype=%s scores=%s", subtype,
              {k: v["score"] for k, v in scores.items()})
    return scores


# ── Frontmatter helpers ───────────────────────────────────────────────────────

def update_quality_frontmatter(content: str, scores: dict) -> str:
    """
    Inject or replace section_quality + quality_assessed in YAML frontmatter.
    Leaves the body (everything after the closing ---) untouched.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content

    fm_end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end is None:
        return content

    # Strip existing section_quality / quality_assessed blocks
    cleaned_fm: list[str] = []
    skip = False
    for line in lines[1:fm_end]:
        if line.startswith("section_quality:") or line.startswith("quality_assessed:"):
            skip = True
            continue
        if skip and (line.startswith("  ") or line.startswith("\t")):
            continue
        skip = False
        cleaned_fm.append(line)

    # Build new section_quality block
    sq: list[str] = ["section_quality:"]
    for section in sorted(scores):
        data   = scores[section]
        score  = data.get("score", 0)
        flags  = data.get("flags", [])
        sq.append(f'  "{section}":')
        sq.append(f"    score: {score}")
        sq.append(f"    flags: [{', '.join(flags)}]")
    sq.append(f"quality_assessed: {date.today().isoformat()}")

    result = (
        ["---"]
        + cleaned_fm
        + sq
        + ["---"]
        + lines[fm_end + 1:]
    )
    return "\n".join(result)


def parse_section_quality(content: str) -> dict:
    """
    Parse section_quality from YAML frontmatter into a plain dict.
    Returns {} if the block is absent.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fm_end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_end = i
            break
    if fm_end is None:
        return {}

    result: dict = {}
    in_sq           = False
    current_section: Optional[str] = None

    for line in lines[1:fm_end]:
        if line.startswith("section_quality:"):
            in_sq = True
            continue
        if in_sq:
            if not (line.startswith("  ") or line.startswith("\t")):
                in_sq = False
                continue
            # Section name line: '  "SectionName":'
            m = re.match(r'^\s{2}"?([^":\n]+)"?:\s*$', line)
            if m:
                current_section = m.group(1).strip()
                result[current_section] = {"score": 0, "flags": []}
                continue
            if current_section:
                sm = re.match(r'^\s{4}score:\s*(\d+)', line)
                if sm:
                    result[current_section]["score"] = int(sm.group(1))
                    continue
                fm_m = re.match(r'^\s{4}flags:\s*\[([^\]]*)\]', line)
                if fm_m:
                    fs = fm_m.group(1).strip()
                    result[current_section]["flags"] = (
                        [f.strip() for f in fs.split(",") if f.strip()]
                        if fs else []
                    )
    return result


# ── Scope templates & helpers ─────────────────────────────────────────────────

SCOPE_TEMPLATES: dict = {
    "condition": (
        "{title} — general disease: etiology-agnostic pathophysiology, diagnosis, and "
        "management. Etiology-specific variants (e.g. drug-induced, disease-associated) "
        "and population-specific adaptations belong on their own dedicated pages."
    ),
    "medication": (
        "{title} — pharmacology, mechanism of action, general dosing, adverse effects, "
        "and contraindications. Disease-specific dosing protocols and combination regimens "
        "belong on condition pages."
    ),
    "investigation": (
        "{title} — what it measures, how to perform, and how to interpret results. "
        "Disease-specific reference ranges and clinical decision thresholds belong on "
        "condition pages."
    ),
    "procedure": (
        "{title} — technique, indications, contraindications, and general complications. "
        "Condition-specific procedural modifications belong on condition pages."
    ),
    "default": (
        "{title} — general information about this topic. Closely related but distinct "
        "subtopics belong on their own dedicated pages."
    ),
}


def _default_scope(title: str, subtype: str) -> str:
    """Generate a default scope declaration from title + subtype template."""
    template = SCOPE_TEMPLATES.get(subtype, SCOPE_TEMPLATES["default"])
    return template.format(title=title)


# ── Scope check tool ───────────────────────────────────────────────────────────

_SCOPE_CHECK_TOOL = {
    "name": "report_scope_check",
    "description": "Report scope contamination findings for this wiki page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clean": {
                "type": "boolean",
                "description": "True if ALL content is within the page's declared scope."
            },
            "violations": {
                "type": "array",
                "description": "Out-of-scope content blocks found. Empty list if clean.",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": "The ## section heading containing the violation."
                        },
                        "content": {
                            "type": "string",
                            "description": "The exact sentence(s) or bullet(s) that are out of scope."
                        },
                        "belongs_on": {
                            "type": "string",
                            "description": "Page title where this content belongs."
                        },
                        "is_new_page": {
                            "type": "boolean",
                            "description": "True if that target page does not exist yet in the wiki."
                        }
                    },
                    "required": ["section", "content", "belongs_on", "is_new_page"]
                }
            }
        },
        "required": ["clean", "violations"]
    }
}

_SCOPE_CHECK_SYSTEM = """You are a wiki curator checking for scope contamination.

## What is scope contamination?

Scope contamination ONLY applies to CONDITION / DISEASE pages.
It means: a general condition page contains a detailed management PROTOCOL that is specific
to a subtype, variant, or named cause — rather than the general condition itself.

Canonical contamination example:
- "Acute Pancreatitis" page has a full insulin-infusion + plasmapheresis protocol
  that specifically targets hypertriglyceridemia-induced pancreatitis (a subtype).
  That subtype protocol should live on "Hypertriglyceridemia Induced Pancreatitis", not here.

## Page-type rules — READ THESE CAREFULLY

### MEDICATION / DRUG pages
Drug pages (scope mentions "drug", "dosing", "pharmacology", "antibiotic", "agent"):
- Condition-specific dosing IS the core purpose of a drug page. NEVER flag it.
- "Ticagrelor: Dosing → Myocardial Infarction" — correct, NOT contamination
- "Furosemide: Dosing → Chronic Heart Failure" — correct, NOT contamination
- "Noradrenaline: titration steps in Septic Shock" — correct, NOT contamination
- Only flag a drug page if it contains a full DISEASE MANAGEMENT PROTOCOL
  (history, diagnosis, workup, differentials) completely unrelated to the drug's use.

### PROCEDURE / DEVICE / TECHNIQUE pages
Procedure pages (scope mentions "procedure", "technique", "device", "ventilation", "catheter"):
- Condition-specific INDICATIONS and CONTRAINDICATIONS are the core purpose. NEVER flag them.
- "Non-Invasive Ventilation: Indications → COPD" — correct, NOT contamination
- "Surgical Consultation: Indications → Acute Pancreatitis" — correct, NOT contamination

### CLINICAL CONCEPT / GENERAL TREATMENT pages
Pages about monitoring targets, supportive therapies, scoring tools:
- Condition-specific parameters that illustrate the concept are appropriate. Do not flag.
- "Blood Pressure Targets: MAP target in cardiogenic shock" — correct, NOT contamination

### GENERAL CONDITION / DISEASE pages (the only type where contamination applies)
Flag ONLY if: a clinical section (Management, Dosing, Treatment, Monitoring) contains a
FULL STANDALONE PROTOCOL for a named subtype/variant/etiology, not the general disease.

## Universal exclusions — NEVER flag regardless of page type

- Any section named "Etiology-Specific Variants", "See Also", "Subtypes", "Related Conditions"
- Cross-reference lines: "See [[X]] for details", "[[X]] — see this page for Y protocols"
- Single bullets mentioning a condition with a wiki link
- Aetiology or Definition sections mentioning subtypes

## Self-referential rule

NEVER set belongs_on to the same page being checked. If content belongs where it already is,
the page is clean. Do NOT flag content as "belongs on [this very page]".

## Verdict threshold

To flag a violation: the content must be a standalone protocol block (≥2 consecutive bullets
or a full paragraph) providing actual management/dosing/monitoring STEPS for a subtype,
with no routing link to a separate page for those details.

When in doubt: DO NOT FLAG.
"""


_MEDICATION_KEYWORDS = {
    "drug", "medication", "dose", "dosing", "pharmacology", "pharmacokinetic",
    "antibiotic", "antifungal", "antiviral", "agent", "infusion", "tablet",
    "capsule", "injection", "mg", "mcg", "iv", "oral", "subcutaneous",
    "half-life", "clearance", "bioavailability", "adverse effect", "side effect",
}
_PROCEDURE_KEYWORDS = {
    "procedure", "technique", "device", "ventilation", "catheter", "intubation",
    "extubation", "cannulation", "line insertion", "bronchoscopy", "dialysis",
    "plasmapheresis", "ecmo", "surgery", "operation", "biopsy", "drain",
    "defibrillation", "cardioversion", "resuscitation",
}


def _page_type_hint(content: str, scope: str = "") -> str:
    """Extract a one-line page-type hint from frontmatter + scope to guide the scope check."""
    import re
    fm_type = re.search(r'^type:\s*(\S+)', content, re.MULTILINE)
    fm_subtype = re.search(r'^subtype:\s*(\S+)', content, re.MULTILINE)
    t = (fm_type.group(1) if fm_type else "").lower()
    s = (fm_subtype.group(1) if fm_subtype else "").lower()

    # Explicit subtype/type match
    if s == "medication" or "medication" in t:
        return "PAGE TYPE: medication/drug — condition-specific dosing is core content, never contamination."
    if s in ("procedure", "device", "technique"):
        return "PAGE TYPE: procedure/device — condition-specific indications and contraindications are core content, never contamination."

    # Infer from scope string keywords
    scope_lower = scope.lower()
    content_lower = content[:500].lower()  # frontmatter + first section only
    combined = scope_lower + " " + content_lower

    med_hits = sum(1 for kw in _MEDICATION_KEYWORDS if kw in combined)
    proc_hits = sum(1 for kw in _PROCEDURE_KEYWORDS if kw in combined)

    if med_hits >= 3:
        return "PAGE TYPE: medication/drug — condition-specific dosing is core content, never contamination."
    if proc_hits >= 2:
        return "PAGE TYPE: procedure/device — condition-specific indications and contraindications are core content, never contamination."
    if s == "condition" or t == "concept":
        return "PAGE TYPE: condition/concept — check for subtype-specific management protocols."
    return ""


def check_scope(
    page_title: str,
    content: str,
    scope: str,
    existing_page_titles: Optional[list] = None,
) -> dict:
    """
    LLM-based scope contamination check for a wiki page.

    Returns:
        {
            "clean": bool,
            "violations": [
                {"section": str, "content": str, "belongs_on": str, "is_new_page": bool}
            ]
        }
    Errors are non-fatal — returns {"clean": True, "violations": []} on failure.
    """
    existing_titles = existing_page_titles or []
    try:
        from .llm_client import get_llm_client
        llm = get_llm_client()

        # Strip frontmatter — only check the body
        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                body = content[end + 3:].strip()

        existing_block = ""
        if existing_titles:
            existing_block = (
                "\nExisting wiki pages (use these titles for belongs_on):\n"
                + ", ".join(existing_titles[:120])
                + "\n"
            )

        type_hint = _page_type_hint(content, scope)

        prompt = (
            f"Wiki page title: {page_title}\n"
            f"Declared scope: {scope}\n"
            + (f"{type_hint}\n" if type_hint else "")
            + f"{existing_block}\n"
            f"--- PAGE CONTENT ---\n{body[:8000]}\n--- END ---\n\n"
            f"Check this page for scope contamination. "
            f"Remember: belongs_on must NEVER equal '{page_title}' (the page being checked). "
            f"Call report_scope_check with your findings."
        )

        resp = llm.create_message(
            messages=[{"role": "user", "content": prompt}],
            tools=[_SCOPE_CHECK_TOOL],
            system=_SCOPE_CHECK_SYSTEM,
            max_tokens=1500,
        )

        block = next((b for b in resp.content if b.type == "tool_use"), None)
        if not block:
            log.warning("check_scope: no tool_use block for '%s'", page_title)
            return {"clean": True, "violations": []}

        raw_violations = block.input.get("violations", [])

        # Post-processing: filter self-referential and whitelisted violations
        def _normalise(s: str) -> str:
            return s.lower().strip().replace("-", " ").replace("_", " ")

        title_norm = _normalise(page_title)
        ignored = {
            (_normalise(e.get("section", "")), _normalise(e.get("belongs_on", "")))
            for e in parse_scope_ignore(content)
        }

        violations = [
            v for v in raw_violations
            if _normalise(v.get("belongs_on", "")) != title_norm
            and (_normalise(v.get("section", "")), _normalise(v.get("belongs_on", ""))) not in ignored
        ]

        if len(violations) < len(raw_violations):
            log.info(
                "check_scope: dropped %d self-referential/ignored violation(s) on '%s'",
                len(raw_violations) - len(violations), page_title,
            )

        result = {
            "clean": len(violations) == 0,
            "violations": violations,
        }
        if result["violations"]:
            log.info(
                "check_scope: %d violation(s) on '%s' → %s",
                len(result["violations"]), page_title,
                [v.get("belongs_on") for v in result["violations"]],
            )
        return result

    except Exception as exc:
        log.warning("check_scope failed (non-fatal) for '%s': %s", page_title, exc)
        return {"clean": True, "violations": []}


# ── Scope frontmatter helpers ──────────────────────────────────────────────────

def _get_fm_bounds(content: str):
    """Return (fm_start_lines, fm_end_idx, body_lines) or None if no frontmatter."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines, i
    return None


def update_scope_frontmatter(content: str, scope: str) -> str:
    """Inject or replace the scope: field in YAML frontmatter."""
    result = _get_fm_bounds(content)
    if result is None:
        return content
    lines, fm_end = result
    cleaned = [l for l in lines[1:fm_end] if not l.startswith("scope:")]
    return "\n".join(
        ["---"] + cleaned + [f'scope: "{scope}"'] + ["---"] + lines[fm_end + 1:]
    )


def update_contamination_frontmatter(content: str, violations: list) -> str:
    """Inject or replace scope_contamination: block in YAML frontmatter."""
    result = _get_fm_bounds(content)
    if result is None:
        return content
    lines, fm_end = result

    # Strip existing block
    cleaned: list = []
    skip = False
    for line in lines[1:fm_end]:
        if line.startswith("scope_contamination:"):
            skip = True
            continue
        if skip and (line.startswith("  ") or line.startswith("\t")):
            continue
        skip = False
        cleaned.append(line)

    if not violations:
        return "\n".join(["---"] + cleaned + ["---"] + lines[fm_end + 1:])

    block = ["scope_contamination:"]
    for v in violations:
        block.append(f'  - section: "{v.get("section", "")}"')
        block.append(f'    belongs_on: "{v.get("belongs_on", "")}"')
        block.append(f'    is_new_page: {str(v.get("is_new_page", False)).lower()}')
        excerpt = v.get("content", "")[:200].replace('"', "'").replace("\n", " ")
        block.append(f'    excerpt: "{excerpt}"')

    return "\n".join(["---"] + cleaned + block + ["---"] + lines[fm_end + 1:])


def clear_contamination_frontmatter(content: str) -> str:
    """Remove scope_contamination block from frontmatter (called after successful defrag)."""
    return update_contamination_frontmatter(content, [])


def parse_scope(content: str) -> str:
    """Extract scope: field value from frontmatter. Returns '' if absent."""
    for line in content.splitlines():
        if line.startswith("scope:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
        if line.strip() == "---" and line != content.splitlines()[0]:
            break
    return ""


def parse_scope_contamination(content: str) -> list:
    """Parse scope_contamination block from frontmatter into a list of violation dicts."""
    result = _get_fm_bounds(content)
    if result is None:
        return []
    lines, fm_end = result

    violations: list = []
    in_sc = False
    current: dict = {}

    for line in lines[1:fm_end]:
        if line.startswith("scope_contamination:"):
            in_sc = True
            continue
        if in_sc:
            if not (line.startswith("  ") or line.startswith("\t")):
                in_sc = False
                if current:
                    violations.append(current)
                    current = {}
                continue
            if re.match(r'^\s{2}-\s', line):
                if current:
                    violations.append(current)
                current = {}
                m = re.match(r'^\s{2}-\s+section:\s+"?([^"]+)"?', line)
                if m:
                    current["section"] = m.group(1)
            elif current:
                for pat, key in [
                    (r'^\s{4}belongs_on:\s+"?([^"]+)"?', "belongs_on"),
                    (r'^\s{4}excerpt:\s+"?([^"]*)"?',    "excerpt"),
                ]:
                    m = re.match(pat, line)
                    if m:
                        current[key] = m.group(1)
                m_new = re.match(r'^\s{4}is_new_page:\s+(true|false)', line)
                if m_new:
                    current["is_new_page"] = m_new.group(1) == "true"

    if current:
        violations.append(current)
    return violations


# ── Scope ignore (false-positive whitelist) ───────────────────────────────────

def parse_scope_ignore(content: str) -> list:
    """Parse scope_ignore block from frontmatter → list of {section, belongs_on} dicts."""
    result = _get_fm_bounds(content)
    if result is None:
        return []
    lines, fm_end = result

    entries: list = []
    in_si = False
    current: dict = {}

    for line in lines[1:fm_end]:
        if line.startswith("scope_ignore:"):
            in_si = True
            continue
        if in_si:
            if not (line.startswith("  ") or line.startswith("\t")):
                in_si = False
                if current:
                    entries.append(current)
                    current = {}
                continue
            if re.match(r'^\s{2}-\s', line):
                if current:
                    entries.append(current)
                current = {}
                m = re.match(r'^\s{2}-\s+section:\s+"?([^"]+)"?', line)
                if m:
                    current["section"] = m.group(1)
            elif current:
                m = re.match(r'^\s{4}belongs_on:\s+"?([^"]+)"?', line)
                if m:
                    current["belongs_on"] = m.group(1)

    if current:
        entries.append(current)
    return entries


def add_scope_ignore(content: str, section: str, belongs_on: str) -> str:
    """Add a (section, belongs_on) entry to scope_ignore frontmatter and clear that violation."""
    result = _get_fm_bounds(content)
    if result is None:
        return content
    lines, fm_end = result

    # Build updated ignore list (deduplicated)
    existing = parse_scope_ignore(content)
    def _norm(s): return s.lower().strip()
    entry_key = (_norm(section), _norm(belongs_on))
    if not any((_norm(e.get("section", "")), _norm(e.get("belongs_on", ""))) == entry_key
               for e in existing):
        existing.append({"section": section, "belongs_on": belongs_on})

    # Also remove matching entry from scope_contamination
    contam = parse_scope_contamination(content)
    contam_filtered = [
        v for v in contam
        if not (
            _norm(v.get("section", "")) == _norm(section)
            and _norm(v.get("belongs_on", "")) == _norm(belongs_on)
        )
    ]

    # Strip both blocks from frontmatter, then re-inject
    cleaned: list = []
    skip = False
    for line in lines[1:fm_end]:
        if line.startswith("scope_ignore:") or line.startswith("scope_contamination:"):
            skip = True
            continue
        if skip and (line.startswith("  ") or line.startswith("\t")):
            continue
        skip = False
        cleaned.append(line)

    # Re-build scope_ignore block
    ignore_block: list = []
    if existing:
        ignore_block = ["scope_ignore:"]
        for e in existing:
            ignore_block.append(f'  - section: "{e["section"]}"')
            ignore_block.append(f'    belongs_on: "{e["belongs_on"]}"')

    # Re-build scope_contamination block (remaining violations)
    contam_block: list = []
    if contam_filtered:
        contam_block = ["scope_contamination:"]
        for v in contam_filtered:
            contam_block.append(f'  - section: "{v.get("section", "")}"')
            contam_block.append(f'    belongs_on: "{v.get("belongs_on", "")}"')
            contam_block.append(f'    is_new_page: {str(v.get("is_new_page", False)).lower()}')
            excerpt = v.get("excerpt", "")[:200].replace('"', "'").replace("\n", " ")
            contam_block.append(f'    excerpt: "{excerpt}"')

    return "\n".join(
        ["---"] + cleaned + ignore_block + contam_block + ["---"] + lines[fm_end + 1:]
    )


# ── LLM enhancement (opt-in, for borderline sections) ────────────────────────

def llm_enhance_scores(scores: dict, sections: dict[str, str], subtype: str) -> dict:
    """
    For sections scoring 1 or 2, ask the LLM to check clinical completeness
    and add flags that regex cannot catch.

    This is intentionally opt-in — call it from the lint endpoint only, not from
    the automatic ingest/fill path. Errors are non-fatal: original scores returned.
    """
    import json as _json
    borderline = {s: d for s, d in scores.items() if d["score"] in (1, 2)}
    if not borderline:
        return scores

    try:
        from .llm_client import get_llm_client
        llm = get_llm_client()

        section_blocks = "\n\n".join(
            f"### {name}\n{sections.get(name, '')[:1200]}"
            for name in borderline
        )

        resp = llm.create_message(
            messages=[{"role": "user", "content": (
                f"Review these wiki sections for clinical completeness. "
                f"Page type: {subtype}.\n\n"
                f"For each section return:\n"
                f'  "score": 1-4 (1=sparse, 4=complete)\n'
                f'  "flags": list of short strings naming what is clinically missing\n\n'
                f"{section_blocks}\n\n"
                f'Return ONLY valid JSON: {{"SectionName": {{"score": N, "flags": [...]}}, ...}}'
            )}],
            max_tokens=700,
        )

        text = next(
            (getattr(b, "text", "") for b in resp.content if hasattr(b, "text")),
            "",
        )
        m = re.search(r'\{[\s\S]+\}', text)
        if not m:
            return scores

        data = _json.loads(m.group())
        updated = dict(scores)

        for sec, llm_data in data.items():
            if sec not in updated:
                continue
            llm_score = int(llm_data.get("score", updated[sec]["score"]))
            llm_flags = [str(f) for f in llm_data.get("flags", [])]
            merged_flags = list(dict.fromkeys(updated[sec]["flags"] + llm_flags))
            final_score  = min(updated[sec]["score"], llm_score)
            updated[sec] = {"score": final_score, "flags": merged_flags}

        log.info("llm_enhance_scores: refined %d borderline section(s)", len(borderline))
        return updated

    except Exception as exc:
        log.warning("llm_enhance_scores failed (returning rule-based scores): %s", exc)
        return scores
