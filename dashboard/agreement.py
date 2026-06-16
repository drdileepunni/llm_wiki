"""
Inter-rater agreement metrics for the CDS pipeline dashboard.

Metrics (all ordinal 1-5 scale):
  - Krippendorff's alpha (ordinal) — handles variable raters per alert
  - % exact agreement  — fraction of alert pairs with identical ratings
  - % within-1 agreement — fraction within ±1
  - Mean pairwise quadratic-weighted Cohen's kappa

Returns a dict with all metrics + bookkeeping counts.
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any


# ── Krippendorff alpha (ordinal metric: d(v1,v2) = (v1-v2)^2) ─────────────────

def _ordinal_d(v1: int, v2: int) -> float:
    return float((v1 - v2) ** 2)


def krippendorff_alpha(ratings_by_unit: dict[str, list[int]]) -> float | None:
    """
    Compute Krippendorff's alpha for ordinal data.

    ratings_by_unit: {alert_id: [r1, r2, ...]}  — variable raters per alert.
    Only units with ≥2 ratings contribute to D_o; all ratings contribute to D_e.
    Returns None if insufficient data.
    """
    # All ratings (for D_e)
    all_vals = [r for rs in ratings_by_unit.values() for r in rs]
    n = len(all_vals)
    if n < 2:
        return None

    # Units with ≥2 raters (for D_o)
    multi = {u: rs for u, rs in ratings_by_unit.items() if len(rs) >= 2}
    if not multi:
        return None

    # D_o: observed disagreement
    # = [Σ_u Σ_{c≠c'} d(r_cu, r_c'u) / (n_u*(n_u-1))] / N_multi
    d_o_sum = 0.0
    for rs in multi.values():
        n_u = len(rs)
        unit_d = sum(_ordinal_d(rs[i], rs[j]) for i in range(n_u) for j in range(n_u) if i != j)
        d_o_sum += unit_d / (n_u * (n_u - 1))
    D_o = d_o_sum / len(multi)

    # D_e: expected disagreement under chance
    # = Σ_{i≠j} d(v_i, v_j) / (n*(n-1))
    d_e_sum = sum(
        _ordinal_d(all_vals[i], all_vals[j])
        for i in range(n) for j in range(n) if i != j
    )
    D_e = d_e_sum / (n * (n - 1))

    if D_e == 0:
        return 1.0
    return round(1.0 - D_o / D_e, 4)


# ── Cohen's kappa (quadratic weighted, 1-5 scale) ─────────────────────────────

def _weighted_kappa_pair(a_ratings: list[int], b_ratings: list[int], max_scale: int = 5) -> float | None:
    """Quadratic-weighted Cohen's kappa for two raters on a shared set of alerts."""
    pairs = [(a, b) for a, b in zip(a_ratings, b_ratings) if a and b]
    n = len(pairs)
    if n < 2:
        return None

    categories = list(range(1, max_scale + 1))
    k = len(categories)

    # Weight matrix: w[i][j] = 1 - ((i-j)/(k-1))^2
    def w(i, j):
        return 1 - ((categories[i] - categories[j]) / (max_scale - 1)) ** 2

    # Confusion matrix
    conf = [[0] * k for _ in range(k)]
    for a, b in pairs:
        if 1 <= a <= max_scale and 1 <= b <= max_scale:
            conf[a - 1][b - 1] += 1

    row_totals = [sum(conf[i][j] for j in range(k)) for i in range(k)]
    col_totals = [sum(conf[i][j] for i in range(k)) for j in range(k)]

    # Observed and expected weighted agreement
    Po = sum(w(i, j) * conf[i][j] for i in range(k) for j in range(k)) / n
    Pe = sum(w(i, j) * row_totals[i] * col_totals[j] for i in range(k) for j in range(k)) / (n ** 2)

    if Pe == 1:
        return 1.0 if Po == 1 else None
    return round((Po - Pe) / (1 - Pe), 4)


# ── % agreement helpers ────────────────────────────────────────────────────────

def _pct_agreements(ratings_by_unit: dict[str, list[int]]) -> tuple[float, float]:
    """Returns (pct_exact, pct_within_1) over all unique pairs within each unit."""
    exact = within1 = total = 0
    for rs in ratings_by_unit.values():
        if len(rs) < 2:
            continue
        for i, j in combinations(range(len(rs)), 2):
            diff = abs(rs[i] - rs[j])
            total += 1
            if diff == 0:
                exact += 1
                within1 += 1
            elif diff == 1:
                within1 += 1
    if total == 0:
        return 0.0, 0.0
    return round(exact / total * 100, 1), round(within1 / total * 100, 1)


# ── main entry point ──────────────────────────────────────────────────────────

def compute_agreement(ratings_by_alert: dict[str, list[int]]) -> dict[str, Any]:
    """
    Compute all inter-rater agreement metrics.

    Input: {alert_id: [rating_from_rater1, rating_from_rater2, ...]}
    Returns a dict of metrics + metadata.
    """
    total_alerts   = len(ratings_by_alert)
    multi_rater    = {k: v for k, v in ratings_by_alert.items() if len(v) >= 2}
    n_multi        = len(multi_rater)
    total_ratings  = sum(len(v) for v in ratings_by_alert.values())

    if n_multi < 2:
        return {
            "sufficient_data": False,
            "total_alerts":    total_alerts,
            "multi_rater_alerts": n_multi,
            "total_ratings":   total_ratings,
            "message": (
                f"Need ≥2 alerts with multiple raters for agreement stats "
                f"(currently {n_multi} multi-rated alert(s) across {total_ratings} total rating(s))."
            ),
        }

    alpha = krippendorff_alpha(multi_rater)
    pct_exact, pct_within1 = _pct_agreements(multi_rater)

    # Mean pairwise kappa: build per-rater rating vectors over shared alerts
    # Collect raters and their ratings per alert
    rater_vectors: dict[str, dict[str, int]] = {}
    # We need per-rater data — but get_ratings_per_alert doesn't separate by rater.
    # We only have a list of ratings per alert here.  With the current single-rater
    # data shape, kappa isn't computable; we'll skip it gracefully.
    mean_kappa = None  # requires per-rater identity, computed in server if needed

    return {
        "sufficient_data":    True,
        "total_alerts":       total_alerts,
        "multi_rater_alerts": n_multi,
        "total_ratings":      total_ratings,
        "krippendorff_alpha": alpha,
        "pct_exact_agreement":    pct_exact,
        "pct_within1_agreement":  pct_within1,
        "mean_pairwise_kappa":    mean_kappa,
        "interpretation": _interpret_alpha(alpha),
    }


def _interpret_alpha(alpha: float | None) -> str:
    if alpha is None:
        return "N/A"
    if alpha >= 0.8:
        return "Strong agreement"
    if alpha >= 0.667:
        return "Acceptable agreement"
    if alpha >= 0.4:
        return "Moderate agreement"
    return "Poor agreement — consider calibration"
