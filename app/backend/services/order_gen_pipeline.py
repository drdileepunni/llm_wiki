"""
Order generation pipeline.

Takes a list of clinical recommendation strings and converts them into
structured EMR orders by running an LLM agent with tool access to the
orderables catalog and patient demographics.
"""

import json
import logging
import re
from pathlib import Path
from ..config import MODEL, _default_kb
from .llm_client import get_llm_client
from .token_tracker import log_call, calculate_cost
from .emr import get_patient_demographics, search_orderables
from . import vector_store as vs_mod
from .ingest_pipeline import write_gap_files

log = logging.getLogger("wiki.order_gen")

_SYSTEM = """\
You are an EMR order generation assistant. Your job is to convert clinical recommendation \
text into structured medication, lab, procedure, and monitoring orders.

For each recommendation:
1. Identify the order type: med, lab, procedure, or monitoring
2. Search the orderables catalog to find the best matching item
3. If the recommendation contains weight-based dosing (mg/kg, mcg/kg, units/kg), \
   fetch patient demographics to get the weight and calculate the actual dose
4. Map the recommendation to an order with specific quantity, unit, route, frequency

Rules:
- Always search the catalog — never invent an orderable that wasn't found
- For monitoring (SpO2 target, HR target, UO target), the instruction text IS the order content; \
  search for a "vital monitoring" or "nursing" procedure orderable
- For labs, search by test name (e.g. "CBC", "creatinine", "lactate")
- Confidence: "high" if orderable found and dose is unambiguous; "medium" if approximate; \
  "low" if no matching orderable found or weight unavailable for weight-based dosing
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
                                "frequency": {"type": "string"},
                                "instructions": {"type": "string"},
                            },
                        },
                        "dose_calculation": {"type": "string", "description": "Dose calculation string if weight-based, e.g. '10 mg/kg × 65 kg = 650 mg'"},
                        "confidence": {"type": "string", "description": "high, medium, or low"},
                        "notes": {"type": "string", "description": "Any caveats or warnings"},
                    },
                    "required": ["recommendation", "order_type", "confidence"],
                },
            }
        },
        "required": ["orders"],
    },
}

_GATHER_TOOLS = [_TOOL_SEARCH_ORDERABLES, _TOOL_GET_DEMOGRAPHICS]


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
            assistant_content.append({
                "type": "tool_use",
                "id": block.id or f"call_{block.name}",
                "name": block.name,
                "input": block.input,
            })
    if assistant_content:
        messages.append({"role": "assistant", "content": assistant_content})
    return tool_calls


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
        # Look for an explicit "Default weight" or "Standard adult weight" with a number
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
    try:
        written = write_gap_files(
            [{"page": gap_page, "missing_sections": gap_sections}],
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
) -> dict:
    """
    Two-phase pipeline:
      Phase 1 — data gathering loop (patient demographics + orderable searches)
               If weight is null, looks up standard weight from the wiki.
               If wiki has no data, registers a knowledge gap for the learn cycle.
      Phase 2 — forced submit_orders call with all context in the conversation
    """
    if kb is None:
        kb = _default_kb()

    resolved_model = model or MODEL
    log.info("=== ORDER GEN START  recs=%d  cpmrn=%s  model=%s  kb=%s ===",
             len(recommendations), cpmrn, resolved_model, kb.name)

    llm = get_llm_client(model=model)
    total_input = total_output = 0

    rec_text = "\n".join(f"- {r}" for r in recommendations)
    user_msg = f"I need to convert these clinical recommendations to EMR orders:\n\n{rec_text}"
    if cpmrn:
        user_msg += f"\n\nPatient CPMRN: {cpmrn} (patient_type: {patient_type}). Use get_patient_demographics if any recommendation needs weight-based dosing."
    else:
        user_msg += "\n\nNo CPMRN provided — skip weight-based calculations, set confidence to low for any weight-based dosing."
    user_msg += "\n\nPlease search the orderables catalog for each recommendation now."

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

        if not tool_calls or response.stop_reason == "end_turn":
            break

        # Execute each tool call one at a time to avoid Gemini multi-call issues
        for block in tool_calls:
            result_text = _execute_tool(block.name, block.input, cpmrn)
            log.info("  Tool %s → %d chars", block.name, len(result_text))
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": block.id or f"call_{block.name}",
                "name": block.name,
                "content": result_text,
            }]})

    # ── Weight resolution: determine dosing weight from EMR / IBW / wiki ──────
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
                conf_instruction = (
                    "This is the actual recorded weight — confidence for weight-based orders may be high."
                    if is_actual else
                    "This weight is ESTIMATED (not recorded in EMR) — set confidence to 'medium' for all weight-based orders."
                )
                messages.append({"role": "user", "content":
                    f"Use {dosing_weight:.1f} kg as the dosing weight for this patient. "
                    f"({weight_note}) {conf_instruction}"
                })
            else:
                messages.append({"role": "user", "content":
                    f"No reliable dosing weight available. {weight_note} "
                    f"For weight-based orders, set confidence to low."
                })
            break

    # ── Phase 2: force submit_orders ──────────────────────────────────────────
    messages.append({"role": "user", "content":
        "You now have all the data needed. Call submit_orders with the structured orders for every recommendation."
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

    cost = log_call(
        operation="order_gen",
        source_name=(recommendations[0][:60] if recommendations else ""),
        input_tokens=total_input,
        output_tokens=total_output,
        model=resolved_model,
    )
    log.info("=== ORDER GEN END  orders=%d  in=%d  out=%d  cost=$%.4f ===",
             len(orders), total_input, total_output, cost)

    return {
        "orders": orders,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
        "model": resolved_model,
        "weight_gap_registered": weight_gap,
    }
