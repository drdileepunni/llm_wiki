"""
ReActTracer — captures every round of a ReAct tool-use loop and persists
the full trace to MongoDB pipeline_traces for offline debugging.

Usage:
    tracer = ReActTracer(cpmrn, encounter, step="status_classifier", db=db)
    tracer.start_round(0)
    tracer.log_thinking("Model thinks about HR trend...")
    tracer.log_tool_call("get_vital_trend", {"vital_name": "HR", "n": 8})
    tracer.log_tool_result("get_vital_trend", "HR trend: 115, 110, 108...")
    tracer.end_round()
    tracer.save(final_output={"problems": [...]})

MongoDB document shape (pipeline_traces):
    {
      CPMRN, encounter, step, started_at, duration_ms, total_rounds,
      rounds: [
        {
          round, started_at, duration_ms,
          thinking: ["..."],          # LLMThinkingBlock texts
          tool_calls: [{name, args}],
          tool_results: [{name, result}],  # truncated to 1000 chars
          text: ["..."],
          tokens: {in, out}
        }
      ],
      final_output: {...}
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RESULT_CHARS = 1000  # truncate tool results stored in trace


class ReActTracer:
    def __init__(self, cpmrn: str, encounter: int, step: str, db: Any = None):
        self.cpmrn     = cpmrn
        self.encounter = encounter
        self.step      = step
        self.db        = db
        self.started_at = datetime.now(timezone.utc)
        self.rounds: list[dict] = []
        self._cur: dict = {}

    # ── round lifecycle ────────────────────────────────────────────────────────

    def start_round(self, round_num: int) -> None:
        self._cur = {
            "round":      round_num,
            "started_at": datetime.now(timezone.utc),
            "thinking":   [],
            "tool_calls": [],
            "tool_results": [],
            "text":       [],
            "tokens":     None,
        }

    def end_round(self) -> None:
        elapsed = (datetime.now(timezone.utc) - self._cur["started_at"]).total_seconds()
        self._cur["duration_ms"] = int(elapsed * 1000)
        self.rounds.append(self._cur)
        self._cur = {}

    # ── per-event logging ──────────────────────────────────────────────────────

    def log_thinking(self, text: str) -> None:
        self._cur.setdefault("thinking", []).append(text)
        preview = text[:300].replace("\n", " ")
        logger.debug(
            "[%s] %s enc=%d round=%d THINKING: %s%s",
            self.step, self.cpmrn, self.encounter,
            self._cur.get("round", "?"),
            preview, "…" if len(text) > 300 else "",
        )

    def log_tool_call(self, name: str, args: dict) -> None:
        self._cur.setdefault("tool_calls", []).append({"name": name, "args": args})
        try:
            args_str = json.dumps(args, default=str)
        except Exception:
            args_str = str(args)
        logger.debug(
            "[%s] %s enc=%d round=%d → CALL %s(%s)",
            self.step, self.cpmrn, self.encounter,
            self._cur.get("round", "?"), name, args_str,
        )

    def log_tool_result(self, name: str, result: str) -> None:
        stored = result[:_MAX_RESULT_CHARS] + ("…" if len(result) > _MAX_RESULT_CHARS else "")
        self._cur.setdefault("tool_results", []).append({"name": name, "result": stored})
        preview = result[:400].replace("\n", " | ")
        logger.debug(
            "[%s] %s enc=%d round=%d ← %s: %s%s",
            self.step, self.cpmrn, self.encounter,
            self._cur.get("round", "?"), name,
            preview, "…" if len(result) > 400 else "",
        )

    def log_text(self, text: str) -> None:
        self._cur.setdefault("text", []).append(text)
        preview = text[:200].replace("\n", " ")
        logger.debug(
            "[%s] %s enc=%d round=%d TEXT: %s%s",
            self.step, self.cpmrn, self.encounter,
            self._cur.get("round", "?"),
            preview, "…" if len(text) > 200 else "",
        )

    def log_tokens(self, input_tokens: int, output_tokens: int, thinking_tokens: int = 0) -> None:
        self._cur["tokens"] = {"in": input_tokens, "out": output_tokens, "thinking": thinking_tokens}
        logger.debug(
            "[%s] %s enc=%d round=%d tokens in=%d out=%d thinking=%d",
            self.step, self.cpmrn, self.encounter,
            self._cur.get("round", "?"), input_tokens, output_tokens, thinking_tokens,
        )

    # ── persist ────────────────────────────────────────────────────────────────

    def save(self, final_output: Any = None) -> None:
        if self.db is None:
            logger.debug("[%s] tracer: no db — skipping save for %s enc=%d", self.step, self.cpmrn, self.encounter)
            return

        total_ms = int((datetime.now(timezone.utc) - self.started_at).total_seconds() * 1000)

        # Summarise token totals across all rounds
        total_in      = sum((r.get("tokens") or {}).get("in", 0)       for r in self.rounds)
        total_out     = sum((r.get("tokens") or {}).get("out", 0)      for r in self.rounds)
        total_think   = sum((r.get("tokens") or {}).get("thinking", 0) for r in self.rounds)

        doc = {
            "CPMRN":        self.cpmrn,
            "encounter":    self.encounter,
            "step":         self.step,
            "started_at":   self.started_at,
            "duration_ms":  total_ms,
            "total_rounds": len(self.rounds),
            "total_tokens": {"in": total_in, "out": total_out, "thinking": total_think},
            "rounds":       self.rounds,
            "final_output": final_output,
        }

        try:
            self.db["pipeline_traces"].insert_one(doc)
            logger.info(
                "[%s] trace saved for %s enc=%d — %d rounds, %dms, %d+%d tokens",
                self.step, self.cpmrn, self.encounter,
                len(self.rounds), total_ms, total_in, total_out,
            )
        except Exception:
            logger.exception("[%s] tracer: failed to save trace for %s enc=%d", self.step, self.cpmrn, self.encounter)
