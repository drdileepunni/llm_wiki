"""
Unified LLM client abstraction.

Supports Anthropic (claude-*) and Google Gemini (gemini-*) models behind a
single interface so the rest of the pipeline doesn't care which provider is
active.

Usage
-----
    from .llm_client import get_llm_client

    client = get_llm_client()
    response = client.create_message(
        messages=[{"role": "user", "content": "..."}],
        tools=[PLAN_TOOL],           # Anthropic tool-schema format
        system=system_prompt,
        max_tokens=8000,
        force_tool=True,             # maps to tool_choice=any / mode=ANY
    )
    # response.stop_reason   → "end_turn" | "tool_use" | "max_tokens"
    # response.content       → list of LLMToolUseBlock | LLMTextBlock
    # response.usage         → LLMUsage(input_tokens, output_tokens)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("wiki.llm_client")


# ── Shared response types ────────────────────────────────────────────────────


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMToolUseBlock:
    type: str = "tool_use"
    name: str = ""
    input: dict = field(default_factory=dict)
    id: str = ""


@dataclass
class LLMTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class LLMResponse:
    stop_reason: str          # "end_turn" | "tool_use" | "max_tokens"
    content: list[Any]        # LLMToolUseBlock | LLMTextBlock items
    usage: LLMUsage


# ── Anthropic backend ────────────────────────────────────────────────────────


_TRANSCRIBE_PROMPT = (
    "Transcribe ALL visible text from this document page exactly as it appears, "
    "preserving its structure and hierarchy.\n"
    "Rules:\n"
    "- Format tables as markdown tables (use | separators)\n"
    "- For flowcharts, diagrams, or figures write: [FIGURE: one-sentence description]\n"
    "- Preserve headings, bullet points, numbered lists\n"
    "- Include headers, footers, page numbers, and sidebar text\n"
    "- Do not add commentary, interpretation, or summaries — faithful transcription only\n"
    "Page {page_num} of {total_pages}."
)


class AnthropicLLMClient:
    def __init__(self, api_key: str, model: str):
        import anthropic
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def transcribe_page(
        self, image_bytes: bytes, page_num: int, total_pages: int
    ) -> tuple[str, LLMUsage]:
        """Send a rendered page image to the model and return its text transcription."""
        import base64

        b64 = base64.b64encode(image_bytes).decode()
        prompt = _TRANSCRIBE_PROMPT.format(page_num=page_num, total_pages=total_pages)

        raw = self._client.messages.create(
            model=self.model,
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        text = raw.content[0].text if raw.content else ""
        return text, LLMUsage(raw.usage.input_tokens, raw.usage.output_tokens)

    def create_message(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
        max_tokens: int = 4000,
        force_tool: bool = True,
        temperature: float | None = None,
    ) -> LLMResponse:
        tool_choice = {"type": "any"} if force_tool else {"type": "auto"}
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
            messages=messages,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        raw = self._client.messages.create(**kwargs)
        content = []
        for block in raw.content:
            if block.type == "tool_use":
                content.append(LLMToolUseBlock(name=block.name, input=block.input, id=block.id))
            else:
                content.append(LLMTextBlock(text=getattr(block, "text", "")))

        return LLMResponse(
            stop_reason=raw.stop_reason,
            content=content,
            usage=LLMUsage(raw.usage.input_tokens, raw.usage.output_tokens),
        )


# ── Gemini backend ───────────────────────────────────────────────────────────


def _convert_schema_types(schema: dict) -> dict:
    """
    Gemini's new SDK (google-genai) requires JSON Schema type values to be
    UPPERCASE ("STRING", "OBJECT", "ARRAY", …) whereas Anthropic uses
    lowercase ("string", "object", "array", …).  This converter recurses
    through the schema dict and uppercases all "type" values.
    """
    if not isinstance(schema, dict):
        return schema
    result = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            result[k] = v.upper()
        elif k in ("properties", ) and isinstance(v, dict):
            result[k] = {pk: _convert_schema_types(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            result[k] = _convert_schema_types(v)
        elif isinstance(v, list):
            result[k] = [_convert_schema_types(i) if isinstance(i, dict) else i for i in v]
        elif isinstance(v, dict):
            result[k] = _convert_schema_types(v)
        else:
            result[k] = v
    return result


def _anthropic_tool_to_gemini_decl(tool: dict):
    """
    Convert an Anthropic tool definition into a google-genai FunctionDeclaration.

    Anthropic:  {"name": "...", "description": "...", "input_schema": {JSON Schema}}
    Gemini:     types.FunctionDeclaration(name, description, parameters={JSON Schema uppercase types})
    """
    from google.genai import types

    schema = _convert_schema_types(tool.get("input_schema", {}))
    return types.FunctionDeclaration(
        name=tool["name"],
        description=tool.get("description", ""),
        parameters=schema,
    )


def _to_python(obj) -> Any:
    """
    Recursively convert Gemini response structures (MapComposite, protos, etc.)
    into plain Python dicts / lists so they're JSON-serialisable.
    """
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if hasattr(obj, "items"):           # dict-like
        return {k: _to_python(v) for k, v in obj.items()}
    if hasattr(obj, "__iter__"):        # list-like
        return [_to_python(v) for v in obj]
    return obj


class GeminiLLMClient:
    """
    LLM client backed by Google Gemini via the google-genai SDK (v1+).
    """

    def __init__(self, api_key: str, model: str):
        from google import genai
        self._client = genai.Client(api_key=api_key)
        self.model = model

    def transcribe_page(
        self, image_bytes: bytes, page_num: int, total_pages: int
    ) -> tuple[str, LLMUsage]:
        """Send a rendered page image to the model and return its text transcription."""
        from google.genai import types

        prompt = _TRANSCRIBE_PROMPT.format(page_num=page_num, total_pages=total_pages)

        raw = self._client.models.generate_content(
            model=self.model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                        types.Part(text=prompt),
                    ],
                )
            ],
            config=types.GenerateContentConfig(max_output_tokens=4000),
        )

        text   = raw.text or ""
        meta   = raw.usage_metadata
        in_tok  = getattr(meta, "prompt_token_count", 0) or 0
        out_tok = getattr(meta, "candidates_token_count", 0) or 0
        return text, LLMUsage(in_tok, out_tok)

    def create_message(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str = "",
        max_tokens: int = 4000,
        force_tool: bool = True,
        thinking_budget: int | None = None,
        temperature: float | None = None,
        _retries: int = 3,
    ) -> LLMResponse:
        import time
        from google.genai import types

        # ── tools ────────────────────────────────────────────────────────────
        fn_decls = [_anthropic_tool_to_gemini_decl(t) for t in tools]

        # ── tool config (skip entirely when no tools — Gemini rejects it) ────
        if fn_decls:
            mode = "ANY" if force_tool else "AUTO"
            gemini_tools     = [types.Tool(function_declarations=fn_decls)]
            gemini_tool_cfg  = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=mode)
            )
        else:
            gemini_tools    = []
            gemini_tool_cfg = None

        # ── convert messages to Gemini format ────────────────────────────────
        gemini_contents = []
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            content = msg["content"]
            if isinstance(content, list):
                import base64, json as _json
                parts = []
                for block in content:
                    btype = block.get("type")
                    if btype == "image":
                        src = block["source"]
                        img_bytes = base64.b64decode(src["data"])
                        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=src.get("media_type", "image/jpeg")))
                    elif btype == "text":
                        parts.append(types.Part(text=block["text"]))
                    elif btype == "tool_use":
                        # assistant calling a function
                        parts.append(types.Part(function_call=types.FunctionCall(
                            name=block["name"],
                            args=block.get("input", {}),
                        )))
                    elif btype == "tool_result":
                        # user returning a function result
                        raw_content = block.get("content", "")
                        try:
                            response_data = _json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                        except Exception:
                            response_data = {"output": raw_content}
                        parts.append(types.Part(function_response=types.FunctionResponse(
                            name=block.get("name", "tool"),
                            response=response_data,
                        )))
                if parts:
                    gemini_contents.append(types.Content(role=role, parts=parts))
            else:
                gemini_contents.append(
                    types.Content(role=role, parts=[types.Part(text=content)])
                )

        # ── thinking config (Gemini 2.5 thinking tokens eat into max_output_tokens) ──
        thinking_cfg = None
        if thinking_budget is not None:
            thinking_cfg = types.ThinkingConfig(thinking_budget=thinking_budget)

        # ── generate (with escalating retry on MALFORMED_FUNCTION_CALL) ────────
        # Attempt 1: ANY mode, thinking disabled (most reliable for tool calls)
        # Attempt 2: AUTO mode + temperature=0  (lets model choose text vs tool)
        # Attempt 3: no tools, pure text + JSON extraction in caller
        # If caller explicitly set thinking_budget, honour it for all models.
        # Otherwise disable thinking when tools are present (most reliable for tool calls),
        # except gemini-2.5-pro which rejects budget=0 entirely.
        _requires_thinking = "2.5-pro" in self.model
        if fn_decls and thinking_budget is None and not _requires_thinking:
            no_thinking = types.ThinkingConfig(thinking_budget=0)
        else:
            no_thinking = thinking_cfg  # None → omitted, or caller-specified budget

        _temperature = temperature if temperature is not None else 0.0

        def _make_config(attempt: int) -> "types.GenerateContentConfig":
            if attempt == 1:
                return types.GenerateContentConfig(
                    system_instruction=system or None,
                    tools=gemini_tools or None,
                    tool_config=gemini_tool_cfg,
                    max_output_tokens=max_tokens,
                    thinking_config=no_thinking,
                    temperature=_temperature,
                )
            if attempt == 2:
                # Relax to AUTO — Gemini can return text if function call is tricky
                auto_cfg = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="AUTO")
                ) if fn_decls else None
                return types.GenerateContentConfig(
                    system_instruction=system or None,
                    tools=gemini_tools or None,
                    tool_config=auto_cfg,
                    max_output_tokens=max_tokens,
                    thinking_config=no_thinking,
                    temperature=_temperature,
                )
            # attempt 3: no tools at all — plain text, caller handles JSON extraction
            return types.GenerateContentConfig(
                system_instruction=system or None,
                max_output_tokens=max_tokens,
                thinking_config=no_thinking,
                temperature=_temperature,
            )

        raw = None
        for attempt in range(1, _retries + 1):
            config = _make_config(attempt)
            raw = self._client.models.generate_content(
                model=self.model,
                contents=gemini_contents,
                config=config,
            )
            finish_check = str(getattr(raw.candidates[0], "finish_reason", "")).upper()
            if "MALFORMED" in finish_check:
                wait = attempt * 2
                log.warning(
                    "Gemini MALFORMED_FUNCTION_CALL on attempt %d/%d — retrying in %ds (next: %s)",
                    attempt, _retries, wait,
                    "AUTO mode" if attempt == 1 else "no-tools text",
                )
                if attempt < _retries:
                    time.sleep(wait)
                    continue
            break  # success or non-retryable finish reason

        # ── parse candidate ──────────────────────────────────────────────────
        candidate = raw.candidates[0]
        finish    = str(getattr(candidate, "finish_reason", "")).upper()
        log.debug("Gemini finish_reason=%s", finish)

        if "MAX_TOKEN" in finish:
            stop_reason = "max_tokens"
        elif "MALFORMED" in finish:
            log.error("Gemini MALFORMED_FUNCTION_CALL after %d retries — giving up", _retries)
            stop_reason = "malformed"
        elif "SAFETY" in finish or "RECITATION" in finish or "OTHER" in finish:
            log.warning("Gemini blocked response: finish_reason=%s", finish)
            stop_reason = "blocked"
        else:
            stop_reason = "end_turn"

        # ── parse content blocks ─────────────────────────────────────────────
        content: list[Any] = []
        candidate_content = getattr(candidate, "content", None)
        if candidate_content is None:
            log.warning("Gemini candidate.content is None (finish_reason=%s)", finish)
            meta    = raw.usage_metadata
            in_tok  = getattr(meta, "prompt_token_count", 0) or 0
            out_tok = getattr(meta, "candidates_token_count", 0) or 0
            return LLMResponse(
                stop_reason=stop_reason,
                content=[],
                usage=LLMUsage(in_tok, out_tok),
            )

        for part in candidate_content.parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                args = _to_python(dict(fc.args) if hasattr(fc.args, "items") else fc.args)
                content.append(LLMToolUseBlock(name=fc.name, input=args, id=f"gemini_{fc.name}"))
                stop_reason = "tool_use"
            elif getattr(part, "text", None):
                content.append(LLMTextBlock(text=part.text))

        # ── JSON extraction fallback: if no tool block found but text has JSON ──
        # This handles attempt-3 (no-tools) responses where Gemini embeds the
        # tool payload in plain text (often inside a ```json block).
        if not any(b.type == "tool_use" for b in content) and tools:
            raw_text = " ".join(b.text for b in content if b.type == "text")
            import json as _json, re as _re
            m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, _re.DOTALL)
            if not m:
                m = _re.search(r"(\{.*\})", raw_text, _re.DOTALL)
            if m:
                try:
                    parsed = _json.loads(m.group(1))
                    tool_name = tools[0]["name"] if tools else "extracted"
                    content.append(LLMToolUseBlock(name=tool_name, input=parsed))
                    stop_reason = "tool_use"
                    log.info("Gemini text-fallback: extracted JSON tool block from plain text")
                except Exception:
                    pass

        # ── usage ────────────────────────────────────────────────────────────
        meta    = raw.usage_metadata
        in_tok  = getattr(meta, "prompt_token_count", 0) or 0
        out_tok = getattr(meta, "candidates_token_count", 0) or 0

        return LLMResponse(
            stop_reason=stop_reason,
            content=content,
            usage=LLMUsage(in_tok, out_tok),
        )


# ── Factory ──────────────────────────────────────────────────────────────────


def get_llm_client(model: str | None = None) -> AnthropicLLMClient | GeminiLLMClient:
    """Return the right client. Uses MODEL env var unless overridden by `model`."""
    from ..config import MODEL, ANTHROPIC_API_KEY, GOOGLE_API_KEY

    resolved = model or MODEL
    if resolved.startswith("gemini"):
        if not GOOGLE_API_KEY:
            raise RuntimeError("MODEL is a Gemini model but GOOGLE_API_KEY is not set in .env")
        log.debug("Using Gemini client  model=%s", resolved)
        return GeminiLLMClient(api_key=GOOGLE_API_KEY, model=resolved)

    log.debug("Using Anthropic client  model=%s", resolved)
    return AnthropicLLMClient(api_key=ANTHROPIC_API_KEY, model=resolved)
