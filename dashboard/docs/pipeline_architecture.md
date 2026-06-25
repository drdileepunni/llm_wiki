# CDS Pipeline — Per-Patient Decision Flow

This page documents the adaptive scheduling logic introduced to reduce LLM cost and guarantee
per-problem clinical follow-up. The pipeline runs **every hour** (Cloud Scheduler), loops all
admitted patients sequentially, and applies a cascade of gates before reaching any paid LLM call.

---

## Gate sequence

```mermaid
flowchart TD
    A([Hourly Scheduler Tick]) --> B[Delta Extraction\nfree, no LLM]
    B --> C[Overdue next_check probe\nscan patient_problems]
    C --> D{GATE 1\ncadence: now < next_run_at?}
    D -- YES\nno force --> E[fn_detector only\nzero LLM]
    D -- NO\nor force_expensive\nor force_glucose_check --> F{GATE 2\nany new data?}
    F -- NO\nno force --> G[fn_detector only\nzero LLM]
    F -- NO\nforce_glucose_check only --> GF[Glucose forced recheck\ncheap path — no LLM\nnext_check.due_after = next_grbs_after]
    F -- NO\nforce_expensive --> H[Forced shortcut\nskip summary & pass-1]
    F -- YES --> I{GATE 2.5 NEW\nvitals-only delta?\nall NEWS2=0 O2 stripped?}
    H --> R
    I -- YES --> J[Zero LLM cost\nfn_detector only]
    I -- NO --> K[CHEAP RUN\nSummary update flash-lite\nPass-1 screener]
    K --> L{GATE 3\npass-1 needs_full\nOR force_expensive?}
    L -- NO --> M[Pass-2 skipped\nfn_detector only]
    L -- YES\npass-1 flagged --> N[EXPENSIVE RUN\nstatus_classifier\nproblem_tracker]
    L -- YES\nforced only --> R[problem_tracker\nfocus=overdue items]
    N --> O{Assessment result\nstill abnormal & unaddressed?}
    R --> O
    O -- Normalized --> P[Clear next_check\nReset alert_attempts=0]
    O -- Still abnormal --> Q[Fire alert if cooldown elapsed\nalert_attempts++\ncompute backoff interval]
    Q --> S[Write next_check.due_after\nnow + interval_h]
    P --> T
    S --> T[fn_detector\nalways, zero LLM]
    T --> U([End — next patient])

    style H fill:#fef9e7,stroke:#e67e22
    style R fill:#fef9e7,stroke:#e67e22
    style K fill:#eafaf1,stroke:#27ae60
    style N fill:#fdecea,stroke:#c0392b
    style Q fill:#f0f0ff,stroke:#6c5ce7
    style S fill:#f0f0ff,stroke:#6c5ce7
```

---

## The three new gates

### GATE 2.5 — Vital-within-normal gate

Added in `fn_detector.py` (`is_vital_row_normal`, `all_new_vitals_normal`).

Fires when:
- The delta is **vitals-only** (no new labs, notes, or report findings)
- Every new vital row scores **0 on NEWS2** — with the FiO2/supplemental-O2 component stripped so stable ventilated patients can also be skipped
- No Netra camera `abnormal_list` flags
- GCS ≥ 15

Result: zero LLM spend for that cycle. The fn_detector safety net still runs.

### Overdue next_check routing

When `overdue_next_checks()` finds any lab-type `next_check` whose `due_after < now`, the overdue
items are split into two buckets:

| Bucket | Variable | Condition | Effect |
|---|---|---|---|
| **Glucose** | `_overdue_glucose` | key/label matches glucose keywords (glucose, rbs, cbg, grbs, blood sugar …) | Sets `force_glucose_check=True` |
| **Generic** | `_overdue_generic` | everything else (creatinine, Hb, lactate, …) | Sets `force_expensive=True` |

**`force_expensive` (generic)** — bypasses cadence gate, delta gate, and pass-1; runs the full
`track_problems` LLM call with a `FORCED FOLLOW-UP` block naming the overdue item(s).

**`force_glucose_check` (glucose-only)** — bypasses cadence gate and delta gate but skips all LLM
entirely. Routes to the same cheap path as Gate 2.6: `insulin_advice.gather_inputs + compute →
alert card → update next_check.due_after`. No pass-1, no problem_tracker.

If both buckets are non-empty simultaneously (e.g. glucose + creatinine both overdue), only
`force_expensive` is set — the full expensive run handles glucose via the normal
`attach_insulin_order` enrichment path.

### Exponential backoff

When a follow-up finds the value **still abnormal and unaddressed** (proxy for caregiver ignoring the alert), `alert_attempts` increments and the next interval grows:

```
interval_h = min(base_interval × 2^attempts, 24h)
```

Growth stops at 4 attempts **or** when the 24h cap is hit, whichever comes first.

| Type | Base | attempt 0 | 1 | 2 | 3 | 4 (stop) |
|---|---|---|---|---|---|---|
| Vital | 1h | 1h | 2h | 4h | 8h | 16h |
| Fast lab (lactate, Na, K, ABG, Hb) | 6h | 6h | 12h | 24h | — | — |
| Other labs | 24h | 24h | — | — | — | — |
| **Glucose (hyperglycemia)** | **insulin timing** | **1h / 4h / 6h** | — | — | — | — |

**Reset conditions:** problem normalises, or a fresh plan note appears (`being_addressed` flips to true) → `alert_attempts = 0`.

**Glucose exception:** `next_check.due_after` for hyperglycemia problems is **not** computed from the backoff table. After `attach_insulin_order` runs (in both the cheap glucose path and the full tracker path), the stored `due_after` is overwritten with `snapshot_at + next_grbs_after`, where `next_grbs_after` comes from the insulin recommendation engine:

| Route | Diet | `next_grbs_after` |
|---|---|---|
| IV insulin | any | 1h (hourly if glucose uncontrolled) or 2h (if all readings 140–180) |
| SC insulin | NPO / fasting | 4h |
| SC insulin | eating | 6h |

---

## IO-change delta triggering

### Why a raw IO entry can't be a trigger by itself

The Radar IO data structure is `chart.io.days[].hours[].minutes[]` — entries are keyed by day/hour/minute index, not by a standalone timestamp. This makes it impossible to apply the same cutoff comparison used for vitals and labs (`timestamp > last_llm_run_at`). It also means a single isolated IO reading (e.g. 5 cc urine in one hour) would be a clinically meaningless trigger — urine output is only interpretable in the context of the past several hours.

### How IO triggering works

`extract_delta` computes `io_last_24h` — a 24-hour aggregate of all intake, output, and balance entries — and compares it to `last_io_aggregate` stored in `snapshot_schedule` after the previous run:

```python
io_changed = bool(prev_io_aggregate) and (io_now != prev_io_aggregate)
```

If the aggregates differ, `io_changed=True` is included in the delta and Gate 2 treats it as new data, allowing the pipeline to proceed. What the LLM then receives is the **full 24h aggregate** (`I/O: intake=X ml  output=Y ml  balance=Z ml`), not the raw individual entry. This means the model always assesses IO in the context of cumulative fluid balance rather than a single measurement.

### Seeding and bootstrap

On the first run for a newly enrolled patient, `last_io_aggregate` is `None` in the schedule doc. `io_changed` defaults to `False` in this case — IO alone will not trigger a run until a baseline aggregate has been stored. Once any real LLM run completes (triggered by a vital, lab, note, or overdue next_check), `last_io_aggregate` is written to the schedule doc and future IO changes will be detected normally.

### What triggers `last_io_aggregate` to update

It is stored alongside `last_llm_run_at` at every point the pipeline completes a real LLM analysis: pass1 completion, glucose-only gate, and forced-by-overdue-next_check runs. It is **not** updated on delta-gate skips, vital-normal skips, or empty-chart skips — those runs did not process the IO data, so the stored baseline should not advance.

---

## Re-alert suppression (cooldown)

The backoff interval doubles as the re-alert cooldown window — there is **no separate 8-hour cooldown**. When an alert fires, `_upsert_problem` stores `current_interval_h` on the problem doc and sets `next_check.due_after = now + interval_h`. `_should_suppress_alert` reads `current_interval_h` from the stored doc to decide whether to suppress the next alert attempt for the same problem.

### Concrete example — SpO2 / hypoxemia

| Alert # | `alert_attempts` before fire | Cooldown stored | Earliest next alert |
|---|---|---|---|
| 1st | 0 | 1h | 1h after 1st alert |
| 2nd | 1 | 2h | 2h after 2nd alert |
| 3rd | 2 | 4h | 4h after 3rd alert |
| 4th | 3 | 8h | 8h after 4th alert |
| 5th+ | 4 | 16h (cap) | 16h after each subsequent alert |

This means a vital problem that keeps alerting without a plan will see its re-alert window grow: 1h → 2h → 4h → 8h → 16h. The old hardcoded 8h value (`_ALERT_COOLDOWN_H = 8`) is retained **only as a legacy fallback** for problem docs written before the backoff system existed (docs that have no `current_interval_h` field). Any problem doc written by the current code will have `current_interval_h` set and will never reach the 8h fallback.

### Suppression check
`_should_suppress_alert` in `problem_tracker.py`:
```python
cooldown_h = doc.get("current_interval_h") or _ALERT_COOLDOWN_H   # 8h fallback for legacy docs only
return (datetime.now(timezone.utc) - last) < timedelta(hours=cooldown_h)
```

The suppression fires **before** the model runs — a suppressed problem is never sent to the tracker LLM for that cycle.

---

## Key files

| File | Role |
|---|---|
| `app/backend/scheduler.py` | Gate sequence, `_run_live_pipeline()` |
| `tools/radar_sync/fn_detector.py` | `is_vital_row_normal`, `all_new_vitals_normal`, NEWS2 scoring |
| `tools/radar_sync/problem_tracker.py` | `overdue_next_checks`, `track_problems(focus=...)`, backoff in `_upsert_problem` |
| `tools/radar_sync/pass1_screener.py` | Cheap triage; `needs_full_analysis`, `next_run_hours` |
| `tools/radar_sync/status_classifier.py` | Reasoning-model status labels (skipped on forced-only path) |
