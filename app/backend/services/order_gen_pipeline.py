"""
Order generation pipeline.

Takes a list of clinical recommendation strings and converts them into
structured EMR orders by running an LLM agent with tool access to the
orderables catalog and patient demographics.

Pipeline:
  Phase 0 — intent extraction: reduce each recommendation to a clean
             catalog search term + structured parameters, guided by
             order_rules.json (add rules there — no code change needed).
  Phase 1 — data gathering loop: search catalog, fetch demographics.
  Phase 2 — forced submit_orders call.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from ..config import MODEL, TRACES_DIR, _default_kb
from .llm_client import get_llm_client
from .token_tracker import log_call, calculate_cost
from .emr import get_patient_demographics, search_orderables, create_orderable
from . import vector_store as vs_mod
from .ingest_pipeline import write_gap_files
from .provenance_logger import write_trace

log = logging.getLogger("wiki.order_gen")

_RULES_PATH = Path(__file__).parent / "order_rules.json"


def _load_rules() -> tuple[list[dict], str]:
    """
    Load order_rules.json and return (rules_list, formatted_table_string).
    The table string is injected into the Phase 0 prompt.
    """
    if not _RULES_PATH.exists():
        return [], ""
    try:
        rules = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
        intents = rules.get("intents", [])
        lines = ["CATALOG LOOKUP RULES (use these when a recommendation matches):"]
        for r in intents:
            triggers = ", ".join(f'"{t}"' for t in r["match"])
            lines.append(
                f"  If text contains [{triggers}]"
                f" → catalog_query='{r['catalog_query']}'"
                f"  order_type='{r['order_type']}'"
                + (f"  note: {r['note']}" if r.get("note") else "")
            )
        return intents, "\n".join(lines)
    except Exception as exc:
        log.warning("Failed to load order_rules.json: %s", exc)
        return [], ""


_TOOL_EXTRACT_INTENTS = {
    "name": "extract_intents",
    "description": (
        "For each clinical recommendation, identify every orderable entity mentioned "
        "and emit one intent per entity. A single recommendation string may contain "
        "multiple drugs, procedures, or labs — emit a separate intent for each one. "
        "For example, 'Have etomidate 0.3 mg/kg IV and rocuronium 1 mg/kg IV ready' "
        "must produce two intents (index=same, catalog_query='etomidate' and 'rocuronium')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "0-based index of the source recommendation (multiple intents may share the same index)",
                        },
                        "catalog_query": {
                            "type": "string",
                            "description": (
                                "Short 1-3 word term to search the EMR catalog "
                                "(e.g. 'ventilator', 'noradrenaline', 'ABG', 'dextrose', 'etomidate'). "
                                "For procedures and imaging, ALWAYS include the body part or region "
                                "to avoid ambiguity (e.g. 'CT angiography head', 'CT head', "
                                "'X-ray chest', 'ultrasound abdomen', 'arterial line radial'). "
                                "Use the rules table when a match applies."
                            ),
                        },
                        "order_type": {
                            "type": "string",
                            "description": "med, lab, procedure, monitoring, vents, diet, blood, or comm",
                        },
                        "parameters": {
                            "type": "string",
                            "description": (
                                "Structured parameters for this specific orderable extracted from the text. "
                                "For ventilator: 'Mode: VC, TV: 6 ml/kg IBW, RR: 20/min, PEEP: 8 cmH2O, FiO2: 100%'. "
                                "For drugs: include dose and route, e.g. 'Dose: 0.3 mg/kg IV'. "
                                "Leave empty string if no structured parameters are present."
                            ),
                        },
                    },
                    "required": ["index", "catalog_query", "order_type", "parameters"],
                },
            }
        },
        "required": ["intents"],
    },
}

_SYSTEM = """\
You are an EMR order generation assistant. Your job is to convert clinical recommendation \
text into structured medication, lab, procedure, and monitoring orders.

For each recommendation:
1. Identify the order type: med, lab, procedure, or monitoring
2. Search the orderables catalog to find the best matching item
3. If the recommendation contains weight-based dosing (mg/kg, mcg/kg, units/kg), \
   fetch patient demographics to get the weight and calculate the actual dose
4. Map the recommendation to an order with specific quantity, unit, route, frequency
5. For ventilator orders: put mode/TV/RR/PEEP/FiO2 into the instructions field

ACTIVE ORDERS COMPARISON:
- If active_orders are provided, compare every recommendation against them before generating
- If the same drug/item is already active: set action="edit", existing_order_no=<the orderNo>, \
  from_dose=<current dose string>, to_dose=<new dose string>
- If you are asked to discontinue something active: set action="stop", existing_order_no=<orderNo>
- Otherwise: action="new"

Rules:
- Always search the catalog first. If search returns 0 results, call create_orderable to add it — \
  never submit an order without a catalog entry
- Use the pre-analyzed catalog_query and order_type from Phase 0 for each recommendation
- For monitoring (SpO2 target, HR target, UO target), the instruction text IS the order content; \
  search for a "vital monitoring" or "nursing" procedure orderable
- For labs, search by test name (e.g. "CBC", "creatinine", "lactate")
- Confidence: "high" if orderable found and dose is unambiguous; "medium" if approximate; \
  "low" if no matching orderable found, weight unavailable for weight-based dosing, \
  OR the matched orderable's body part / region does not match the intended one \
  (e.g. recommendation is "CT angiography head" but best catalog match is "CT angiography lower extremities" — set low)
- Frequency must be one of: 'once', 'continuous', 'daily', 'every N hours', 'every N minutes', \
  'every N days' where N is a single integer. Never use ranges like '1-2 hours'. \
  Never append qualifiers like 'initially' or 'then as clinically indicated' to frequency — \
  put that context in the instructions field instead.
- When done processing ALL recommendations, call submit_orders exactly once"""

_TOOL_SEARCH_ORDERABLES = {
    "name": "search_orderables",
    "description": "Search the EMR orderables catalog by name. Returns matching items with presets (default dosing, route, frequency).",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Drug/test/procedure name to search (e.g. 'vancomycin', 'CBC', 'vital monitoring')"},
            "order_type": {"type": "string", "description": "Filter: med, lab, procedure, diet, blood, vents, comm. Omit to search all."},
            "patient_type": {"type": "string", "description": "adult (default), pediatric, or neonatal"},
        },
        "required": ["query"],
    },
}

_TOOL_CREATE_ORDERABLE = {
    "name": "create_orderable",
    "description": (
        "Create a new orderable in the EMR catalog when search_orderables returns 0 results. "
        "Use only when you are clinically certain about the order and no catalog match exists. "
        "The new entry is immediately available for use in submit_orders."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Specific orderable name including body part for imaging/procedures (e.g. 'CT angiography head', 'X-ray chest AP', 'Ultrasound abdomen')",
            },
            "order_type": {
                "type": "string",
                "description": "med, lab, procedure, vents, diet, blood, or comm",
            },
            "form": {
                "type": "string",
                "description": "e.g. 'imaging', 'monitoring', 'tablet', 'injection', 'infusion', 'procedure'",
            },
            "frequency": {
                "type": "string",
                "description": "once, continuous, daily, every N hours",
            },
            "instructions": {
                "type": "string",
                "description": "Default clinical instructions for this orderable",
            },
            "patient_type": {
                "type": "string",
                "description": "adult (default), pediatric, or neonatal",
            },
        },
        "required": ["name", "order_type"],
    },
}

_TOOL_GET_DEMOGRAPHICS = {
    "name": "get_patient_demographics",
    "description": "Get patient demographics including weight (kg), age, gender, and allergies. Use when a recommendation has weight-based dosing (mg/kg, mcg/kg).",
    "input_schema": {
        "type": "object",
        "properties": {
            "cpmrn": {"type": "string", "description": "Patient CPMRN identifier"},
        },
        "required": ["cpmrn"],
    },
}

_TOOL_SUBMIT_ORDERS = {
    "name": "submit_orders",
    "description": "Submit the final structured orders. Call this exactly once after processing all recommendations.",
    "input_schema": {
        "type": "object",
        "properties": {
            "orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "recommendation": {"type": "string", "description": "Original recommendation text"},
                        "order_type": {"type": "string", "description": "med, lab, procedure, or monitoring"},
                        "orderable_name": {"type": "string", "description": "Matched orderable name from catalog, or null if not found"},
                        "order_details": {
                            "type": "object",
                            "description": "Structured order fields",
                            "properties": {
                                "name": {"type": "string"},
                                "quantity": {"type": "string"},
                                "unit": {"type": "string"},
                                "route": {"type": "string"},
                                "form": {"type": "string"},
                                "frequency": {
                                    "type": "string",
                                    "description": (
                                        "Use ONLY these formats: 'once', 'continuous', 'daily', "
                                        "'every N hours', 'every N minutes', 'every N days' "
                                        "where N is a single integer. "
                                        "Never use ranges (e.g. '1-2 hours'), never append qualifiers "
                                        "like 'initially' or 'then as clinically indicated' — "
                                        "put those in the instructions field instead."
                                    ),
                                },
                                "instructions": {"type": "string"},
                            },
                        },
                        "dose_calculation": {"type": "string", "description": "Dose calculation string if weight-based, e.g. '10 mg/kg × 65 kg = 650 mg'"},
                        "confidence": {"type": "string", "description": (
                            "high — orderable found, body part matches, dose unambiguous; "
                            "medium — orderable found but dose is approximate; "
                            "low — no orderable found, OR body part / region mismatch between "
                            "recommendation and matched orderable (e.g. 'CT angio head' matched to 'CT angio lower extremities'), "
                            "OR weight unavailable for weight-based dosing"
                        )},
                        "notes": {"type": "string", "description": "Any caveats or warnings"},
                        "action": {
                            "type": "string",
                            "description": (
                                "'new' (default) — start a fresh order; "
                                "'edit' — modify an existing active order (set existing_order_no); "
                                "'stop' — discontinue an existing active order (set existing_order_no)"
                            ),
                        },
                        "existing_order_no": {
                            "type": "string",
                            "description": "orderNo of the currently active order being edited or stopped",
                        },
                        "from_dose": {
                            "type": "string",
                            "description": "Current dose/rate of the existing order being edited, e.g. '20 mg/hr'",
                        },
                        "to_dose": {
                            "type": "string",
                            "description": "New dose/rate for the edited order, e.g. '10 mg/hr'",
                        },
                    },
                    "required": ["recommendation", "order_type", "confidence"],
                },
            }
        },
        "required": ["orders"],
    },
}

_GATHER_TOOLS = [_TOOL_SEARCH_ORDERABLES, _TOOL_CREATE_ORDERABLE, _TOOL_GET_DEMOGRAPHICS]


def _execute_tool(name: str, args: dict, cpmrn: str | None) -> str:
    """Execute a tool call and return the result as a JSON string."""
    try:
        if name == "search_orderables":
            result = search_orderables(
                query=args["query"],
                order_type=args.get("order_type"),
                patient_type=args.get("patient_type", "adult"),
                limit=5,
            )
        elif name == "create_orderable":
            result = create_orderable(
                name=args["name"],
                order_type=args["order_type"],
                form=args.get("form", ""),
                frequency=args.get("frequency", "once"),
                instructions=args.get("instructions", ""),
                patient_type=args.get("patient_type", "adult"),
            )
            log.info("  create_orderable  name=%r  type=%r  → created=%s",
                     args["name"], args["order_type"], result.get("created"))
        elif name == "get_patient_demographics":
            effective_cpmrn = args.get("cpmrn") or cpmrn
            if not effective_cpmrn:
                return json.dumps({"error": "No CPMRN provided"})
            result = get_patient_demographics(effective_cpmrn)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
        return json.dumps(result, default=str)
    except Exception as exc:
        log.warning("Tool %s failed: %s", name, exc)
        return json.dumps({"error": str(exc)})


def _append_turn(messages: list, response) -> list:
    """Append an assistant turn and return the tool_call blocks."""
    tool_calls = [b for b in response.content if b.type == "tool_use"]
    assistant_content = []
    for block in response.content:
        if block.type == "text":
            assistant_content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            tc: dict = {
                "type": "tool_use",
                "id": block.id or f"call_{block.name}",
                "name": block.name,
                "input": block.input,
            }
            if block.thought_signature:
                tc["thought_signature"] = block.thought_signature
            assistant_content.append(tc)
    if assistant_content:
        messages.append({"role": "assistant", "content": assistant_content})
    return tool_calls


def _phase0_extract_intents(
    recommendations: list[str],
    llm,
    rules_table: str,
) -> list[dict]:
    """
    Phase 0: single forced LLM call that reduces each recommendation to
    {index, catalog_query, order_type, parameters}.
    Returns a list aligned with recommendations (same length, same order).
    Falls back to empty dicts on failure so Phase 1 still runs.
    """
    rec_lines = "\n".join(f"{i}. {r}" for i, r in enumerate(recommendations))
    prompt = (
        f"{rules_table}\n\n"
        "RECOMMENDATIONS TO ANALYSE:\n"
        f"{rec_lines}\n\n"
        "For each recommendation above, call extract_intents with:\n"
        "- catalog_query: the short search term to use (apply rules table above when it matches)\n"
        "- order_type: the correct EMR order category\n"
        "- parameters: any structured clinical parameters embedded in the text "
        "(ventilator settings, drug dose/route/rate, targets) — these will be written "
        "verbatim into the order instructions field"
    )
    try:
        response = llm.create_message(
            messages=[{"role": "user", "content": prompt}],
            tools=[_TOOL_EXTRACT_INTENTS],
            system="You are an EMR order assistant. Extract catalog search intents from clinical recommendation text.",
            max_tokens=2000,
            force_tool=True,
        )
        block = next((b for b in response.content if b.type == "tool_use" and b.name == "extract_intents"), None)
        if not block:
            return [{} for _ in recommendations]

        raw = block.input.get("intents", [])
        # Group by index — one rec can yield multiple intents
        by_index: dict[int, list] = {}
        for item in raw:
            by_index.setdefault(item["index"], []).append(item)
        return [by_index.get(i, [{}]) for i in range(len(recommendations))]
    except Exception as exc:
        log.warning("Phase 0 intent extraction failed: %s", exc)
        return [[{}] for _ in recommendations]


def _resolve_dosing_weight(
    demographics: dict, kb
) -> tuple[float | None, str, str | None]:
    """
    Determine the best weight to use for drug dosing.

    Priority:
      1. Actual weight from EMR
      2. IBW calculated from height (formula from wiki: concepts/weight-estimation-standard-adult.md)
      3. Default weight from wiki "Default weight when not recorded" section
      4. Register knowledge gap and return None

    Returns (weight_kg, note, gap_page_registered)
    """
    weight_kg = demographics.get("weightKg")
    height_cm = demographics.get("heightCm")
    gender     = (demographics.get("gender") or "").lower()
    patient_type = demographics.get("patientType") or "adult"
    age_years  = demographics.get("age_years")

    # 1. Actual EMR weight
    if weight_kg:
        return float(weight_kg), f"Actual weight from EMR: {weight_kg} kg", None

    # 2. Height available → IBW (Devine/Ideal Body Weight formula, per wiki)
    if height_cm:
        is_male = gender in ("male", "m")
        ibw = 45.4 + 0.89 * (float(height_cm) - 152.4) + (4.5 if is_male else 0)
        ibw = max(round(ibw, 1), 30.0)  # floor at 30 kg
        gender_label = "M" if is_male else "F"
        note = (
            f"IBW from height {height_cm} cm ({gender_label}): {ibw} kg  "
            f"[45.4 + 0.89×({height_cm}−152.4){' + 4.5' if is_male else ''}]  "
            f"Source: wiki/concepts/weight-estimation-standard-adult.md"
        )
        log.info("IBW calculated: %.1f kg  height=%.0f cm  gender=%s", ibw, float(height_cm), gender_label)
        return ibw, note, None

    # 3. Neither weight nor height → search wiki for a default/fallback weight
    if patient_type == "pediatric" and age_years is not None:
        query    = f"pediatric weight estimation {age_years} year old child kg"
        gap_page = f"concepts/weight-estimation-pediatric-{age_years}yr.md"
        gap_sections = [
            f"Standard weight for {age_years}-year-old child (kg)",
            "Weight estimation formulas (APLS, Broselow)",
            "Age-based weight chart",
        ]
    else:
        query    = "default weight adult drug dosing IBW not recorded"
        gap_page = "concepts/weight-estimation-standard-adult.md"
        gap_sections = ["Default weight when not recorded"]

    hits = vs_mod.search(query, kb.wiki_dir, top_k=3)
    weight_keywords = ("weight", "ibw", "ideal body", "body weight")

    for hit in hits:
        if hit["score"] < 0.70 or not any(kw in hit["path"].lower() for kw in weight_keywords):
            continue
        page_path = kb.wiki_dir / hit["path"]
        if not page_path.exists():
            for sub in ("concepts", "entities", "sources"):
                cand = kb.wiki_dir / sub / Path(hit["path"]).name
                if cand.exists():
                    page_path = cand
                    break
        if not page_path.exists():
            continue

        content = page_path.read_text(encoding="utf-8")
        m = re.search(
            r'(?:default|standard)\s+(?:adult\s+)?weight[^\n]*?(\d{2,3})\s*kg',
            content, re.IGNORECASE
        )
        if m:
            w = float(m.group(1))
            note = f"Default weight from wiki ({hit['path']}): {w} kg"
            log.info("Wiki default weight: %.0f kg  page=%s  score=%.3f", w, hit["path"], hit["score"])
            return w, note, None

    # 4. Nothing found — register KG so the learn cycle can fill it
    log.info("No dosing weight available — registering KG: %s", gap_page)
    if patient_type == "pediatric" and age_years is not None:
        resolution_question = (
            f"What is the recommended standard body weight in kg for a {age_years}-year-old child "
            f"when actual weight is not recorded, for use in weight-based drug dosing in a paediatric ICU?"
        )
    else:
        resolution_question = (
            "What default adult body weight (kg) should be assumed for weight-based drug dosing "
            "(e.g. rocuronium 1 mg/kg, gentamicin 5 mg/kg) in an ICU setting when the patient's "
            "actual weight and height are not available in the EMR?"
        )
    try:
        written = write_gap_files(
            [{"page": gap_page, "missing_sections": gap_sections, "resolution_question": resolution_question}],
            kb,
            source_name=f"order_gen: weight and height unavailable for {patient_type} patient",
        )
        gap_registered = written[0] if written else gap_page
    except Exception as exc:
        log.warning("Failed to register weight KG: %s", exc)
        gap_registered = None

    return None, "No weight or height in EMR; wiki has no default weight yet (knowledge gap registered)", gap_registered


def run_order_generation(
    recommendations: list[str],
    cpmrn: str | None = None,
    patient_type: str = "adult",
    model: str | None = None,
    kb=None,
    active_orders: dict | None = None,
    parent_run_id: str | None = None,
) -> dict:
    """
    Three-phase pipeline:
      Phase 0 — intent extraction: map each recommendation to a clean catalog
               search term + structured parameters using order_rules.json.
      Phase 1 — data gathering loop: catalog search + patient demographics.
               Starts with pre-analyzed intents so catalog queries are accurate.
      Phase 2 — forced submit_orders call.
    """
    if kb is None:
        kb = _default_kb()

    resolved_model = model or MODEL
    run_id = str(uuid.uuid4())
    log.info("=== ORDER GEN START  run_id=%s  recs=%d  cpmrn=%s  model=%s  kb=%s ===",
             run_id, len(recommendations), cpmrn, resolved_model, kb.name)

    llm = get_llm_client(model=model)
    total_input = total_output = 0

    # ── Provenance trace accumulator ──────────────────────────────────────────
    trace: dict = {
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpmrn": cpmrn,
        "patient_type": patient_type,
        "model": resolved_model,
        "kb": kb.name,
        "recommendations": recommendations,
        "phase0": {},
        "phase1": {"iterations": []},
        "weight_resolution": None,
        "phase2": {},
        "final_orders": [],
        "tokens": {},
    }

    # ── Phase 0: intent extraction ────────────────────────────────────────────
    _, rules_table = _load_rules()
    log.info("Phase 0: extracting intents for %d recommendations", len(recommendations))
    intents = _phase0_extract_intents(recommendations, llm, rules_table)
    for idx, (rec, rec_intents) in enumerate(zip(recommendations, intents)):
        log.info("  [%d] input: %.80s…", idx, rec)
        for intent in rec_intents:
            log.info(
                "  [%d] intent: catalog_query=%r  order_type=%r  params=%r",
                idx,
                intent.get("catalog_query", "—"),
                intent.get("order_type", "—"),
                intent.get("parameters", "") or "—",
            )
    trace["phase0"]["intents"] = intents

    # ── Build Phase 1 user message with pre-analyzed intents ─────────────────
    rec_lines = []
    for idx, (rec, rec_intents) in enumerate(zip(recommendations, intents)):
        line = f"{idx + 1}. {rec}"
        for intent in rec_intents:
            if intent.get("catalog_query"):
                line += f"\n   → catalog_query: '{intent['catalog_query']}'  order_type: '{intent.get('order_type', '')}'"
            if intent.get("parameters"):
                line += f"\n   → parameters: {intent['parameters']}"
        rec_lines.append(line)
    rec_text = "\n\n".join(rec_lines)

    user_msg = (
        "Convert these clinical recommendations to EMR orders. "
        "Each recommendation includes a pre-analyzed catalog_query — use it exactly when searching.\n\n"
        + rec_text
    )
    if cpmrn:
        user_msg += (
            f"\n\nPatient CPMRN: {cpmrn} (patient_type: {patient_type}). "
            "Use get_patient_demographics if any recommendation needs weight-based dosing."
        )
    else:
        user_msg += "\n\nNo CPMRN provided — skip weight-based calculations, set confidence to low for any weight-based dosing."

    if active_orders:
        import json as _json
        meds = active_orders.get("medications") or []
        labs = active_orders.get("labs") or []
        procs = active_orders.get("procedures") or []
        vents = active_orders.get("vents") or []
        if any([meds, labs, procs, vents]):
            active_lines = []
            for m in meds:
                active_lines.append(
                    f"  MED  orderNo={m.get('orderNo','')}  {m.get('name','')}  "
                    f"{m.get('quantity','')} {m.get('unit','')} {m.get('route','')}  freq={m.get('frequency','')}"
                )
            for l in labs:
                active_lines.append(f"  LAB  orderNo={l.get('orderNo','')}  {l.get('investigation','')}")
            for p in procs:
                active_lines.append(f"  PROC orderNo={p.get('orderNo','')}  {p.get('name','')}")
            for v in vents:
                active_lines.append(f"  VENT orderNo={v.get('orderNo','')}  {v.get('name','')}  {v.get('instructions','')}")
            user_msg += (
                "\n\nCURRENTLY ACTIVE ORDERS ON THIS PATIENT:\n"
                + "\n".join(active_lines)
                + "\n\nFor each recommendation, check if it overlaps with an active order above. "
                "If so: set action='edit' + existing_order_no + from_dose + to_dose. "
                "If discontinuing something active: action='stop' + existing_order_no. "
                "New items: action='new'."
            )

    user_msg += "\n\nSearch the orderables catalog for each recommendation now."

    messages: list[dict] = [{"role": "user", "content": user_msg}]

    # ── Phase 1: gather data (max 8 iterations) ───────────────────────────────
    for iteration in range(8):
        response = llm.create_message(
            messages=messages,
            tools=_GATHER_TOOLS,
            system=_SYSTEM,
            max_tokens=4000,
            force_tool=False,
        )
        total_input  += response.usage.input_tokens
        total_output += response.usage.output_tokens

        tool_calls = _append_turn(messages, response)
        log.info("Phase1 iter %d: stop=%s  tools=%d  in=%d  out=%d",
                 iteration, response.stop_reason, len(tool_calls),
                 response.usage.input_tokens, response.usage.output_tokens)

        iter_trace: dict = {
            "iteration": iteration,
            "stop_reason": response.stop_reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "tool_calls": [],
        }

        if not tool_calls or response.stop_reason == "end_turn":
            trace["phase1"]["iterations"].append(iter_trace)
            break

        for block in tool_calls:
            if block.name == "search_orderables":
                log.info("  search_orderables  query=%r  order_type=%r",
                         block.input.get("query"), block.input.get("order_type"))
            elif block.name == "create_orderable":
                log.info("  create_orderable  name=%r  type=%r",
                         block.input.get("name"), block.input.get("order_type"))
            elif block.name == "get_patient_demographics":
                log.info("  get_patient_demographics  cpmrn=%r", block.input.get("cpmrn"))
            result_text = _execute_tool(block.name, block.input, cpmrn)
            tc_trace: dict = {"name": block.name, "args": block.input}
            try:
                result_preview = json.loads(result_text)
                if block.name == "search_orderables":
                    names = [o.get("name", "?") for o in result_preview.get("orderables", [])]
                    log.info("  → %d matches: %s", result_preview.get("count", 0), names)
                    tc_trace["result_count"] = result_preview.get("count", 0)
                    tc_trace["result_summary"] = names
                elif block.name == "get_patient_demographics":
                    log.info("  → weight=%s kg  height=%s cm  gender=%s",
                             result_preview.get("weightKg", "—"),
                             result_preview.get("heightCm", "—"),
                             result_preview.get("gender", "—"))
                    tc_trace["result_summary"] = {
                        "weightKg": result_preview.get("weightKg"),
                        "heightCm": result_preview.get("heightCm"),
                        "gender": result_preview.get("gender"),
                        "age_years": result_preview.get("age_years"),
                    }
                elif block.name == "create_orderable":
                    tc_trace["result_summary"] = {
                        "created": result_preview.get("created"),
                        "id": result_preview.get("id"),
                    }
                else:
                    tc_trace["result_summary"] = result_preview
            except Exception:
                log.info("  → %d chars", len(result_text))
                tc_trace["result_summary"] = result_text[:200]
            iter_trace["tool_calls"].append(tc_trace)
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": block.id or f"call_{block.name}",
                "name": block.name,
                "content": result_text,
            }]})
        trace["phase1"]["iterations"].append(iter_trace)

    # ── Weight resolution ─────────────────────────────────────────────────────
    weight_note = None
    weight_gap  = None
    for msg in messages:
        if msg["role"] != "user":
            continue
        content = msg["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "tool_result" or block.get("name") != "get_patient_demographics":
                continue
            try:
                demo = json.loads(block["content"])
            except Exception:
                continue
            if demo.get("error"):
                break
            dosing_weight, weight_note, weight_gap = _resolve_dosing_weight(demo, kb)
            if dosing_weight is not None:
                is_actual = weight_note.startswith("Actual weight from EMR")
                weight_source = "actual_emr" if is_actual else (
                    "ibw_from_height" if "IBW from height" in weight_note else "wiki_default"
                )
                conf_instruction = (
                    "This is the actual recorded weight — confidence for weight-based orders may be high."
                    if is_actual else
                    "This weight is ESTIMATED (not recorded in EMR) — set confidence to 'medium' for all weight-based orders."
                )
                trace["weight_resolution"] = {
                    "source": weight_source,
                    "weight_kg": dosing_weight,
                    "note": weight_note,
                    "gap_registered": weight_gap,
                }
                messages.append({"role": "user", "content":
                    f"Use {dosing_weight:.1f} kg as the dosing weight for this patient. "
                    f"({weight_note}) {conf_instruction}"
                })
            else:
                trace["weight_resolution"] = {
                    "source": "none",
                    "weight_kg": None,
                    "note": weight_note,
                    "gap_registered": weight_gap,
                }
                messages.append({"role": "user", "content":
                    f"No reliable dosing weight available. {weight_note} "
                    f"For weight-based orders, set confidence to low."
                })
            break

    # ── Phase 2: force submit_orders ──────────────────────────────────────────
    messages.append({"role": "user", "content":
        "You now have all the data needed. Call submit_orders with the structured orders for every recommendation. "
        "Remember to include the extracted parameters (mode, TV, RR, PEEP, FiO2, dose, etc.) in each order's instructions field."
    })

    response = llm.create_message(
        messages=messages,
        tools=[_TOOL_SUBMIT_ORDERS],
        system=_SYSTEM,
        max_tokens=4000,
        force_tool=True,
    )
    total_input  += response.usage.input_tokens
    total_output += response.usage.output_tokens

    _append_turn(messages, response)
    submit_block = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_orders"), None)
    orders = submit_block.input.get("orders", []) if submit_block else []
    log.info("Phase2: submit_orders → %d orders  in=%d  out=%d",
             len(orders), response.usage.input_tokens, response.usage.output_tokens)
    for o in orders:
        log.info(
            "  order: [%s] orderable=%r  confidence=%s  instructions=%r",
            o.get("order_type", "?"),
            o.get("orderable_name", "—"),
            o.get("confidence", "?"),
            (o.get("order_details") or {}).get("instructions", "") or "—",
        )

    cost = log_call(
        operation="order_gen",
        source_name=(recommendations[0][:60] if recommendations else ""),
        input_tokens=total_input,
        output_tokens=total_output,
        model=resolved_model,
    )
    log.info("=== ORDER GEN END  run_id=%s  orders=%d  in=%d  out=%d  cost=$%.4f ===",
             run_id, len(orders), total_input, total_output, cost)

    # ── Write provenance trace ────────────────────────────────────────────────
    trace["phase2"] = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    trace["final_orders"] = orders
    trace["tokens"] = {
        "input": total_input,
        "output": total_output,
        "cost_usd": cost,
    }
    write_trace(trace, TRACES_DIR)

    return {
        "orders": orders,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
        "model": resolved_model,
        "weight_gap_registered": weight_gap,
        "run_id": run_id,
    }
