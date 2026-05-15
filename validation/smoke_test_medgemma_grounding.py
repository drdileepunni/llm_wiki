"""
Smoke test: MedGemma + _CDS_GROUND_TOOL schema
===============================================
Tests whether medgemma:4b can:
  1. Receive the full _CDS_GROUND_TOOL schema (injected as text prompt)
  2. Return valid JSON that matches the schema
  3. Have that JSON correctly parsed into an LLMToolUseBlock by OllamaLLMClient

Run from the repo root:
    python validation/smoke_test_medgemma_grounding.py

No app server needed — imports OllamaLLMClient directly.
"""

import sys
import json
import textwrap

MEDGEMMA_URL = "http://35.222.113.0:11435"
MEDGEMMA_API_KEY = "ca9e4b6eaf35a4b4c3d5512f101fd7032e5eb6bc0e68ab1dc4ab429449111432"
MODEL = "medgemma:4b"

# ── Copy of _CDS_GROUND_TOOL from chat_pipeline.py ──────────────────────────

CDS_GROUND_TOOL = {
    "name": "ground_parameters",
    "description": "For each parameter, state whether the provided wiki context confirms the value.",
    "input_schema": {
        "type": "object",
        "properties": {
            "groundings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "parameter": {"type": "string"},
                        "value": {
                            "type": "string",
                            "description": "The confirmed value/dose/range if found. Empty string if not found.",
                        },
                        "grounded": {
                            "type": "boolean",
                            "description": "true only if context explicitly supports this value.",
                        },
                        "source": {
                            "type": "string",
                            "description": "Wiki page path that confirms the value. Empty if not grounded.",
                        },
                        "resolution_question": {
                            "type": "string",
                            "description": "Only when grounded=false. Precise clinical question to fill the gap.",
                        },
                        "missing_values": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Only when grounded=false. Specific data points needed but not found.",
                        },
                        "entity": {
                            "type": "string",
                            "description": "The drug, device, or clinical entity this parameter belongs to.",
                        },
                        "section_heading": {
                            "type": "string",
                            "description": "The ## section heading this gap belongs to on the wiki page.",
                        },
                        "subtype": {
                            "type": "string",
                            "enum": ["medication", "investigation", "procedure", "condition", "default"],
                            "description": "Page type of the target wiki page for this gap.",
                        },
                    },
                    "required": ["parameter", "value", "grounded"],
                },
                "description": "One entry per parameter submitted.",
            },
        },
        "required": ["groundings"],
    },
}

# ── Test cases ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = textwrap.dedent("""
    You are a clinical grounding assistant. You will be given:
    1. A list of clinical parameters to look up
    2. A wiki context excerpt (may be empty or partial)

    Your job: for each parameter, decide if the wiki context confirms an exact,
    actionable clinical value. Output your grounding decision using the tool schema.
""").strip()

# Simulated wiki context for noradrenaline dosing
WIKI_CONTEXT = textwrap.dedent("""
    ## Noradrenaline (Norepinephrine)
    ### Dosing
    - Initial dose: 0.01–0.1 mcg/kg/min IV infusion
    - Titrate by 0.05 mcg/kg/min every 5–10 minutes to target MAP ≥ 65 mmHg
    - Maximum dose: 3 mcg/kg/min (some centres allow higher in refractory shock)
    - Preparation: 4 mg in 50 mL NS = 80 mcg/mL

    ## Vasopressin
    ### Dosing
    - Fixed dose: 0.03–0.04 units/min IV (do NOT titrate)
    - Added as adjunct when noradrenaline > 0.25 mcg/kg/min
""").strip()

# Parameters to ground — one grounded, one not in context
SPECIFIC_QUERIES = [
    {"parameter": "noradrenaline_starting_dose", "search_query": "noradrenaline initial dose septic shock"},
    {"parameter": "hydrocortisone_dose_refractory_shock", "search_query": "hydrocortisone dose refractory vasoplegic shock"},
]

USER_MESSAGE = textwrap.dedent(f"""
    Clinical context: 65-year-old patient in septic shock, MAP 48 mmHg despite 3L fluid resuscitation.

    Parameters to ground:
    {json.dumps(SPECIFIC_QUERIES, indent=2)}

    Wiki context available:
    {WIKI_CONTEXT}

    Ground each parameter against the wiki context above.
    For parameters not supported by the context, set grounded=false and populate resolution_question, missing_values, entity, section_heading, subtype.
""").strip()


# ── Test runner ──────────────────────────────────────────────────────────────

def run_smoke_test():
    sys.path.insert(0, "/Users/drdileepunni/github_/llm_wiki/app/backend")
    from services.llm_client import OllamaLLMClient, LLMToolUseBlock

    client = OllamaLLMClient(
        base_url=MEDGEMMA_URL,
        api_key=MEDGEMMA_API_KEY,
        model=MODEL,
    )

    print("=" * 60)
    print(f"SMOKE TEST: MedGemma grounding tool schema")
    print(f"Model  : {MODEL}")
    print(f"URL    : {MEDGEMMA_URL}")
    print("=" * 60)

    # ── Test 1: Basic connectivity ───────────────────────────────────────────
    print("\n[Test 1] Basic connectivity (plain chat, no tool schema)...")
    try:
        r = client.create_message(
            messages=[{"role": "user", "content": "Reply with just the word PONG."}],
            tools=[],
            system="",
            max_tokens=20,
            force_tool=False,
        )
        text = r.content[0].text if r.content else ""
        ok = "PONG" in text.upper()
        print(f"  Response : {text!r}")
        print(f"  Tokens   : in={r.usage.input_tokens} out={r.usage.output_tokens}")
        print(f"  Result   : {'✓ PASS' if ok else '✗ FAIL (no PONG in response)'}")
    except Exception as e:
        print(f"  Result   : ✗ FAIL — {e}")
        return

    # ── Test 2: Schema injection — does prompt render correctly? ─────────────
    print("\n[Test 2] Schema injection — inspect rendered prompt...")
    schema_block = client._tool_schema_prompt([CDS_GROUND_TOOL])
    char_count = len(schema_block)
    has_required = all(k in schema_block for k in ["groundings", "grounded", "missing_values", "subtype"])
    print(f"  Schema block length : {char_count} chars")
    print(f"  Required fields present : {'✓ yes' if has_required else '✗ NO — schema rendering broken'}")
    print(f"  First 300 chars:\n{textwrap.indent(schema_block[:300], '    ')}")

    # ── Test 3: Full grounding call with tool schema ──────────────────────────
    print("\n[Test 3] Full grounding call with _CDS_GROUND_TOOL schema...")
    try:
        r = client.create_message(
            messages=[{"role": "user", "content": USER_MESSAGE}],
            tools=[CDS_GROUND_TOOL],
            system=SYSTEM_PROMPT,
            max_tokens=1200,
            force_tool=True,
        )
        print(f"  Stop reason : {r.stop_reason}")
        print(f"  Tokens      : in={r.usage.input_tokens} out={r.usage.output_tokens}")
        print(f"  Blocks      : {len(r.content)}")

        if r.stop_reason == "tool_use" and r.content and isinstance(r.content[0], LLMToolUseBlock):
            block = r.content[0]
            data = block.input
            print(f"\n  ✓ Parsed as LLMToolUseBlock (tool: {block.name!r})")
            groundings = data.get("groundings", [])
            print(f"  Groundings returned: {len(groundings)}")

            all_pass = True
            for g in groundings:
                param = g.get("parameter", "?")
                grounded = g.get("grounded")
                value = g.get("value", "")
                source = g.get("source", "")
                entity = g.get("entity", "")
                section = g.get("section_heading", "")
                subtype = g.get("subtype", "")
                missing = g.get("missing_values", [])
                resolution_q = g.get("resolution_question", "")

                # Validation checks
                checks = {
                    "has 'parameter'": bool(param),
                    "has 'grounded' (bool)": isinstance(grounded, bool),
                    "grounded=True → value non-empty": (not grounded) or bool(value),
                    "grounded=False → resolution_question": grounded or bool(resolution_q),
                    "grounded=False → missing_values list": grounded or isinstance(missing, list),
                    "grounded=False → entity": grounded or bool(entity),
                    "grounded=False → section_heading": grounded or bool(section),
                    "grounded=False → subtype valid": grounded or subtype in {"medication","investigation","procedure","condition","default"},
                }
                failed = [k for k, v in checks.items() if not v]
                status = "✓" if not failed else "✗"
                if failed:
                    all_pass = False

                print(f"\n  [{status}] Parameter: {param!r}")
                print(f"       grounded={grounded}  value={value!r}")
                print(f"       entity={entity!r}  section={section!r}  subtype={subtype!r}")
                if not grounded:
                    print(f"       resolution_q={resolution_q!r}")
                    print(f"       missing_values={missing}")
                if failed:
                    print(f"       FAILED CHECKS: {failed}")

            print(f"\n  Overall schema compliance: {'✓ ALL PASS' if all_pass else '✗ SOME CHECKS FAILED'}")

        else:
            # Fallback — raw text returned instead of JSON
            raw = r.content[0].text if r.content else ""
            print(f"  ✗ Did NOT parse as tool_use — stop_reason={r.stop_reason!r}")
            print(f"  Raw response (first 500 chars):\n{textwrap.indent(raw[:500], '    ')}")

    except Exception as e:
        import traceback
        print(f"  ✗ EXCEPTION: {e}")
        traceback.print_exc()

    # ── Test 4: Edge case — empty wiki context ────────────────────────────────
    print("\n[Test 4] Edge case — no wiki context (all params should be ungrounded)...")
    try:
        no_context_msg = textwrap.dedent(f"""
            Clinical context: Patient in cardiogenic shock.

            Parameters to ground:
            {json.dumps([{"parameter": "dobutamine_starting_dose", "search_query": "dobutamine dose cardiogenic shock"}], indent=2)}

            Wiki context available:
            (none — no relevant pages found)

            Ground each parameter. Since there is no wiki context, all should be grounded=false.
        """).strip()

        r = client.create_message(
            messages=[{"role": "user", "content": no_context_msg}],
            tools=[CDS_GROUND_TOOL],
            system=SYSTEM_PROMPT,
            max_tokens=600,
            force_tool=True,
        )
        if r.stop_reason == "tool_use" and r.content and isinstance(r.content[0], LLMToolUseBlock):
            groundings = r.content[0].input.get("groundings", [])
            if groundings and groundings[0].get("grounded") is False:
                print(f"  ✓ PASS — correctly returned grounded=false with no context")
                print(f"  resolution_question: {groundings[0].get('resolution_question','')!r}")
            else:
                print(f"  ✗ FAIL — expected grounded=false, got: {groundings}")
        else:
            print(f"  ✗ Did not parse as tool_use: {r.stop_reason}")
    except Exception as e:
        print(f"  ✗ EXCEPTION: {e}")

    print("\n" + "=" * 60)
    print("Smoke test complete.")
    print("=" * 60)


if __name__ == "__main__":
    run_smoke_test()
