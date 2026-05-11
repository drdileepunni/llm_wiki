#!/usr/bin/env python3
"""
Compute inter-rater agreement metrics from reviewer result files.

Usage:
  python compute_kappa.py                        # reads results/*.json
  python compute_kappa.py --out report.csv       # also write CSV
  python compute_kappa.py --verbose              # show per-order disagreements
"""
import argparse
import csv
import json
import sys
from itertools import combinations
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"

RATING_MAP  = {"appropriate": 2, "uncertain": 1, "inappropriate": 0}
ORDER_CATS  = ["med", "lab", "procedure", "monitoring", "vents", "comm", "diet", "blood"]
CAT_LABELS  = {
    "med": "Medications", "lab": "Investigations", "procedure": "Procedures",
    "monitoring": "Monitoring", "vents": "Ventilation", "comm": "Consultations",
    "diet": "Nutrition & Diet", "blood": "Blood Products",
}


# ─── Cohen's kappa ────────────────────────────────────────────────────────────
def cohen_kappa(a_ratings: list, b_ratings: list) -> float | None:
    """
    Compute Cohen's kappa for two lists of categorical ratings.
    Ignores positions where either value is None.
    Categories: appropriate / uncertain / inappropriate
    """
    pairs = [(a, b) for a, b in zip(a_ratings, b_ratings) if a is not None and b is not None]
    n = len(pairs)
    if n < 2:
        return None

    cats = list(RATING_MAP.keys())
    po = sum(a == b for a, b in pairs) / n

    pe = sum(
        (sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n)
        for c in cats
    )

    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def pct_agree(a_ratings: list, b_ratings: list) -> float | None:
    pairs = [(a, b) for a, b in zip(a_ratings, b_ratings) if a is not None and b is not None]
    if not pairs:
        return None
    return sum(a == b for a, b in pairs) / len(pairs) * 100


def mean_kappa(kappas: list) -> float | None:
    valid = [k for k in kappas if k is not None]
    return sum(valid) / len(valid) if valid else None


def kappa_label(k: float | None) -> str:
    if k is None: return "—"
    if k < 0:     return f"{k:.3f}  (poor)"
    if k < 0.20:  return f"{k:.3f}  (slight)"
    if k < 0.40:  return f"{k:.3f}  (fair)"
    if k < 0.60:  return f"{k:.3f}  (moderate)"
    if k < 0.80:  return f"{k:.3f}  (substantial)"
    return            f"{k:.3f}  (almost perfect)"


# ─── Load results ─────────────────────────────────────────────────────────────
def load_results(results_dir: Path) -> list[dict]:
    files = sorted(results_dir.glob("*.json"))
    if not files:
        print(f"No result files found in {results_dir}")
        sys.exit(1)
    results = []
    for f in files:
        try:
            r = json.loads(f.read_text())
            results.append(r)
            print(f"  Loaded: {f.name}  ({r['reviewer_name']} — {r['reviewer_role']})")
        except Exception as e:
            print(f"  Skip {f.name}: {e}")
    return results


# ─── Build rating matrix ──────────────────────────────────────────────────────
def build_order_matrix(results: list[dict]) -> dict:
    """
    Returns { order_id: { reviewer_name: rating, "order_type": ..., "display": ..., "scenario_id": ... } }
    """
    matrix = {}
    for r in results:
        name = r["reviewer_name"]
        for scenario in r.get("scenarios", []):
            sid = scenario["scenario_id"]
            for turn in scenario.get("turns", []):
                for ore in turn.get("order_ratings", []):
                    oid = ore["id"]
                    if oid not in matrix:
                        matrix[oid] = {
                            "order_type":  ore.get("order_type", ""),
                            "display":     ore.get("display", ""),
                            "scenario_id": sid,
                        }
                    matrix[oid][name] = ore.get("rating")
    return matrix


def build_category_matrix(results: list[dict]) -> dict:
    """
    Returns { (scenario_id, cat): { reviewer_name: rating } }
    """
    matrix = {}
    for r in results:
        name = r["reviewer_name"]
        for scenario in r.get("scenarios", []):
            sid = scenario["scenario_id"]
            for cat, cr in scenario.get("category_ratings", {}).items():
                key = (sid, cat)
                if key not in matrix:
                    matrix[key] = {"scenario_id": sid, "category": cat}
                matrix[key][name] = cr.get("rating")
    return matrix


def build_overall_matrix(results: list[dict]) -> dict:
    """Returns { scenario_id: { reviewer_name: rating } }"""
    matrix = {}
    for r in results:
        name = r["reviewer_name"]
        for scenario in r.get("scenarios", []):
            sid = scenario["scenario_id"]
            if sid not in matrix:
                matrix[sid] = {"scenario_id": sid}
            matrix[sid][name] = scenario.get("overall", {}).get("rating")
    return matrix


# ─── Analysis ─────────────────────────────────────────────────────────────────
def analyse(results: list[dict], verbose: bool = False) -> dict:
    reviewer_names = [r["reviewer_name"] for r in results]
    pairs          = list(combinations(reviewer_names, 2))

    order_matrix    = build_order_matrix(results)
    category_matrix = build_category_matrix(results)
    overall_matrix  = build_overall_matrix(results)

    report = {}

    # ── 1. Overall order-level kappa ──────────────────────────────────────────
    pair_kappas = {}
    for a, b in pairs:
        a_vals = [row.get(a) for row in order_matrix.values()]
        b_vals = [row.get(b) for row in order_matrix.values()]
        pair_kappas[(a, b)] = {
            "kappa": cohen_kappa(a_vals, b_vals),
            "pct":   pct_agree(a_vals, b_vals),
        }
    report["order_level"] = {
        "n_orders": len(order_matrix),
        "pairs":    {f"{a} vs {b}": v for (a, b), v in pair_kappas.items()},
        "mean_kappa": mean_kappa([v["kappa"] for v in pair_kappas.values()]),
    }

    # ── 2. Per-category kappa ─────────────────────────────────────────────────
    cat_kappas = {}
    for cat in ORDER_CATS:
        rows = [row for row in order_matrix.values() if row.get("order_type") == cat]
        if not rows:
            continue
        cat_pairs = {}
        for a, b in pairs:
            a_v = [row.get(a) for row in rows]
            b_v = [row.get(b) for row in rows]
            cat_pairs[(a, b)] = cohen_kappa(a_v, b_v)
        cat_kappas[cat] = {
            "n_orders":    len(rows),
            "mean_kappa":  mean_kappa(list(cat_pairs.values())),
            "pairs":       {f"{a} vs {b}": k for (a, b), k in cat_pairs.items()},
        }
    report["per_category"] = cat_kappas

    # ── 3. Per-scenario kappa ─────────────────────────────────────────────────
    scenario_ids = list({row["scenario_id"] for row in order_matrix.values()})
    scen_kappas  = {}
    for sid in scenario_ids:
        rows = [row for row in order_matrix.values() if row["scenario_id"] == sid]
        sp = {}
        for a, b in pairs:
            a_v = [row.get(a) for row in rows]
            b_v = [row.get(b) for row in rows]
            sp[(a, b)] = cohen_kappa(a_v, b_v)
        scen_kappas[sid] = {
            "n_orders":   len(rows),
            "mean_kappa": mean_kappa(list(sp.values())),
        }
    report["per_scenario"] = scen_kappas

    # ── 4. Category-level (summary) kappa ────────────────────────────────────
    cat_sum_kappas = {}
    for cat in ORDER_CATS:
        rows = [row for row in category_matrix.values() if row.get("category") == cat]
        if not rows:
            continue
        cp = {}
        for a, b in pairs:
            a_v = [row.get(a) for row in rows]
            b_v = [row.get(b) for row in rows]
            cp[(a, b)] = cohen_kappa(a_v, b_v)
        cat_sum_kappas[cat] = {
            "n_scenarios": len(rows),
            "mean_kappa":  mean_kappa(list(cp.values())),
        }
    report["category_summary_level"] = cat_sum_kappas

    # ── 5. Overall case-level kappa ───────────────────────────────────────────
    ov_pairs = {}
    for a, b in pairs:
        a_v = [row.get(a) for row in overall_matrix.values()]
        b_v = [row.get(b) for row in overall_matrix.values()]
        ov_pairs[(a, b)] = cohen_kappa(a_v, b_v)
    report["case_level"] = {
        "n_scenarios": len(overall_matrix),
        "mean_kappa":  mean_kappa(list(ov_pairs.values())),
        "pairs":       {f"{a} vs {b}": k for (a, b), k in ov_pairs.items()},
    }

    # ── 6. Flagged orders (high disagreement) ────────────────────────────────
    if verbose:
        flagged = []
        for oid, row in order_matrix.items():
            rater_vals = [row.get(n) for n in reviewer_names if row.get(n)]
            if len(set(rater_vals)) > 1:  # any disagreement
                flagged.append({
                    "id":       oid,
                    "display":  row["display"],
                    "category": row["order_type"],
                    "ratings":  {n: row.get(n) for n in reviewer_names},
                })
        report["flagged_orders"] = flagged

    return report


# ─── Print ────────────────────────────────────────────────────────────────────
def print_report(report: dict):
    W = 72
    print("\n" + "═" * W)
    print("  CDS VALIDATION — INTER-RATER AGREEMENT REPORT")
    print("═" * W)

    ol = report["order_level"]
    print(f"\n  ORDER-LEVEL  ({ol['n_orders']} orders total)")
    print(f"  {'Mean kappa':<30} {kappa_label(ol['mean_kappa'])}")
    for pair, v in ol["pairs"].items():
        pct = f"{v['pct']:.1f}% agree" if v["pct"] is not None else "—"
        print(f"    {pair:<40} κ = {v['kappa']:.3f}  ({pct})" if v["kappa"] is not None else f"    {pair:<40} —")

    print(f"\n  PER-CATEGORY ORDER KAPPA")
    print(f"  {'Category':<22} {'N':>5}  {'Mean κ':<8}  Label")
    print(f"  {'-'*60}")
    for cat, v in report["per_category"].items():
        label = CAT_LABELS.get(cat, cat)
        mk    = v["mean_kappa"]
        print(f"  {label:<22} {v['n_orders']:>5}  {kappa_label(mk)}")

    print(f"\n  PER-SCENARIO ORDER KAPPA")
    for sid, v in report["per_scenario"].items():
        print(f"  {sid[:40]:<42} n={v['n_orders']:>3}  κ = {kappa_label(v['mean_kappa'])}")

    cl = report["case_level"]
    print(f"\n  CASE-LEVEL  ({cl['n_scenarios']} scenarios)")
    print(f"  {'Mean kappa':<30} {kappa_label(cl['mean_kappa'])}")
    for pair, k in cl["pairs"].items():
        print(f"    {pair:<40} κ = {kappa_label(k)}")

    if "flagged_orders" in report:
        flagged = report["flagged_orders"]
        print(f"\n  FLAGGED ORDERS — disagreement ({len(flagged)} orders)")
        for o in flagged[:20]:  # cap at 20
            ratings_str = "  ".join(f"{n}: {v}" for n, v in o["ratings"].items() if v)
            print(f"    [{o['category']:<10}] {o['display'][:45]:<46}  {ratings_str}")

    print("\n" + "═" * W + "\n")


# ─── CSV export ───────────────────────────────────────────────────────────────
def write_csv(report: dict, out_path: Path):
    rows = []

    rows.append(["Section", "Item", "N", "Mean Kappa", "Label"])
    ol = report["order_level"]
    rows.append(["Order-level", "Overall", ol["n_orders"],
                 f"{ol['mean_kappa']:.3f}" if ol["mean_kappa"] is not None else "",
                 kappa_label(ol["mean_kappa"])])

    for cat, v in report["per_category"].items():
        mk = v["mean_kappa"]
        rows.append(["Per-category", CAT_LABELS.get(cat, cat), v["n_orders"],
                     f"{mk:.3f}" if mk is not None else "", kappa_label(mk)])

    for sid, v in report["per_scenario"].items():
        mk = v["mean_kappa"]
        rows.append(["Per-scenario", sid, v["n_orders"],
                     f"{mk:.3f}" if mk is not None else "", kappa_label(mk)])

    cl = report["case_level"]
    rows.append(["Case-level", "Overall", cl["n_scenarios"],
                 f"{cl['mean_kappa']:.3f}" if cl["mean_kappa"] is not None else "",
                 kappa_label(cl["mean_kappa"])])

    with open(out_path, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"  CSV written → {out_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(RESULTS_DIR), help="Directory of reviewer JSON files")
    ap.add_argument("--out",     default=None,             help="Optional CSV output path")
    ap.add_argument("--verbose", action="store_true",      help="Show per-order disagreements")
    args = ap.parse_args()

    results_dir = Path(args.results)
    print(f"\nLoading reviewer files from {results_dir}…")
    results = load_results(results_dir)

    if len(results) < 2:
        print("Need at least 2 reviewer files to compute kappa.")
        sys.exit(1)

    report = analyse(results, verbose=args.verbose)
    print_report(report)

    if args.out:
        write_csv(report, Path(args.out))


if __name__ == "__main__":
    main()
