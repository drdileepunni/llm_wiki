#!/usr/bin/env python3
"""
A/B Winner Analysis
===================
Reads all results/ab_reviewer_*.json files and computes:
  - Win / Tie / Loss counts for Arm A (wiki) vs Arm B (medgemma)
  - Per-turn and per-difficulty breakdowns
  - Step appropriateness ratings (good / unsure / bad per arm)
  - Side-preference bias check (blinding leak detector)
  - Cohen's kappa across reviewers (inter-rater agreement on arm preference)
  - Cost and marker stats from ab_results.json

Usage:
  python validation/compute_ab_winner.py
  python validation/compute_ab_winner.py --results data/ab_results_XYZ.json
  python validation/compute_ab_winner.py --verbose
  python validation/compute_ab_winner.py --csv report.csv
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

VALIDATION_DIR = Path(__file__).parent
RESULTS_DIR    = VALIDATION_DIR / "results"
DATA_DIR       = VALIDATION_DIR / "data"


# ── Kappa helpers ────────────────────────────────────────────────────────────

def cohen_kappa(r1: list, r2: list) -> float:
    """Compute Cohen's kappa between two rating lists of equal length."""
    cats = sorted(set(r1) | set(r2))
    n    = len(r1)
    if n == 0:
        return float("nan")

    # Observed agreement
    p_o = sum(a == b for a, b in zip(r1, r2)) / n

    # Expected agreement
    p_e = sum(
        (r1.count(c) / n) * (r2.count(c) / n)
        for c in cats
    )
    if p_e == 1.0:
        return 1.0
    return (p_o - p_e) / (1 - p_e)


def mean_kappa_pairwise(reviewer_ratings: dict[str, list]) -> float:
    """Mean pairwise Cohen's kappa across all reviewer pairs."""
    names = list(reviewer_ratings.keys())
    if len(names) < 2:
        return float("nan")
    kappas = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r1 = reviewer_ratings[names[i]]
            r2 = reviewer_ratings[names[j]]
            # Align on shared cases
            shared = set(r1.keys()) & set(r2.keys())
            if not shared:
                continue
            a = [r1[k] for k in sorted(shared)]
            b = [r2[k] for k in sorted(shared)]
            kappas.append(cohen_kappa(a, b))
    return sum(kappas) / len(kappas) if kappas else float("nan")


# ── Load data ────────────────────────────────────────────────────────────────

def load_reviewer_files() -> list[dict]:
    files = sorted(RESULTS_DIR.glob("ab_reviewer_*.json"))
    reviewers = []
    for f in files:
        try:
            reviewers.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"  ⚠ Could not read {f.name}: {e}")
    return reviewers


def load_ab_results(path: Path | None) -> dict | None:
    if path and path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    # Auto-find latest
    files = sorted(DATA_DIR.glob("ab_results_*.json"), reverse=True)
    if files:
        try:
            return json.loads(files[0].read_text())
        except Exception:
            pass
    return None


# ── Analysis ────────────────────────────────────────────────────────────────

def analyse(reviewers: list[dict], ab_results: dict | None, verbose: bool) -> dict:
    # ── 1. Preference counts ────────────────────────────────────────────────
    overall    = defaultdict(int)        # arm_a / arm_b / tie
    by_turn    = defaultdict(lambda: defaultdict(int))
    by_diff    = defaultdict(lambda: defaultdict(int))
    side_counts = defaultdict(int)       # "left" / "right" / "tie" — detect bias

    # per-reviewer ratings for kappa: {reviewer_name: {case_key: "arm_a"|"arm_b"|"tie"}}
    reviewer_ratings: dict[str, dict] = {}

    for rev in reviewers:
        name = rev.get("reviewer_name", "unknown")
        reviewer_ratings[name] = {}

        for case in rev.get("cases", []):
            sid   = case.get("scenario_id", "?")
            tnum  = case.get("turn_num", "?")
            pref  = case.get("preferred_arm", "tie")   # arm_a | arm_b | tie
            raw   = case.get("preferred", "tie")        # left | right | tie

            key = f"{sid}_t{tnum}"
            overall[pref] += 1
            by_turn[tnum][pref] += 1
            side_counts[raw] += 1
            reviewer_ratings[name][key] = pref

        # For verbose: look at disagreements
    if verbose:
        print("\n  Per-reviewer breakdown:")
        for rev in reviewers:
            counts = defaultdict(int)
            for c in rev.get("cases", []):
                counts[c.get("preferred_arm", "tie")] += 1
            print(f"    {rev.get('reviewer_name','?'):20s}  "
                  f"arm_a={counts['arm_a']}  tie={counts['tie']}  arm_b={counts['arm_b']}")

    # ── 2. Kappa ─────────────────────────────────────────────────────────────
    kappa = mean_kappa_pairwise(reviewer_ratings)

    # ── 3. Step appropriateness ──────────────────────────────────────────────
    step_ratings: dict[str, dict[str, int]] = {
        "arm_a": defaultdict(int),
        "arm_b": defaultdict(int),
    }
    for rev in reviewers:
        for case in rev.get("cases", []):
            sr = case.get("step_ratings", {})
            for arm in ("arm_a", "arm_b"):
                for rating_val in sr.get(arm, {}).values():
                    step_ratings[arm][rating_val] += 1

    # ── 4. Pipeline stats from ab_results ────────────────────────────────────
    pipeline_stats: dict = {}
    if ab_results:
        a_ok = [c for c in ab_results.get("cases", []) if c.get("arm_a", {}).get("ok")]
        b_ok = [c for c in ab_results.get("cases", []) if c.get("arm_b", {}).get("ok")]

        def _avg(cases, arm, key):
            vals = [c[arm][key] for c in cases if isinstance(c[arm].get(key), (int, float))]
            return sum(vals) / len(vals) if vals else 0.0

        pipeline_stats = {
            "arm_a_n_ok":           len(a_ok),
            "arm_b_n_ok":           len(b_ok),
            "arm_a_avg_steps":      _avg(a_ok, "arm_a", "markers_remaining"),  # renamed below
            "arm_b_avg_markers":    _avg(b_ok, "arm_b", "markers_remaining"),
            "arm_a_avg_markers":    _avg(a_ok, "arm_a", "markers_remaining"),
            "arm_b_avg_mg_resolved": _avg(b_ok, "arm_b", "markers_resolved_by_medgemma"),
            "arm_a_avg_cost_usd":   _avg(a_ok, "arm_a", "cost_usd"),
            "arm_b_avg_cost_usd":   _avg(b_ok, "arm_b", "cost_usd"),
            "arm_a_avg_pages":      sum(
                len(c["arm_a"].get("pages_consulted", [])) for c in a_ok
            ) / max(len(a_ok), 1),
        }

    return {
        "overall":        dict(overall),
        "by_turn":        {k: dict(v) for k, v in by_turn.items()},
        "by_difficulty":  {k: dict(v) for k, v in by_diff.items()},
        "side_counts":    dict(side_counts),
        "kappa":          kappa,
        "step_ratings":   {k: dict(v) for k, v in step_ratings.items()},
        "n_reviewers":    len(reviewers),
        "pipeline_stats": pipeline_stats,
        "reviewer_names": [r.get("reviewer_name","?") for r in reviewers],
    }


# ── Report ───────────────────────────────────────────────────────────────────

def print_report(a: dict, ab_results: dict | None):
    arm_a_label = "Arm A (wiki-grounded)"
    arm_b_label = "Arm B (medgemma-only)"
    if ab_results:
        arm_a_label += f"  [{ab_results.get('arm_a_config',{}).get('model','')}]"
        arm_b_label += f"  [{ab_results.get('arm_b_config',{}).get('model','')}]"

    total = sum(a["overall"].values())
    a_wins = a["overall"].get("arm_a", 0)
    b_wins = a["overall"].get("arm_b", 0)
    ties   = a["overall"].get("tie",   0)

    print("\n" + "═" * 62)
    print(" CDS A/B WINNER ANALYSIS")
    print("═" * 62)
    print(f"  Reviewers : {a['n_reviewers']}  ({', '.join(a['reviewer_names'])})")
    print(f"  Cases     : {total} preference ratings\n")

    # ── Primary outcome ────────────────────────────────────────────────────
    print("  PRIMARY OUTCOME — Blinded preference")
    print("  " + "─" * 42)
    _bar = lambda n: "█" * round(n / max(total, 1) * 30)
    for label, arm_key, cnt in [
        (arm_a_label, "arm_a", a_wins),
        ("Tie / Equal", "tie", ties),
        (arm_b_label, "arm_b", b_wins),
    ]:
        pct = 100 * cnt / total if total else 0
        print(f"  {label[:35]:<35} {cnt:3d} ({pct:4.1f}%)  {_bar(cnt)}")

    winner = "Arm A" if a_wins > b_wins else ("Arm B" if b_wins > a_wins else "Draw")
    print(f"\n  → Preferred: {winner}")

    # ── Blinding bias check ────────────────────────────────────────────────
    left_n  = a["side_counts"].get("left",  0)
    right_n = a["side_counts"].get("right", 0)
    tie_n   = a["side_counts"].get("tie",   0)
    non_tie = left_n + right_n
    if non_tie > 0:
        left_pct = 100 * left_n / non_tie
        bias_flag = "⚠️  Possible side bias" if abs(left_pct - 50) > 15 else "✓ No strong side bias"
        print(f"\n  BLINDING CHECK  (left={left_pct:.1f}%  right={100-left_pct:.1f}%  of non-tie): {bias_flag}")

    # ── Inter-rater agreement ──────────────────────────────────────────────
    kappa = a["kappa"]
    if not (kappa != kappa):   # not NaN
        if kappa >= 0.6:   interp = "substantial"
        elif kappa >= 0.4: interp = "moderate"
        elif kappa >= 0.2: interp = "fair"
        else:              interp = "slight"
        print(f"\n  INTER-RATER AGREEMENT  κ = {kappa:.3f}  ({interp})")
    else:
        print("\n  INTER-RATER AGREEMENT  κ = n/a  (need ≥2 reviewers)")

    # ── Step appropriateness ──────────────────────────────────────────────
    sr = a["step_ratings"]
    if any(sr[arm] for arm in ("arm_a","arm_b")):
        print("\n  STEP APPROPRIATENESS RATINGS  (✓good / ?unsure / ✗bad)")
        for arm, label in [("arm_a","Arm A"), ("arm_b","Arm B")]:
            g = sr[arm].get("good",  0)
            u = sr[arm].get("unsure",0)
            b = sr[arm].get("bad",   0)
            tot = g + u + b
            if tot == 0:
                continue
            print(f"  {label}: ✓{g} ({100*g//tot}%)  ?{u} ({100*u//tot}%)  ✗{b} ({100*b//tot}%)")

    # ── Pipeline stats ─────────────────────────────────────────────────────
    ps = a.get("pipeline_stats", {})
    if ps:
        print("\n  PIPELINE STATS  (from ab_results.json)")
        print(f"  Arm A avg markers remaining : {ps.get('arm_a_avg_markers',0):.2f}")
        print(f"  Arm B avg markers remaining : {ps.get('arm_b_avg_markers',0):.2f}")
        print(f"  Arm B avg MedGemma resolved : {ps.get('arm_b_avg_mg_resolved',0):.2f}")
        print(f"  Arm A avg wiki pages used   : {ps.get('arm_a_avg_pages',0):.1f}")
        print(f"  Arm A avg cost USD          : ${ps.get('arm_a_avg_cost_usd',0):.4f}")
        print(f"  Arm B avg cost USD          : ${ps.get('arm_b_avg_cost_usd',0):.4f}")

    print("\n" + "═" * 62 + "\n")


def write_csv(a: dict, out_path: Path):
    import csv
    rows = []
    for arm, wins in a["overall"].items():
        rows.append({"dimension": "overall", "category": arm, "count": wins})
    for turn, counts in a["by_turn"].items():
        for arm, n in counts.items():
            rows.append({"dimension": f"turn_{turn}", "category": arm, "count": n})
    for diff, counts in a["by_difficulty"].items():
        for arm, n in counts.items():
            rows.append({"dimension": f"difficulty_{diff}", "category": arm, "count": n})
    # Step ratings
    for arm, ratings in a["step_ratings"].items():
        for rating, n in ratings.items():
            rows.append({"dimension": f"step_rating_{arm}", "category": rating, "count": n})

    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["dimension","category","count"])
        w.writeheader()
        w.writerows(rows)
    print(f"  CSV → {out_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Compute A/B test winner")
    ap.add_argument("--results", default=None,
                    help="ab_results_*.json path (default: latest in data/)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--csv", default=None, help="Write summary to CSV file")
    args = ap.parse_args()

    reviewers = load_reviewer_files()
    if not reviewers:
        print("\n  No reviewer files found in results/")
        print("  Run:  python serve_ab.py  → open in browser → complete ratings → submit")
        sys.exit(0)

    ab_results = load_ab_results(Path(args.results) if args.results else None)

    analysis = analyse(reviewers, ab_results, verbose=args.verbose)
    print_report(analysis, ab_results)

    if args.csv:
        write_csv(analysis, Path(args.csv))

    return analysis


if __name__ == "__main__":
    main()
