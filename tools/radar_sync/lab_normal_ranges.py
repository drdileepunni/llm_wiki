"""
Per-panel normal ranges for the Phase-1 other-lab skip gate.

Rules live at app_settings/lab_normal_ranges.json in GCS and can be edited from the
dashboard without a redeploy. Falls back to DEFAULT_CONFIG if the GCS doc is absent
so the skip gate is never regressed to "always expensive".

Functions:
  load_config(db) -> dict           — GCS doc (or default), sorted and cached
  detect_panel(lab_doc, cfg) -> str | None
  check_lab_normal(lab_doc, cfg, glucose_keywords) -> bool | None

Schema of each panel entry in the config doc:
  {
    "keywords":     ["renal function", ...],   # substrings matched against lab name
    "always_normal": true,                     # qualitative panel, never significant
    "ranges": [
      {"attr": "sodium",    "low": 128, "high": 152},
      {"attr": "potassium", "low": 3.0, "high": 5.8},
      ...
    ]
  }

For ranges, "low"/"high" of null = no bound in that direction.
attr values are matched as substrings against the lowercased attribute key.
Ranges are sorted longest-attr-first at load time so "urea nitrogen" beats "urea".
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Default config (mirrors former hardcoded constants in scheduler.py) ────────

DEFAULT_CONFIG: dict = {
    "_id": "lab_normal_ranges",
    "enabled": True,
    "panels": {
        "blood_type": {
            "keywords": [
                "blood type", "indirect antibody", "antibody screen",
                "type and screen", "type &", "type & screen",
            ],
            "always_normal": True,
            "ranges": [],
        },
        "renal": {
            "keywords": [
                "renal function", "renal 2000", "metabolic panel",
                "basic metabolic", "comprehensive metabolic", "bmp", "cmp",
            ],
            "ranges": [
                # Longer / more-specific attr keys listed first; _prepare_config
                # also sorts by length desc, but explicit ordering adds clarity.
                {"attr": "urea nitrogen/creatinine", "low": 5.0,    "high":  35.0},
                {"attr": "urea nitrogen",            "low": 5.0,    "high":  35.0},
                {"attr": "glomerular filtration",    "low": 25.0,   "high":  None},
                {"attr": "ionized calcium",          "low":  0.9,   "high":   1.5},
                {"attr": "carbon dioxide",           "low": 17.0,   "high":  32.0},
                {"attr": "bicarbonate",              "low": 17.0,   "high":  32.0},
                {"attr": "anion gap",                "low":  4.0,   "high":  18.0},
                {"attr": "phosphorus",               "low":  1.5,   "high":   6.0},
                {"attr": "phosphate",                "low":  1.5,   "high":   6.0},
                {"attr": "magnesium",                "low":  1.2,   "high":   2.9},
                {"attr": "creatinine",               "low":  0.3,   "high":   2.0},
                {"attr": "potassium",                "low":  3.0,   "high":   5.8},
                {"attr": "chloride",                 "low": 93.0,   "high": 113.0},
                {"attr": "calcium",                  "low":  7.5,   "high":  11.5},
                {"attr": "albumin",                  "low":  3.0,   "high":   5.5},
                {"attr": "sodium",                   "low": 128.0,  "high": 152.0},
                {"attr": "uric acid",                "low":  1.5,   "high":  10.0},
                {"attr": "urea",                     "low":  1.5,   "high":  35.0},
                {"attr": "bicarb",                   "low": 17.0,   "high":  32.0},
                {"attr": "gfr",                      "low": 25.0,   "high":  None},
                {"attr": "iron",                     "low": 30.0,   "high": 180.0},
            ],
        },
        "hepatic": {
            "keywords": [
                "hepatic function", "hepatic 1996", "liver function",
                "lft", "liver panel",
            ],
            "ranges": [
                {"attr": "alanine aminotransferase", "low":  5.0, "high":  85.0},
                {"attr": "aspartate aminotransferase","low":  5.0, "high":  75.0},
                {"attr": "alkaline phosphatase",     "low": 30.0, "high": 200.0},
                {"attr": "bilirubin.direct",         "low":  0.0, "high":   0.6},
                {"attr": "bilirubin direct",         "low":  0.0, "high":   0.6},
                {"attr": "bilirubin indirect",       "low":  0.0, "high":   1.0},
                {"attr": "bil(indirect)",             "low":  0.0, "high":   1.0},
                {"attr": "bili(indirect)",            "low":  0.0, "high":   1.0},
                {"attr": "bilirubin.total",          "low":  0.0, "high":   2.0},
                {"attr": "bilirubin total",          "low":  0.0, "high":   2.0},
                {"attr": "total protein",            "low":  4.5, "high":   9.5},
                {"attr": "alt/sgpt",                 "low":  5.0, "high":  85.0},
                {"attr": "ast/sgot",                 "low":  5.0, "high":  75.0},
                {"attr": "globulin",                 "low":  1.5, "high":   5.5},
                {"attr": "albumin",                  "low":  2.5, "high":   5.5},
                {"attr": "sgpt",                     "low":  5.0, "high":  85.0},
                {"attr": "sgot",                     "low":  5.0, "high":  75.0},
                {"attr": "alt",                      "low":  5.0, "high":  85.0},
                {"attr": "ast",                      "low":  5.0, "high":  75.0},
                {"attr": "alp",                      "low": 30.0, "high": 200.0},
                {"attr": "ggt",                      "low":  5.0, "high":  85.0},
            ],
        },
        "cbc": {
            "keywords": [
                "complete blood count", "cbc w", "cbc differential",
                "differential panel", "full blood count", "fbc",
            ],
            "ranges": [
                # Longer attr names first to avoid early-exit on a shorter prefix.
                {"attr": "haemoglobin",  "low":   7.0, "high":  18.5},
                {"attr": "hemoglobin",   "low":   7.0, "high":  18.5},
                {"attr": "haematocrit",  "low":  22.0, "high":  56.0},
                {"attr": "hematocrit",   "low":  22.0, "high":  56.0},
                {"attr": "eosinophil",   "low":   0.0, "high":  10.0},
                {"attr": "basophil",     "low":   0.0, "high":   3.0},
                {"attr": "lymphocyte",   "low":   5.0, "high":  60.0},
                {"attr": "monocyte",     "low":   0.0, "high":  20.0},
                {"attr": "neutrophil",   "low":  15.0, "high":  92.0},
                {"attr": "leukocyte",    "low": 1500.0,"high": 20000.0},
                {"attr": "total count",  "low": 1500.0,"high": 20000.0},
                {"attr": "platelet",     "low": 50000.0,"high": 700000.0},
                {"attr": "atypical",     "low":   0.0, "high":   5.0},
                {"attr": "rdw-cv",       "low":   9.0, "high":  17.0},
                {"attr": "rdw-sd",       "low":  25.0, "high":  60.0},
                {"attr": "mchc",         "low":  28.0, "high":  38.0},
                {"attr": "mch",          "low":  22.0, "high":  36.0},
                {"attr": "mcv",          "low":  65.0, "high": 115.0},
                {"attr": "rdw",          "low":   9.0, "high":  17.0},
                {"attr": "hct",          "low":  22.0, "high":  56.0},
                {"attr": "hgb",          "low":   7.0, "high":  18.5},
                {"attr": "hb",           "low":   7.0, "high":  18.5},
                {"attr": "rbc",          "low":   2.5, "high":   6.5},
                {"attr": "wbc",          "low": 1500.0,"high": 20000.0},
                {"attr": "esr",          "low":   0.0, "high": 120.0},
            ],
        },
        "coag": {
            "keywords": [
                "coagulation", "coag panel", "coag profile",
                "pt/inr", "pt panel", "clotting screen",
            ],
            "ranges": [
                {"attr": "prothrombin time", "low":  8.0, "high": 20.0},
                {"attr": "bleeding time",    "low":  0.5, "high": 18.0},
                {"attr": "clotting time",    "low":  2.0, "high": 35.0},
                {"attr": "fibrinogen",       "low": 150.0,"high": 600.0},
                {"attr": "d-dimer",          "low":  0.0, "high":  2.5},
                {"attr": "aptt",             "low": 16.0, "high": 60.0},
                {"attr": "inr",              "low":  0.7, "high":  2.0},
                {"attr": "ptt",              "low": 16.0, "high": 60.0},
                {"attr": "pt",               "low":  8.0, "high": 20.0},
            ],
        },
    },
}

# ── Module-level cache (shared across all pipeline runs in a process) ──────────

_cache: dict = {}
_cache_ts: float = 0.0
_CACHE_TTL: float = 300.0  # 5 minutes


def load_config(db: Any) -> dict:
    """
    Return the lab normal ranges config, sourced from GCS when available.

    Falls back to DEFAULT_CONFIG if the GCS doc is missing, disabled, or on
    any read error. Cached for _CACHE_TTL seconds to avoid per-chart GCS reads.
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    cfg: dict | None = None
    try:
        doc = db["app_settings"].find_one({"_id": "lab_normal_ranges"})
        if doc and doc.get("enabled", True) and doc.get("panels"):
            cfg = _prepare_config(doc)
            logger.debug("lab_normal_ranges: loaded %d panels from GCS", len(doc["panels"]))
    except Exception:
        logger.exception("lab_normal_ranges: failed to load from GCS — using defaults")

    if cfg is None:
        cfg = _prepare_config(DEFAULT_CONFIG)
        logger.debug("lab_normal_ranges: using default config")

    _cache = cfg
    _cache_ts = now
    return _cache


def _prepare_config(raw: dict) -> dict:
    """
    Deep-copy and sort each panel's ranges by attr length descending.
    Longest attr substrings are matched first so e.g. 'urea nitrogen' beats 'urea'
    and 'mchc' beats 'mch'.
    """
    cfg = copy.deepcopy(raw)
    for panel in cfg.get("panels", {}).values():
        ranges = panel.get("ranges") or []
        panel["_sorted_ranges"] = sorted(
            ranges,
            key=lambda r: len(r.get("attr", "")),
            reverse=True,
        )
    return cfg


# ── Detection & checking ───────────────────────────────────────────────────────

def detect_panel(lab_doc: dict, config: dict) -> str | None:
    """Return the panel key whose keywords match the lab name, or None."""
    name = (lab_doc.get("name") or "").lower()
    for panel_key, panel in config.get("panels", {}).items():
        keywords = panel.get("keywords") or []
        if any(kw in name for kw in keywords):
            return panel_key
    return None


def check_lab_normal(
    lab_doc: dict,
    config: dict,
    glucose_keywords: tuple[str, ...],
) -> bool | None:
    """
    Rule-based normal-range check for a single lab document.

    Returns:
      True  — panel recognised AND all present numeric values within normal range
      False — panel recognised AND at least one value outside normal range
      None  — panel unrecognised (caller treats as significant — conservative)

    Glucose attributes are skipped (handled by the dedicated glucose pathway).
    Non-numeric or missing attribute values are skipped (no opinion).
    Attributes that match no range rule are skipped (no rule → not flagged).
    """
    panel_key = detect_panel(lab_doc, config)
    if panel_key is None:
        return None

    panel = config["panels"][panel_key]
    if panel.get("always_normal"):
        return True

    ranges = panel.get("_sorted_ranges") or panel.get("ranges") or []
    attrs  = lab_doc.get("attributes") or {}

    def _get_val(v: Any) -> float | None:
        if v is None:
            return None
        if isinstance(v, dict):
            v = v.get("value")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for attr_key, attr_val in attrs.items():
        key_lower = (attr_key or "").lower()
        if any(kw in key_lower for kw in glucose_keywords):
            continue  # glucose handled separately
        val = _get_val(attr_val)
        if val is None:
            continue
        for r in ranges:
            if r["attr"] in key_lower:
                lo = r.get("low")
                hi = r.get("high")
                if lo is not None and val < lo:
                    return False
                if hi is not None and val > hi:
                    return False
                break  # first matching rule applied — move to next attribute

    return True
