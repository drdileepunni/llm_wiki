"""
Viva student agent — agentic CDS for live patient data.

Replaces run_custom_snapshot for viva turns. Instead of summarising a static
CSV timeline, the LLM actively queries the patient's MongoDB chart using tools,
building up only the data it needs to answer the clinical question.

Flow
----
Phase 1 — ReAct tool loop (max 6 rounds):
    LLM calls tools (get_vitals, get_lab_result, get_io_summary, …)
    Each result is returned and the LLM reasons further until it has enough data.
    The loop produces a grounded data brief (actual patient values).

Phase 2 — Wiki-grounded CDS synthesis:
    The data brief + clinical context + question are passed to run_chat(mode="cds"),
    which does wiki vector retrieval and produces clinical_direction,
    specific_parameters, immediate_next_steps, gap_sections, etc.
    This is identical to run_custom_snapshot's second half.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from .llm_client import get_llm_client, LLMToolUseBlock
from .token_tracker import log_call, calculate_cost
from .chat_pipeline import run_chat, scan_text_gaps
from .emr import (
    get_latest_vitals,
    get_latest_labs,
    get_io_summary,
    get_vital_trend,
    get_recent_notes_for_patient,
    get_active_orders,
)
from ..config import MODEL, KBConfig

log = logging.getLogger("wiki.viva_student")

_MAX_TOOL_ROUNDS = 6

# ── Tool schema (Anthropic-format, translated automatically for Gemini) ────────

STUDENT_TOOLS = [
    {
        "name": "get_vitals",
        "description": (
            "Get the most recent vital signs snapshot for this patient: "
            "BP, HR, SpO2, RR, Temperature, FiO2, TherapyDevice, ventilator settings."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_lab_result",
        "description": (
            "Get the latest result for a specific lab test. "
            "Examples: 'VBG', 'ABG', 'Creatinine', 'Hb', 'WBC', 'Lactate', 'BNP', 'Troponin'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_name": {
                    "type": "string",
                    "description": "Exact lab test name as ordered, e.g. 'VBG', 'Creatinine'.",
                }
            },
            "required": ["test_name"],
        },
    },
    {
        "name": "get_io_summary",
        "description": (
            "Get intake/output summary over the last N hours: "
            "total urine output, total intake, net fluid balance, and urine rate (mL/hr)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Number of hours back to summarise (default 6).",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_notes",
        "description": (
            "Get recent clinical event notes: ECG findings, nursing notes, procedure results. "
            "Use category='event' for ECG/monitoring notes, or omit for all recent notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Note category: 'event', 'labs', 'nursing'. Omit for all.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max notes to return (default 3).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_active_orders",
        "description": (
            "Get all currently active orders: medications, pending labs, procedures, vents. "
            "Use this to check if a drug is already running or a lab is already pending."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_vital_trend",
        "description": (
            "Get a time-series of one vital sign over the last N hours. "
            "Use this to assess trajectory (improving / worsening / stable)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "parameter": {
                    "type": "string",
                    "description": "Vital name: BP, HR, SpO2, RR, Temperature, FiO2, MAP.",
                },
                "hours": {
                    "type": "integer",
                    "description": "How many hours of history (default 4).",
                },
            },
            "required": ["parameter"],
        },
    },
]

_SYSTEM = """\
You are a senior ICU clinician assessing a patient in a clinical training simulation.
The patient's chart is live in the hospital EMR. Use the available tools to retrieve
the specific data you need to answer the clinical question.

Guidelines:
- Think step-by-step about what clinical data is most relevant to this question.
- Retrieve only what you need — do not call every tool blindly.
- After each tool result, decide whether you have enough information or need more.
- When you have gathered sufficient data, output a concise PATIENT DATA BRIEF summarising
  what you found (actual values with timestamps where available).
- The brief will be used for further clinical reasoning and wiki-grounded synthesis.
- Do NOT output a management plan yet — only the data brief.
"""

_DONE_SYSTEM = """\
You are a senior ICU clinician. Based on the patient data you have retrieved,
write a concise clinical handover brief (200-300 words) covering:

**Primary Syndrome** — dominant problem and severity.
**Key Abnormalities** — bulleted, with actual values from the retrieved data.
**Active Medications & Interventions** — what is currently running.
**Current Status** — trajectory and most urgent concern.

Use only the data retrieved from tools. Do not infer values that were not returned.
"""


# ── Tool executor ──────────────────────────────────────────────────────────────

def _run_tool(name: str, inputs: dict, cpmrn: str) -> str:
    """Execute one tool call and return a JSON string result."""
    try:
        if name == "get_vitals":
            result = get_latest_vitals(cpmrn)
        elif name == "get_lab_result":
            raw = get_latest_labs(cpmrn, tests=[inputs["test_name"]])
            labs = raw.get("labs", {})
            result = labs if labs else {"message": f"No result found for {inputs['test_name']}"}
        elif name == "get_io_summary":
            result = get_io_summary(cpmrn, hours=inputs.get("hours", 6))
        elif name == "get_recent_notes":
            result = get_recent_notes_for_patient(
                cpmrn,
                category=inputs.get("category"),
                limit=inputs.get("limit", 3),
            )
        elif name == "get_active_orders":
            result = get_active_orders(cpmrn)
        elif name == "get_vital_trend":
            result = get_vital_trend(
                cpmrn,
                parameter=inputs["parameter"],
                hours=inputs.get("hours", 4),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        result = {"error": str(exc)}

    return json.dumps(result, default=str)


# ── Phase 1: ReAct tool loop ───────────────────────────────────────────────────

def _tool_loop(
    clinical_context: str,
    question: str,
    cpmrn: str,
    llm,
) -> tuple[str, int, int]:
    """
    Run the tool-use ReAct loop.
    Returns (data_brief, total_input_tokens, total_output_tokens).
    """
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"CLINICAL CONTEXT:\n{clinical_context}\n\n"
                f"QUESTION TO ANSWER:\n{question}\n\n"
                "Use the available tools to retrieve the patient data you need, "
                "then output a PATIENT DATA BRIEF."
            ),
        }
    ]

    total_in = total_out = 0

    for round_num in range(1, _MAX_TOOL_ROUNDS + 1):
        response = llm.create_message(
            messages=messages,
            tools=STUDENT_TOOLS,
            system=_SYSTEM,
            max_tokens=2000,
            force_tool=False,
        )
        total_in  += response.usage.input_tokens
        total_out += response.usage.output_tokens

        tool_calls = [b for b in response.content if isinstance(b, LLMToolUseBlock)]
        text_blocks = [b for b in response.content if hasattr(b, "text") and b.text]

        log.info(
            "Student agent round %d: %d tool call(s)  stop=%s",
            round_num, len(tool_calls), response.stop_reason,
        )

        if not tool_calls:
            # LLM is done — extract the data brief from text output
            data_brief = "\n".join(b.text for b in text_blocks).strip()
            log.info("Student agent: tool loop complete after %d rounds", round_num)
            return data_brief, total_in, total_out

        # Build assistant content (text + tool_use blocks)
        # thought_signature must be forwarded so Gemini accepts the history.
        assistant_content = []
        for b in response.content:
            if isinstance(b, LLMToolUseBlock):
                block_dict: dict = {
                    "type": "tool_use",
                    "id": b.id or f"tool_{round_num}_{b.name}",
                    "name": b.name,
                    "input": b.input,
                }
                if b.thought_signature is not None:
                    block_dict["thought_signature"] = b.thought_signature
                assistant_content.append(block_dict)
            elif hasattr(b, "text") and b.text:
                assistant_content.append({"type": "text", "text": b.text})

        messages.append({"role": "assistant", "content": assistant_content})

        # Execute tools and build tool_result blocks
        tool_result_content = []
        for tc in tool_calls:
            tool_id = tc.id or f"tool_{round_num}_{tc.name}"
            result_str = _run_tool(tc.name, tc.input, cpmrn)
            log.info("  tool=%s  result=%s", tc.name, result_str[:120])
            tool_result_content.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "name": tc.name,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_result_content})

    # Hit max rounds — ask the LLM to summarise what it has so far
    log.warning("Student agent: hit max rounds (%d), forcing summary", _MAX_TOOL_ROUNDS)
    messages.append({
        "role": "user",
        "content": "You have reached the tool call limit. Please output the PATIENT DATA BRIEF now based on what you have retrieved.",
    })
    final = llm.create_message(
        messages=messages,
        tools=[],
        system=_DONE_SYSTEM,
        max_tokens=1500,
        force_tool=False,
    )
    total_in  += final.usage.input_tokens
    total_out += final.usage.output_tokens
    data_brief = "\n".join(
        b.text for b in final.content if hasattr(b, "text") and b.text
    ).strip()
    return data_brief, total_in, total_out


# ── Main entry point ───────────────────────────────────────────────────────────

def run_viva_student_turn(
    clinical_context: str,
    question: str,
    phase: str,
    difficulty: str,
    cpmrn: str,
    kb: KBConfig,
    model: str | None = None,
) -> dict:
    """
    Run one viva student assessment turn.

    Replaces run_custom_snapshot for viva sessions.
    Returns the same schema as run_custom_snapshot's snapshot dict.
    """
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    chat_run_id = str(uuid.uuid4())
    resolved_model = model or MODEL

    llm = get_llm_client(model=model)

    # ── Phase 1: tool loop → data brief ───────────────────────────────────────
    log.info("Viva student agent: starting tool loop  cpmrn=%s  model=%s", cpmrn, resolved_model)
    data_brief, loop_in, loop_out = _tool_loop(clinical_context, question, cpmrn, llm)
    loop_cost = calculate_cost(loop_in, loop_out, resolved_model)
    log.info(
        "Viva student agent: data brief ready (%d chars)  in=%d  out=%d  cost=$%.4f",
        len(data_brief), loop_in, loop_out, loop_cost,
    )

    # ── Phase 2: wiki-grounded CDS synthesis ──────────────────────────────────
    # Assemble the question the same way run_custom_snapshot does:
    # data_brief plays the role of _summarize_timeline(csv_content)
    assembled_question = (
        f"{data_brief}\n\n"
        f"Clinical context: {clinical_context}\n\n"
        f"Question: {question}"
    )

    log.info("Viva student agent: running wiki-grounded CDS synthesis")
    result = run_chat(
        question=assembled_question,
        kb=kb,
        model=model,
        mode="cds",
    )

    # ── Gap scan on immediate_next_steps ──────────────────────────────────────
    text_gaps = scan_text_gaps(
        steps=result.get("immediate_next_steps", []),
        clinical_context=clinical_context,
        kb=kb,
        model=model,
        source_name=f"viva-student: {run_id}",
        force_gap_step_texts=result.get("_step4_failed_texts"),
    )

    gap_sections = list(result.get("gap_sections") or [])
    if text_gaps:
        gap_sections += [g.split("/")[-1].replace(".md", "") for g in text_gaps]

    # ── Assemble snap dict (same schema as run_custom_snapshot) ──────────────
    total_in  = loop_in  + result["input_tokens"]
    total_out = loop_out + result["output_tokens"]
    total_cost = loop_cost + result["cost_usd"]

    snap = {
        "snapshot_num":          1,
        "snapshot_dir":          "viva",
        "clinical_context":      clinical_context,
        "question":              question,
        "phase":                 phase,
        "difficulty":            difficulty,
        "data_brief":            data_brief,          # what the agent retrieved
        "chat_run_id":           result.get("chat_run_id") or chat_run_id,
        "agent_answer":          result["answer"],
        "immediate_next_steps":  result.get("immediate_next_steps", []),
        "clinical_direction":    result.get("clinical_direction", []),
        "immediate_actions":     result.get("clinical_direction", []),
        "clinical_reasoning":    result.get("clinical_reasoning", []),
        "specific_parameters":   result.get("specific_parameters", []),
        "monitoring_followup":   result.get("monitoring_followup", []),
        "alternative_considerations": result.get("alternative_considerations", []),
        "pages_consulted":       result["pages_consulted"],
        "wiki_links":            result.get("wiki_links", []),
        "gap_registered":        result.get("gap_registered"),
        "gap_entity":            result.get("gap_entity"),
        "gap_sections":          gap_sections,
        "tokens_in":             total_in,
        "tokens_out":            total_out,
        "cost_usd":              total_cost,
    }

    return {"snapshots": [snap]}
