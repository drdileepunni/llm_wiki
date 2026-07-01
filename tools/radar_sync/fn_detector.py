"""
False-negative safety net — Detector 3 runs AFTER the problem tracker
to catch cases where the agent may have missed a real clinical deterioration.

Detector 3 — NEWS2 cooldown override
  If a problem alert was suppressed by the 8-hour cooldown but the patient's NEWS2
  score has risen meaningfully since the last alert, break the cooldown and fire.

All detections are written to the `fn_detections` MongoDB collection with enough
context to reconstruct why the alert fired. Query pattern:
  db.fn_detections.find({CPMRN: "...", encounter: 1}).sort({detected_at: -1})
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
# Cooldown override fires if EITHER condition is met:
#   • Any single NEWS2 component rises by ≥ _NEWS2_COMPONENT_DELTA points, OR
#   • Total NEWS2 score rises by ≥ _NEWS2_TOTAL_DELTA points
_NEWS2_COMPONENT_DELTA = 3   # single-parameter acute change (e.g. HR: 0 → 3)
_NEWS2_TOTAL_DELTA     = 6   # broad multi-parameter drift
_CHART_BASE            = "https://cloudphysicianworld.com/patient"

# ── NEWS2 scoring ──────────────────────────────────────────────────────────────

def _parse_systolic(bp_str: str) -> float | None:
    """Extract systolic from '120/80' or '120'."""
    if not bp_str:
        return None
    try:
        return float(str(bp_str).split("/")[0])
    except (ValueError, TypeError):
        return None


def _score_hr(hr: float) -> int:
    if hr <= 40 or hr >= 131:
        return 3
    if 111 <= hr <= 130:
        return 2
    if 41 <= hr <= 50 or 91 <= hr <= 110:
        return 1
    return 0  # 51–90


def _score_rr(rr: float) -> int:
    if rr <= 8 or rr >= 25:
        return 3
    if 21 <= rr <= 24:
        return 2
    if 9 <= rr <= 11:
        return 1
    return 0  # 12–20


def _score_spo2(spo2: float) -> int:
    """
    NEWS2 Scale 1 — used for all patients (we assume non-COPD in ICU context).
    Supplemental O2 is scored separately via _score_supplemental_o2().
    """
    if spo2 <= 91:
        return 3
    if spo2 <= 93:
        return 2
    if spo2 <= 95:
        return 1
    return 0


def _score_bp(systolic: float) -> int:
    if systolic <= 90:
        return 3
    if systolic <= 100:
        return 2
    if systolic <= 109:
        return 1
    return 0  # ≥110


def _score_temp(temp: float) -> int:
    if temp <= 35.0:
        return 3
    if temp <= 36.0:
        return 1
    if temp >= 39.1:
        return 2
    if temp >= 38.1:
        return 1
    return 0  # 36.1–38.0


def _score_supplemental_o2(fio2: float | None) -> int:
    """2 points if on supplemental O2 (FiO2 > 21%)."""
    if fio2 and fio2 > 21:
        return 2
    return 0


def compute_news2(vitals_row: dict) -> tuple[int, dict]:
    """
    Compute a NEWS2 score from a single vitals dict (one row from chart.vitals[]).
    Returns (total_score, component_breakdown).
    """
    components: dict[str, int] = {}

    hr_raw = vitals_row.get("daysHR")
    try:
        components["HR"] = _score_hr(float(hr_raw))
    except (TypeError, ValueError):
        components["HR"] = 0

    rr_raw = vitals_row.get("daysRR")
    try:
        components["RR"] = _score_rr(float(rr_raw))
    except (TypeError, ValueError):
        components["RR"] = 0

    bp_raw  = vitals_row.get("daysBP")
    sys_val = _parse_systolic(str(bp_raw) if bp_raw else "")
    components["BP"] = _score_bp(sys_val) if sys_val is not None else 0

    fio2_raw = vitals_row.get("daysFiO2")
    try:
        fio2 = float(fio2_raw)
    except (TypeError, ValueError):
        fio2 = None
    on_o2 = bool(fio2 and fio2 > 21)
    components["O2"] = _score_supplemental_o2(fio2)

    spo2_raw = vitals_row.get("daysSpO2")
    try:
        components["SpO2"] = _score_spo2(float(spo2_raw))
    except (TypeError, ValueError):
        components["SpO2"] = 0

    temp_raw = vitals_row.get("daysTemp")
    try:
        components["Temp"] = _score_temp(float(temp_raw))
    except (TypeError, ValueError):
        components["Temp"] = 0

    # Consciousness — default 0 (Alert); we don't have AVPU from the vitals struct
    components["AVPU"] = 0

    total = sum(components.values())
    return total, components


def _coerce_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def is_vital_row_normal(vrow: dict) -> bool:
    """
    Return True if a single vitals row is clinically unremarkable — i.e. nothing
    needs LLM attention this cycle.

    Uses NEWS2 scoring with the supplemental-O2 component stripped: a stable
    ventilated patient with otherwise-normal vitals should be skippable.
    Also blocks on Netra camera abnormal_list flags and any GCS < 15 (NEWS2
    doesn't capture altered consciousness from the vitals row alone).
    """
    _, components = compute_news2(vrow)
    total_no_o2 = sum(v for k, v in components.items() if k != "O2")
    if total_no_o2 != 0:
        return False
    if vrow.get("abnormal_list"):
        return False
    gcs = _coerce_float(vrow.get("daysGCS"))
    if gcs is not None and gcs < 15:
        return False
    return True


def all_new_vitals_normal(new_vitals: list[dict]) -> bool:
    """Return True if the latest new vital row is within normal bounds (no LLM needed).
    Only the most recent row is evaluated — older rows in the same delta window are ignored."""
    if not new_vitals:
        return True
    return is_vital_row_normal(new_vitals[0])


def _news2_delta_exceeds_threshold(
    current_score: int,
    current_components: dict,
    baseline_score: int,
    baseline_components: dict,
) -> tuple[bool, str]:
    """Return (exceeded, reason). exceeded=True means run the expensive analysis."""
    total_delta = current_score - baseline_score
    all_keys = set(list(current_components.keys()) + list(baseline_components.keys()))
    component_deltas = {
        k: current_components.get(k, 0) - baseline_components.get(k, 0)
        for k in all_keys
    }
    max_component_delta = max(component_deltas.values(), default=0)
    worst_component     = max(component_deltas, key=component_deltas.get, default="?")

    if total_delta >= _NEWS2_TOTAL_DELTA or max_component_delta >= _NEWS2_COMPONENT_DELTA:
        return True, (
            f"NEWS2 rose: total {baseline_score}→{current_score} (+{total_delta}), "
            f"worst component {worst_component} +{max_component_delta}"
        )
    return False, (
        f"NEWS2 stable {baseline_score}→{current_score} "
        f"(Δtotal={total_delta}, max_component_Δ={max_component_delta} on {worst_component})"
    )


def check_vitals_news2_delta(
    new_vitals: list[dict],
    cpmrn: str,
    encounter: int,
    db: Any,
) -> tuple[bool, str]:
    """
    Smart skip gate — called when the latest vital row is abnormal.
    Applies to ALL patients (not just those in cooldown).

    Returns (should_skip, detail_string).

    Two baselines are checked against `snapshot_schedule`:
      1. last-run baseline  (news2_last_run_*) — catches acute run-to-run changes.
      2. 6h baseline        (news2_baseline_6h_*) — catches slow drift.

    If EITHER baseline shows a meaningful NEWS2 change (component ≥ _NEWS2_COMPONENT_DELTA
    or total ≥ _NEWS2_TOTAL_DELTA), should_skip=False → expensive run.
    If both baselines show stable vitals → should_skip=True → skip expensive run.
    If no baseline exists yet (first run for this patient) → should_skip=False (safe fallback).
    """
    if not new_vitals:
        return True, "no new vitals"

    current_row = new_vitals[0]
    current_score, current_components = compute_news2(current_row)

    sched = db.snapshot_schedule.find_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"news2_last_run_score": 1, "news2_last_run_components": 1,
         "news2_baseline_6h_score": 1, "news2_baseline_6h_components": 1,
         "news2_baseline_6h_at": 1},
    ) or {}

    last_score      = sched.get("news2_last_run_score")
    last_components = sched.get("news2_last_run_components")
    b6h_score       = sched.get("news2_baseline_6h_score")
    b6h_components  = sched.get("news2_baseline_6h_components")

    if last_score is None or last_components is None:
        return False, "no prior NEWS2 baseline — running full analysis"

    # Check 1: delta vs last run
    exceeded, reason = _news2_delta_exceeds_threshold(
        current_score, current_components, last_score, last_components,
    )
    if exceeded:
        return False, f"vs last run: {reason} — running full analysis"

    # Check 2: delta vs 6h baseline (if available)
    if b6h_score is not None and b6h_components is not None:
        exceeded, reason = _news2_delta_exceeds_threshold(
            current_score, current_components, b6h_score, b6h_components,
        )
        if exceeded:
            return False, f"vs 6h baseline: {reason} — running full analysis (drift detected)"

    # Both baselines stable — safe to skip
    detail = (
        f"NEWS2 stable vs last run ({last_score}→{current_score}) "
        f"and vs 6h baseline ({b6h_score}→{current_score}) — skipping expensive run"
    )
    return True, detail


def write_news2_run_snapshot(
    new_vitals: list[dict],
    cpmrn: str,
    encounter: int,
    db: Any,
) -> None:
    """
    Write the current NEWS2 score + components to snapshot_schedule after every
    vitals gate evaluation. Called unconditionally whenever new vitals are present.

    Updates:
      news2_last_run_score/components  — always (captures this run's baseline)
      news2_baseline_6h_score/components/at — only when the stored value is >6h old
        (or missing), so it naturally freezes a snapshot from ~6h ago.
    """
    if not new_vitals:
        return
    try:
        now = datetime.now(timezone.utc)
        current_row = new_vitals[0]
        current_score, current_components = compute_news2(current_row)

        sched = db.snapshot_schedule.find_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"news2_baseline_6h_at": 1},
        ) or {}

        b6h_at = sched.get("news2_baseline_6h_at")
        if isinstance(b6h_at, str):
            try:
                b6h_at = datetime.fromisoformat(b6h_at.replace("Z", "+00:00"))
            except ValueError:
                b6h_at = None
        if b6h_at is not None and b6h_at.tzinfo is None:
            b6h_at = b6h_at.replace(tzinfo=timezone.utc)

        update_fields: dict = {
            "news2_last_run_score":      current_score,
            "news2_last_run_components": current_components,
        }
        # Update 6h baseline only when missing or older than 6h
        if b6h_at is None or (now - b6h_at).total_seconds() >= 6 * 3600:
            update_fields["news2_baseline_6h_score"]      = current_score
            update_fields["news2_baseline_6h_components"] = current_components
            update_fields["news2_baseline_6h_at"]         = now

        db.snapshot_schedule.update_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"$set": update_fields},
        )
    except Exception:
        logger.exception(
            "fn_detector: write_news2_run_snapshot failed for %s enc=%d", cpmrn, encounter
        )


def _get_latest_vitals_row(cpmrn: str, encounter: int, db: Any) -> dict | None:
    """Return the most recent vitals row from the latest snapshot."""
    snap = db.snapshots.find_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.vitals": 1},
        sort=[("snapshot_at", -1)],
    )
    if not snap:
        return None
    vitals = (snap.get("chart") or {}).get("vitals") or []
    return vitals[0] if vitals else None


# ── Alert sender ───────────────────────────────────────────────────────────────

def _send_fn_alert(cpmrn: str, encounter: int, detector: str, message: str, db: Any) -> bool:
    """Send a Google Chat alert for a false-negative detection."""
    try:
        import requests
        cfg = db["app_settings"].find_one({"_id": "gchat_webhook"})
        if not (cfg and cfg.get("enabled") and cfg.get("url")):
            return False

        header = {
            "news2_cooldown_override": "🔶 NEWS2 Escalation — Cooldown Override",
        }.get(detector, "🔶 FN Detector Alert")

        text = (
            f"*{header}*\n"
            f"Patient: *{cpmrn}*  |  Encounter {encounter}\n\n"
            f"{message}\n\n"
            f"🔗 {_CHART_BASE}/{cpmrn}/{encounter}"
        )

        resp = requests.post(cfg["url"], json={"text": text}, timeout=10)
        resp.raise_for_status()
        logger.info("fn_detector: alert sent [%s] for %s enc=%d", detector, cpmrn, encounter)
        return True
    except Exception:
        logger.exception("fn_detector: alert send failed [%s] for %s enc=%d", detector, cpmrn, encounter)
        return False


def _store_detection(detection: dict, db: Any) -> None:
    """Persist a detection event to fn_detections for traceability."""
    try:
        db.fn_detections.insert_one(detection)
    except Exception:
        logger.exception("fn_detector: failed to store detection")


# ── Detector 3: NEWS2 cooldown override ───────────────────────────────────────

def _check_news2_override(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    now: datetime,
    db: Any,
) -> list[dict]:
    """
    For each problem that is worsening/critical AND within cooldown,
    compute current NEWS2. If it has risen ≥ _NEWS2_ESCALATION_DELTA since
    the last alert, break the cooldown.
    """
    detections = []

    vitals_row = _get_latest_vitals_row(cpmrn, encounter, db)
    if not vitals_row:
        return detections

    current_score, components = compute_news2(vitals_row)

    # Find all worsening/critical problems that were recently alerted (in cooldown)
    problems = db.patient_problems.find({
        "CPMRN":    cpmrn,
        "encounter": encounter,
        "clinical_status": {"$in": ["worsening", "critical"]},
        "last_alerted_at": {"$exists": True, "$ne": None},
    })

    for prob in problems:
        problem_name    = prob["problem_name"]
        last_alerted_at = prob.get("last_alerted_at")
        if not isinstance(last_alerted_at, datetime):
            continue
        if last_alerted_at.tzinfo is None:
            last_alerted_at = last_alerted_at.replace(tzinfo=timezone.utc)

        hours_since = (now - last_alerted_at).total_seconds() / 3600

        # Skip if the problem_tracker just alerted in this same cycle (within 5 min).
        # The override is for cooldown-suppressed problems only — not double-alerting
        # something that already fired moments ago.
        if hours_since < (5 / 60):
            continue

        # Only act within the cooldown window (< _ALERT_COOLDOWN_H)
        if hours_since >= 8:
            continue

        last_news2            = prob.get("last_alerted_news2")
        last_news2_components = prob.get("last_alerted_news2_components") or {}

        # If no baseline was ever recorded, we cannot compute a meaningful delta.
        # Skip rather than comparing against 0 (which would fire on any chronic abnormality).
        if last_news2 is None:
            continue

        total_delta = current_score - last_news2

        # Per-component deltas (only where we have a prior reading for that component)
        component_deltas = {
            k: components[k] - last_news2_components.get(k, 0)
            for k in components
        }
        max_component_delta   = max(component_deltas.values(), default=0)
        worst_component       = max(component_deltas, key=component_deltas.get, default="")

        total_gate     = total_delta     >= _NEWS2_TOTAL_DELTA
        component_gate = max_component_delta >= _NEWS2_COMPONENT_DELTA
        triggered      = total_gate or component_gate

        detection: dict = {
            "CPMRN":                     cpmrn,
            "encounter":                 encounter,
            "detected_at":               now,
            "detector":                  "news2_cooldown_override",
            "problem_name":              problem_name,
            "news2_score_now":           current_score,
            "news2_score_at_last_alert": last_news2,
            "news2_total_delta":         total_delta,
            "news2_component_deltas":    component_deltas,
            "news2_components_now":      components,
            "news2_components_prev":     last_news2_components,
            "hours_since_alert":         round(hours_since, 2),
            "last_alerted_at":           last_alerted_at,
            "alert_sent":                False,
            "alert_reason":              "",
            "suppressed_reason":         "",
        }

        if triggered:
            component_lines = "  ".join(
                f"{k} +{v}pts" for k, v in components.items() if v > 0
            )
            if component_gate and not total_gate:
                trigger_desc = (
                    f"{worst_component} rose *+{max_component_delta} pts* "
                    f"(threshold ≥{_NEWS2_COMPONENT_DELTA} for a single parameter)"
                )
            elif total_gate and not component_gate:
                trigger_desc = (
                    f"total NEWS2 rose *+{total_delta} pts* "
                    f"(threshold ≥{_NEWS2_TOTAL_DELTA})"
                )
            else:
                trigger_desc = (
                    f"total NEWS2 rose *+{total_delta} pts* AND "
                    f"{worst_component} rose *+{max_component_delta} pts*"
                )

            msg = (
                f"Problem: *{problem_name}* (alert suppressed by 8h cooldown)\n"
                f"NEWS2: {last_news2} → {current_score} (*+{total_delta} pts total*) "
                f"since last alert {hours_since:.1f}h ago\n"
                f"Trigger: {trigger_desc}\n"
                f"Components now: {component_lines or 'no individual flags'}"
            )
            sent = _send_fn_alert(cpmrn, encounter, "news2_cooldown_override", msg, db)
            detection["alert_sent"]   = sent
            detection["alert_reason"] = trigger_desc

            if sent:
                # Record score + components at alert time so the next cycle has a baseline
                db.patient_problems.update_one(
                    {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
                    {"$set": {
                        "last_alerted_at":              now,
                        "last_alerted_news2":           current_score,
                        "last_alerted_news2_components": components,
                    }},
                )
        else:
            detection["suppressed_reason"] = (
                f"total delta {total_delta} < {_NEWS2_TOTAL_DELTA} pts "
                f"AND max component delta {max_component_delta} ({worst_component}) "
                f"< {_NEWS2_COMPONENT_DELTA} pts"
            )

        _store_detection(detection, db)
        detections.append(detection)

    # Also record the NEWS2 score on any alert that DID fire this cycle so future
    # runs can compare.  We persist to patient_problems for the problems the
    # problem_tracker just alerted on.
    alerted_this_cycle = db.patient_problems.find({
        "CPMRN":           cpmrn,
        "encounter":       encounter,
        "last_alerted_at": {"$gte": now - timedelta(minutes=5)},
    })
    for prob in alerted_this_cycle:
        db.patient_problems.update_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": prob["problem_name"]},
            {"$set": {
                "last_alerted_news2":            current_score,
                "last_alerted_news2_components": components,
            }},
        )

    return detections


# ── Main entry point ───────────────────────────────────────────────────────────

def run_fn_detector(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    snapshot_at: datetime,
    db: Any,
) -> dict:
    """
    Run false-negative detectors for one patient (Detector 3 only).

    Called from the scheduler after track_problems completes.
    All detections (fired or suppressed) are stored in fn_detections
    for traceability.

    Returns a summary dict for the scheduler result entry.
    """
    if isinstance(snapshot_at, str):
        snapshot_at = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    now = snapshot_at if snapshot_at.tzinfo else snapshot_at.replace(tzinfo=timezone.utc)
    alerts_sent: list[str]      = []
    detections_total: list[str] = []

    # ── Detector 3: NEWS2 cooldown override ───────────────────────────────────
    try:
        d3 = _check_news2_override(cpmrn, encounter, structured_summary, now, db)
        for d in d3:
            detections_total.append(d["detector"])
            if d.get("alert_sent"):
                alerts_sent.append(f"news2:{d.get('problem_name','?')}")
    except Exception:
        logger.exception("fn_detector: detector 3 (NEWS2) failed for %s enc=%d", cpmrn, encounter)

    logger.info(
        "fn_detector: %d detection(s) checked, %d alert(s) sent for %s enc=%d — %s",
        len(detections_total), len(alerts_sent), cpmrn, encounter, alerts_sent or "none",
    )

    return {
        "fn_detector":        "ok",
        "detections_checked": len(detections_total),
        "alerts_sent":        alerts_sent,
    }
