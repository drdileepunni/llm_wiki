"""
Order safety annotator.

Sits between the CDS output and order generation. For each clinical recommendation
string, an LLM with patient-data tools proactively fetches relevant context
(organ function, diagnoses, home medications) and appends structured annotations.

The annotated strings are then passed to run_order_generation, which sees the
enriched context and can produce accurate doses and flag safety concerns without
needing to re-query MongoDB itself.

Example:
  in:  "Vancomycin 15 mg/kg IV every 8–12 hours"
  out: "Vancomycin 15 mg/kg IV every 8–12 hours [Renal: Cr 2.1 mg/dL, CrCl ~28 ml/min — extend interval]"

  in:  "Paracetamol 1000 mg PO every 6 hours"
  out: "Paracetamol 1000 mg PO every 6 hours [CAUTION: known cirrhosis — hepatotoxic drug, reduce dose]"
"""

from __future__ import annotations

import json
import logging

from .llm_client import get_llm_client, LLMToolUseBlock
from .emr import get_patient_demographics, get_latest_labs

log = logging.getLogger("wiki.order_safety")

_SYSTEM = """\
You are a clinical pharmacist performing a pre-order safety review.

You will receive a list of clinical recommendations about to be entered as EMR orders.
Your job is to fetch any patient data needed to verify dosing and safety, then return
an annotated version of each recommendation.

Use the available tools to retrieve data. Apply your clinical knowledge to decide when
a fetch is needed — do not call tools blindly for every order. Focus on:
- Organ function that affects drug clearance or toxicity (renal, hepatic)
- Diagnoses or history that create contraindications or require dose adjustment
- Home medications that affect starting doses for tolerance-dependent drugs

Annotate each recommendation by appending a bracketed note with the relevant context
and any dose adjustment or caution. If a recommendation needs no adjustment, return it
unchanged. Do not change the clinical decision itself — only add context.
"""

_TOOL_GET_DEMOGRAPHICS = {
    "name": "get_patient_demographics",
    "description": (
        "Fetch patient demographics: weight, age, gender, chronic diagnoses (PMHX), "
        "home_medications, and allergies."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

_TOOL_GET_LAB = {
    "name": "get_latest_lab",
    "description": (
        "Fetch the most recent result for a specific lab test. "
        "Examples: 'Creatinine', 'ALT', 'AST', 'Bilirubin', 'Potassium', 'Vancomycin level'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "test_name": {"type": "string", "description": "Lab test name"},
        },
        "required": ["test_name"],
    },
}

_TOOL_SUBMIT = {
    "name": "submit_annotated",
    "description": "Submit the final annotated recommendations. Call exactly once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recommendations": {
                "type": "array",
                "description": "Annotated recommendation strings, in the same order as input.",
                "items": {"type": "string"},
            }
        },
        "required": ["recommendations"],
    },
}

_GATHER_TOOLS = [_TOOL_GET_DEMOGRAPHICS, _TOOL_GET_LAB]


def _run_tool(name: str, inputs: dict, cpmrn: str) -> str:
    try:
        if name == "get_patient_demographics":
            result = get_patient_demographics(cpmrn)
        elif name == "get_latest_lab":
            test_name = inputs.get("test_name", "")
            raw = get_latest_labs(cpmrn, tests=[test_name])
            labs = raw.get("labs", {})
            result = labs if labs else {"message": f"No result found for {test_name}"}
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc)}
    return json.dumps(result, default=str)


def annotate_recommendations(
    recommendations: list[str],
    cpmrn: str,
    model: str | None = None,
) -> list[str]:
    """
    Run the safety annotator over a list of recommendation strings.
    Returns an annotated list of the same length.
    Falls back to the original list on any failure.
    """
    if not recommendations or not cpmrn:
        return recommendations

    llm = get_llm_client(model=model)

    rec_text = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(recommendations))
    user_msg = (
        f"Review these {len(recommendations)} recommendations before order entry. "
        "Fetch any patient data you need, then call submit_annotated with the "
        "annotated list (same count, same order).\n\n"
        f"RECOMMENDATIONS:\n{rec_text}"
    )

    messages: list[dict] = [{"role": "user", "content": user_msg}]

    # ── Tool loop (max 6 rounds) ──────────────────────────────────────────────
    for round_num in range(6):
        response = llm.create_message(
            messages=messages,
            tools=_GATHER_TOOLS + [_TOOL_SUBMIT],
            system=_SYSTEM,
            max_tokens=2000,
            force_tool=False,
        )

        tool_calls = [b for b in response.content if isinstance(b, LLMToolUseBlock)]

        # Build assistant turn
        assistant_content = []
        for b in response.content:
            if isinstance(b, LLMToolUseBlock):
                block: dict = {
                    "type": "tool_use",
                    "id": b.id or f"tool_{round_num}_{b.name}",
                    "name": b.name,
                    "input": b.input,
                }
                if b.thought_signature is not None:
                    block["thought_signature"] = b.thought_signature
                assistant_content.append(block)
            elif hasattr(b, "text") and b.text:
                assistant_content.append({"type": "text", "text": b.text})
        if assistant_content:
            messages.append({"role": "assistant", "content": assistant_content})

        if not tool_calls:
            break

        tool_results = []
        for tc in tool_calls:
            tool_id = tc.id or f"tool_{round_num}_{tc.name}"

            if tc.name == "submit_annotated":
                annotated = tc.input.get("recommendations", [])
                log.info(
                    "Order safety annotator: %d recommendations annotated",
                    len(annotated),
                )
                for orig, ann in zip(recommendations, annotated):
                    if ann != orig:
                        log.info("  annotated: %s", ann[:120])
                # Pad or truncate to match input length defensively
                if len(annotated) == len(recommendations):
                    return annotated
                log.warning(
                    "Annotator returned %d items for %d recommendations — falling back",
                    len(annotated), len(recommendations),
                )
                return recommendations

            result_str = _run_tool(tc.name, tc.input, cpmrn)
            log.info("  annotator tool=%s  result=%s", tc.name, result_str[:120])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "name": tc.name,
                "content": result_str,
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

    log.warning("Order safety annotator: did not call submit_annotated — returning originals")
    return recommendations
