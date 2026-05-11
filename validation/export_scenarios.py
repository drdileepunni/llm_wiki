#!/usr/bin/env python3
"""
Export N random completed viva sessions to data/scenarios.json.

Usage:
  python export_scenarios.py --kb agent_school --n 3
  python export_scenarios.py --kb agent_school --batch vbatch_c3f179e4 --n 3
  python export_scenarios.py --kb agent_school --n 3 --seed 42
"""
import argparse
import json
import random
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
KBS_ROOT = REPO_ROOT / "kbs"

ORDER_TYPE_LABELS = {
    "med":       "Medications",
    "lab":       "Investigations",
    "procedure": "Procedures",
    "monitoring":"Monitoring",
    "vents":     "Ventilation",
    "comm":      "Consultations",
    "diet":      "Nutrition & Diet",
    "blood":     "Blood Products",
}

ORDER_TYPE_ORDER = ["med", "lab", "procedure", "monitoring", "vents", "comm", "diet", "blood"]


def format_order(order, session_id, turn_num, idx):
    od = order.get("order_details", {})
    otype = order.get("order_type", "other")

    parts = [order.get("orderable_name", "Unknown")]
    if otype == "med":
        qty  = od.get("quantity", "")
        unit = od.get("unit", "")
        route= od.get("route", "")
        freq = od.get("frequency", "")
        if qty and unit:
            parts.append(f"{qty} {unit}")
        if route and route not in ("N/A", ""):
            parts.append(route)
        if freq:
            parts.append(freq)

    return {
        "id":               f"{session_id}_t{turn_num}_{idx}",
        "order_type":       otype,
        "type_label":       ORDER_TYPE_LABELS.get(otype, otype.title()),
        "display":          " · ".join(parts),
        "instructions":     od.get("instructions", ""),
        "dose_calculation": order.get("dose_calculation", ""),
        "confidence":       order.get("confidence", ""),
        "action":           order.get("action", "new"),
        "recommendation":   order.get("recommendation", ""),
    }


def extract_scenario(session, batch_meta=None):
    sid = session["session_id"]

    diagnosis = complication = ""
    if batch_meta:
        for s in batch_meta.get("sessions", []):
            if s.get("session_id") == sid:
                diagnosis   = s.get("diagnosis", "")
                complication = s.get("complication", "")
                break

    turns_out = []
    all_categories = set()

    for turn in session.get("turns", []):
        tnum   = turn["turn_num"]
        orders = turn.get("orders", [])

        # Index orders globally per turn, then group by type for display
        indexed = [format_order(o, sid, tnum, i) for i, o in enumerate(orders)]
        for o in indexed:
            all_categories.add(o["order_type"])

        # Sort by canonical category order
        cat_rank = {k: i for i, k in enumerate(ORDER_TYPE_ORDER)}
        indexed.sort(key=lambda o: (cat_rank.get(o["order_type"], 99), o["display"]))

        scenario = turn.get("scenario", {})
        turns_out.append({
            "turn_num":          tnum,
            "phase":             scenario.get("phase", ""),
            "difficulty":        scenario.get("difficulty", ""),
            "clinical_context":  scenario.get("clinical_context", ""),
            "question":          scenario.get("question", ""),
            "simulation_summary":turn.get("simulation_summary", ""),
            "orders":            indexed,
        })

    # Ordered list of categories actually present in this scenario
    cats_present = [c for c in ORDER_TYPE_ORDER if c in all_categories]

    return {
        "scenario_id":   sid,
        "topic":         session.get("topic", ""),
        "diagnosis":     diagnosis,
        "complication":  complication,
        "total_turns":   len(turns_out),
        "turns":         turns_out,
        "categories":    cats_present,
        "exported_at":   datetime.utcnow().isoformat(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb",    default="agent_school")
    ap.add_argument("--n",     type=int, default=3)
    ap.add_argument("--batch", default=None, help="Limit to sessions from this batch run ID")
    ap.add_argument("--seed",  type=int, default=None)
    args = ap.parse_args()

    viva_dir      = KBS_ROOT / args.kb / "assessments" / "viva"
    batch_runs_dir= KBS_ROOT / args.kb / "viva_batch_runs"

    # Collect candidate session files
    if args.batch:
        files = list(viva_dir.glob(f"batch_{args.batch}_*.json"))
    else:
        files = list(viva_dir.glob("batch_*.json")) + list(viva_dir.glob("viva_*.json"))

    complete = []
    for p in files:
        try:
            d = json.loads(p.read_text())
            if d.get("status") == "complete" and d.get("turns"):
                complete.append((p, d))
        except Exception as e:
            print(f"  skip {p.name}: {e}")

    if not complete:
        print(f"No complete sessions in {viva_dir}")
        return

    rng = random.Random(args.seed)
    chosen = rng.sample(complete, min(args.n, len(complete)))

    # Load all batch metadata for diagnosis/complication lookup
    batch_metas = {}
    if batch_runs_dir.exists():
        for bf in batch_runs_dir.glob("*.json"):
            try:
                bm = json.loads(bf.read_text())
                batch_metas[bm["run_id"]] = bm
            except Exception:
                pass

    scenarios = []
    for path, session in chosen:
        sid = session.get("session_id", "")
        # Find parent batch meta
        parent_meta = None
        for bm in batch_metas.values():
            if any(s.get("session_id") == sid for s in bm.get("sessions", [])):
                parent_meta = bm
                break

        s = extract_scenario(session, parent_meta)
        scenarios.append(s)
        label = s["diagnosis"] or s["topic"][:60]
        n_orders = sum(len(t["orders"]) for t in s["turns"])
        print(f"  ✓ {sid}  ({s['total_turns']} turns, {n_orders} orders)  {label}")

    out = {
        "exported_at":  datetime.utcnow().isoformat(),
        "kb":           args.kb,
        "n_scenarios":  len(scenarios),
        "scenarios":    scenarios,
    }
    out_path = Path(__file__).parent / "data" / "scenarios.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n→ {out_path}  ({len(scenarios)} scenarios)")


if __name__ == "__main__":
    main()
