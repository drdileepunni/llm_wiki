"""
Scenario catalog for automated viva batch runs.

Defines 14 diagnosis groups and 15 complications derived from real ICU admission
data. Provides cross-product enumeration and weighted sampling so batch runs can
mirror the clinical frequency distribution of the patient population.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

# ── Diagnosis groups ──────────────────────────────────────────────────────────
# Each group merges overlapping diagnoses from the raw admission data.
# weight = sum of raw admission counts across merged diagnoses.

DIAGNOSIS_GROUPS: list[dict] = [
    {"id": "pneumonia",          "label": "Pneumonia / Respiratory Infection",    "weight": 2123},
    {"id": "ami",                "label": "Acute MI / ACS",                       "weight": 1233},
    {"id": "malignancy",         "label": "Malignancy",                           "weight":  886},
    {"id": "gi_abdominal",       "label": "GI / Abdominal Issues",                "weight":  868},
    {"id": "trauma",             "label": "Trauma / TBI",                         "weight":  690},
    {"id": "seizures_hypogly",   "label": "Seizures / Hypoglycemia",              "weight":  621},
    {"id": "ich",                "label": "Intracranial Hemorrhage",              "weight":  600},
    {"id": "sepsis",             "label": "Sepsis / Septic Shock",                "weight":  574},
    {"id": "postop_oncologic",   "label": "Post-op Oncologic Surgery",            "weight":  520},
    {"id": "stroke",             "label": "Stroke / CNS Ischemia",               "weight":  382},
    {"id": "aki_ckd",            "label": "AKI / CKD / Fluid Overload",          "weight":  337},
    {"id": "organophosphate",    "label": "Organophosphate Poisoning",            "weight":  212},
    {"id": "fever_unknown",      "label": "Fever / Undifferentiated",             "weight":  117},
    {"id": "chest_pain_dyspnea", "label": "Chest Pain / Dyspnea / AMS",          "weight":   41},
]

# ── Complications ─────────────────────────────────────────────────────────────

COMPLICATIONS: list[dict] = [
    {"id":  0, "label": "Electrolyte Imbalance"},
    {"id":  1, "label": "Hospital Acquired Infections"},
    {"id":  2, "label": "Pressure Ulcers"},
    {"id":  3, "label": "Bleeding / Arrhythmia"},
    {"id":  4, "label": "Pulmonary / GI Issues"},
    {"id":  5, "label": "ICU Complications"},
    {"id":  6, "label": "Respiratory Failure"},
    {"id":  7, "label": "Oliguric AKI"},
    {"id":  8, "label": "Encephalopathy / Seizures"},
    {"id":  9, "label": "Cardiac Arrhythmias"},
    {"id": 10, "label": "Septic Shock"},
    {"id": 11, "label": "Hematologic Disorders"},
    {"id": 12, "label": "Cerebral Infarct"},
    {"id": 13, "label": "Pulmonary Consolidation"},
    {"id": 14, "label": "Acute Kidney Injury"},
]

# ── Topic templates ───────────────────────────────────────────────────────────
# Templates are filled with diagnosis label + complication label.
# The viva teacher LLM then enriches the topic into a full trajectory.

_TOPIC_TEMPLATES = [
    "{diagnosis} patient in ICU developing {complication} — management and escalation decisions",
    "Elderly {diagnosis} patient with new-onset {complication} — diagnostic workup and treatment priorities",
    "{diagnosis} patient complicated by {complication} — identification, risk stratification, and immediate management",
    "ICU admission for {diagnosis} with worsening {complication} — evaluation and intervention planning",
]


# ── ScenarioEntry ─────────────────────────────────────────────────────────────

@dataclass
class ScenarioEntry:
    diagnosis_id:    str
    diagnosis_label: str
    complication_id: int
    complication_label: str
    topic_string:    str
    weight:          float   # normalised 0–1


# ── Catalog helpers ───────────────────────────────────────────────────────────

def make_topic_string(diagnosis_label: str, complication_label: str, seed: int | None = None) -> str:
    """
    Build a topic string from a diagnosis + complication pair using a template.
    Deterministic when seed is provided.
    """
    rng = random.Random(seed)
    template = rng.choice(_TOPIC_TEMPLATES)
    return template.format(diagnosis=diagnosis_label, complication=complication_label)


def build_catalog(seed: int | None = None) -> list[ScenarioEntry]:
    """
    Full cross-product of 14 diagnosis groups × 15 complications = 210 entries.
    Weights are normalised so they sum to 1.0.
    """
    total_weight = sum(d["weight"] for d in DIAGNOSIS_GROUPS) * len(COMPLICATIONS)
    entries: list[ScenarioEntry] = []
    for d in DIAGNOSIS_GROUPS:
        for c in COMPLICATIONS:
            # Derive a deterministic seed per (diagnosis, complication) pair so
            # the same combination always produces the same topic string, while
            # different combinations get different templates.
            pair_seed = None if seed is None else (seed ^ hash(d["id"]) ^ c["id"]) & 0xFFFFFFFF
            entries.append(ScenarioEntry(
                diagnosis_id=d["id"],
                diagnosis_label=d["label"],
                complication_id=c["id"],
                complication_label=c["label"],
                topic_string=make_topic_string(d["label"], c["label"], seed=pair_seed),
                weight=d["weight"] / total_weight,
            ))
    return entries


def sample_scenarios(
    n: int,
    mode: Literal["weighted", "random", "full"] = "weighted",
    seed: int | None = None,
    diagnosis_filter: str | None = None,
) -> list[ScenarioEntry]:
    """
    Return up to n ScenarioEntry items from the catalog.

    Modes:
      weighted — sample with replacement, probability proportional to diagnosis
                 admission count (mimics real ICU mix).
      random   — sample without replacement, uniform probability.
      full     — return all 210 entries (n is ignored).

    If diagnosis_filter is set, only entries with that diagnosis_id are sampled.
    """
    catalog = build_catalog(seed=seed)

    if diagnosis_filter:
        catalog = [e for e in catalog if e.diagnosis_id == diagnosis_filter]

    if mode == "full":
        return catalog

    rng = random.Random(seed)

    if mode == "weighted":
        weights = [e.weight for e in catalog]
        return rng.choices(catalog, weights=weights, k=n)

    # random (without replacement, uniform)
    return rng.sample(catalog, min(n, len(catalog)))


def get_catalog_stats() -> dict:
    """Return summary stats about the catalog for the API."""
    return {
        "diagnosis_groups": len(DIAGNOSIS_GROUPS),
        "complications": len(COMPLICATIONS),
        "total_combinations": len(DIAGNOSIS_GROUPS) * len(COMPLICATIONS),
        "groups": [
            {"id": d["id"], "label": d["label"], "weight": d["weight"]}
            for d in DIAGNOSIS_GROUPS
        ],
        "complications_list": [
            {"id": c["id"], "label": c["label"]}
            for c in COMPLICATIONS
        ],
    }


# ── Quick sanity check ────────────────────────────────────────────────────────

if __name__ == "__main__":
    stats = get_catalog_stats()
    print(f"Catalog: {stats['total_combinations']} combinations "
          f"({stats['diagnosis_groups']} diagnoses × {stats['complications']} complications)\n")

    print("=== Sample 5 weighted scenarios (seed=42) ===")
    for e in sample_scenarios(5, mode="weighted", seed=42):
        print(f"  [{e.diagnosis_label}] + [{e.complication_label}]")
        print(f"  → {e.topic_string}")
        print()

    print("=== Sample 5 random scenarios (seed=99) ===")
    for e in sample_scenarios(5, mode="random", seed=99):
        print(f"  [{e.diagnosis_label}] + [{e.complication_label}]")
        print(f"  → {e.topic_string}")
        print()
