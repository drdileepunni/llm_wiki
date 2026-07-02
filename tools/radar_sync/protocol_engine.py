"""
Unified protocol engine — replaces the fragmented clinical_rule_blocks +
the inline _load_monitoring_protocols / _match_protocols / _build_gate_block
that lived in problem_tracker.

Every protocol document may carry any combination of three optional sections:

  guidance   — verbatim reasoning text injected into the system prompt.
               Absorbs clinical_rule_blocks: the five category blocks now live as
               seed protocol documents with this field.
  scenarios  — suppression gate (unchanged mechanics: gate_question, scenarios,
               escalation_target_after_window at top level).  Existing protocol
               documents are untouched — no migration required.
  audit      — documentation audit spec: required_documentation (list),
               window_hours (int), record_when_none (str).

The single entry point for problem_tracker is build_injection() which returns
(guidance_block, gate_block, matched_gate) in one call.  match_for_audit() is the
entry point for the hourly documentation audit sweep in scheduler.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ── Protocol loading ──────────────────────────────────────────────────────────

def load_protocols(db: Any) -> list[dict]:
    """
    Load all monitoring protocols from MongoDB.
    Falls back to seed-script constants on any failure so guidance blocks are
    always available even if the DB is unreachable.
    """
    try:
        docs = list(db["monitoring_protocols"].find({}))
        if docs:
            return docs
    except Exception:
        logger.exception("protocol_engine: failed to load monitoring_protocols — using seed fallback")
    try:
        from tools.radar_sync.seed_monitoring_protocols import PROTOCOLS
        return list(PROTOCOLS)
    except Exception:
        logger.exception("protocol_engine: seed fallback also failed — no protocols available")
        return []


# ── Guidance matching (absorbs clinical_rule_blocks) ─────────────────────────

def _problem_text(problems: list[dict]) -> str:
    """Join problem names + cause annotations — the haystack for keyword matching."""
    parts: list[str] = []
    for p in problems:
        parts.append(str(p.get("name", "")))
        if p.get("cause"):
            parts.append(str(p.get("cause")))
    return " ".join(parts).lower()


def _keyword_hit(proto: dict, haystack: str) -> bool:
    """True if any of the protocol's applies_when keywords appears in haystack (case-insensitive)."""
    return any(kw.lower() in haystack for kw in (proto.get("applies_when") or []))


def _match_first_by_section(
    protocols: list[dict],
    problems: list[dict],
    has_section: "Callable[[dict], bool]",
) -> dict[str, dict]:
    """
    Per-problem, first-match-wins keyword matching against protocols carrying a
    given section. Shared by match_for_gate (scenarios/gate_question) and
    match_for_audit (audit) — the two matchers that resolve one protocol per
    problem, as opposed to match_for_guidance's patient-wide multi-match.

    Returns {problem_name: protocol_doc}.
    """
    matched: dict[str, dict] = {}
    for prob in problems:
        name_lc = prob.get("name", "").lower()
        for proto in protocols:
            if not has_section(proto):
                continue
            if _keyword_hit(proto, name_lc):
                matched[prob.get("name", "")] = proto
                break
    return matched


def match_for_guidance(
    protocols: list[dict],
    problems: list[dict],
    prefetch_block: str = "",
    delta_categories: "set[str] | None" = None,
) -> list[dict]:
    """
    Return protocols whose `guidance` field should be injected for this patient.

    Matching mirrors clinical_rule_blocks.filter_blocks_for_patient exactly:
      • Primary:   any applies_when keyword appears in joined problem names/causes
      • Secondary: any applies_when keyword appears in prefetch_block text
                   (same dual-signal pattern as lab_alert_rules)
      • Causal:    applies_when_secondary=true fires when any problem carries a `cause`

    delta_categories: future scope — currently not used to restrict guidance protocols
    (over-injection costs minor attention; under-injection risks a missed safety rule).
    """
    haystack = _problem_text(problems)
    if prefetch_block:
        haystack += " " + prefetch_block.lower()

    has_secondary = any(p.get("cause") for p in problems)
    matched: list[dict] = []
    seen_ids: set[str] = set()

    for proto in protocols:
        if not proto.get("guidance"):
            continue
        pid = proto.get("protocol_id", "")
        if pid in seen_ids:
            continue

        # Causal / secondary trigger
        if proto.get("applies_when_secondary") and has_secondary:
            matched.append(proto)
            seen_ids.add(pid)
            continue

        # Keyword trigger — empty applies_when means "global, matches every problem"
        if not proto.get("applies_when") or _keyword_hit(proto, haystack):
            matched.append(proto)
            seen_ids.add(pid)

    return matched


def format_guidance_block(protocols: list[dict]) -> str:
    """Format matched guidance protocols into the system-prompt injection block."""
    blocks = [p["guidance"] for p in protocols if p.get("guidance")]
    if not blocks:
        return ""
    header = (
        "════════════════════════════════════════════════════════════════════════\n"
        "CATEGORY-SPECIFIC RULES  (apply to the problem types present in this patient)\n"
        "════════════════════════════════════════════════════════════════════════"
    )
    return header + "\n\n" + "\n\n".join(blocks)


def matched_guidance_ids(
    protocols: list[dict],
    problems: list[dict],
    prefetch_block: str = "",
    delta_categories: "set[str] | None" = None,
) -> list[str]:
    """Protocol IDs matched for guidance — for logging."""
    return [p["protocol_id"] for p in match_for_guidance(protocols, problems, prefetch_block, delta_categories)]


# ── Gate (permissive suppression) matching — unchanged mechanics ──────────────

def match_for_gate(protocols: list[dict], problems: list[dict]) -> dict[str, dict]:
    """
    For each problem, find the first matching protocol that has a permissive gate
    (indicated by the presence of a `scenarios` list or a `gate_question`).
    Returns {problem_name: protocol_doc}.  Same first-match-wins logic as before.
    """
    return _match_first_by_section(
        protocols, problems,
        lambda p: bool(p.get("scenarios") or p.get("gate_question")),
    )


def _load_stored_gates(
    cpmrn: str,
    encounter: int,
    problem_names: list[str],
    db: Any,
) -> dict[str, dict]:
    gates: dict[str, dict] = {}
    for name in problem_names:
        doc = db["patient_problems"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter, "problem_name": name},
            {"context_gate": 1},
        )
        if doc and doc.get("context_gate"):
            gates[name] = doc["context_gate"]
    return gates


def _build_gate_block(
    matched_protocols: dict[str, dict],
    stored_gates: dict[str, dict],
    snapshot_at: datetime,
) -> str:
    """Build the == CONTEXT GATE == prompt block — identical logic to the old inline version."""
    if not matched_protocols:
        return ""

    lines: list[str] = [
        "== CONTEXT GATE ==",
        "The following problems have monitoring protocols. For each:",
        "  1. Check if any invalidate_if trigger appears in new notes/data → verdict=permissive_ended",
        "  2. Check if the current value is inside the band_description → if not, verdict=permissive_breached",
        "  3. If all clear → verdict=permissive_active; carry forward in one sentence",
        "  Call tools only if uncertain about a trigger or band status.",
        "  In set_all_assessments, populate context_gate for EACH of these problems.",
        "",
    ]

    for problem_name, proto in matched_protocols.items():
        stored = stored_gates.get(problem_name, {})
        eligibility = stored.get("eligibility", "")
        valid_until = stored.get("valid_until")
        if isinstance(valid_until, datetime) and valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)

        lines.append(f"--- {problem_name} | Protocol: {proto['protocol_id']} ---")

        if eligibility == "ended":
            lines.append("  Gate: ENDED (eligibility permanently closed — normal alert rules apply)")
            lines.append("  Do NOT populate context_gate for this problem.")
            lines.append("")
            continue

        if stored and eligibility == "active":
            anchored_at = stored.get("anchored_at")
            anchored_str = (
                anchored_at.strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(anchored_at, datetime) else "unknown"
            )
            valid_str = (
                valid_until.strftime("%Y-%m-%d %H:%M UTC")
                if isinstance(valid_until, datetime) else "none"
            )
            hours_remaining = (
                f"{(valid_until - snapshot_at).total_seconds() / 3600:.1f}h remaining"
                if isinstance(valid_until, datetime) else "no time cap"
            )

            lines.append(f"  Stored gate (anchored {anchored_str}):")
            lines.append(f"    Eligibility: {eligibility}")
            lines.append(f"    Scenario:    {stored.get('scenario', 'none')}")
            lines.append(f"    Band:        {stored.get('band_description', '?')}")
            lines.append(f"    Valid until: {valid_str} ({hours_remaining})")
            lines.append(f"    Invalidate if: {stored.get('invalidate_if', [])}")
            lines.append(f"    Prior rationale: {stored.get('rationale', '(none)')}")
            lines.append("")
            lines.append("  CARRY-FORWARD INSTRUCTION:")
            lines.append("    If no invalidate_if trigger appears in new data AND value is in-band →")
            lines.append("    set verdict=permissive_active, confirm in one sentence (no tool calls needed).")
            lines.append("    If a trigger appears OR value is out-of-band → re-derive fully using tools.")
        else:
            lines.append(f"  Gate question: {proto.get('gate_question', '')}")
            lines.append("")
            lines.append("  Scenarios to rule in/out:")
            for s in proto.get("scenarios", []):
                lines.append(f"    [{s['name']}]")
                lines.append(f"      Band:         {s['band_description']}")
                lines.append(f"      Window:       {s['window']}")
                lines.append(f"      Invalidate if: {s['invalidate_if']}")
            lines.append("")
            lines.append("  If no scenario applies → verdict=no_permissive_context (normal alert rules apply).")
            lines.append("  If window closes/trigger fires → verdict=permissive_ended.")
            lines.append(f"  After window: {proto.get('escalation_target_after_window', '')}")

        lines.append("")

    return "\n".join(lines)


# ── Audit matching ─────────────────────────────────────────────────────────────

def match_for_audit(protocols: list[dict], problems: list[dict]) -> list[tuple[dict, dict]]:
    """
    Return (problem_doc, protocol_doc) pairs for problems that match a protocol
    carrying an `audit` section.  One problem matches at most one protocol
    (first-match-wins on applies_when).
    """
    matched = _match_first_by_section(protocols, problems, lambda p: bool(p.get("audit")))
    by_name = {p.get("name", ""): p for p in problems}
    return [(by_name[name], proto) for name, proto in matched.items() if name in by_name]


# ── Combined injection entry point ────────────────────────────────────────────

def build_injection(
    protocols: list[dict],
    problems: list[dict],
    prefetch_block: str,
    delta_categories: "set[str] | None",
    cpmrn: str,
    encounter: int,
    db: Any,
    snapshot_at: datetime,
) -> tuple[str, str, dict[str, dict]]:
    """
    Single call site for problem_tracker.

    Returns:
        guidance_block  — inject into system prompt (replaces _category_block)
        gate_block      — inject into user message (replaces the inline gate_block)
        matched_gate    — {problem_name: protocol_doc} for the gate protocols matched
                           this run, so callers can persist protocol identity
    """
    # ── Guidance ──────────────────────────────────────────────────────────────
    guidance_protocols = match_for_guidance(protocols, problems, prefetch_block, delta_categories)
    guidance_block = format_guidance_block(guidance_protocols)
    if guidance_protocols:
        logger.info(
            "protocol_engine: guidance — %d protocol(s) for %s enc=%d (%s)",
            len(guidance_protocols), cpmrn, encounter,
            ", ".join(p["protocol_id"] for p in guidance_protocols),
        )

    # ── Gate ──────────────────────────────────────────────────────────────────
    matched_gate = match_for_gate(protocols, problems)
    stored_gates: dict[str, dict] = {}
    if matched_gate:
        stored_gates = _load_stored_gates(cpmrn, encounter, list(matched_gate.keys()), db)
        _now_utc = datetime.now(timezone.utc)
        for pname, gate in stored_gates.items():
            vu = gate.get("valid_until")
            if isinstance(vu, datetime):
                if vu.tzinfo is None:
                    vu = vu.replace(tzinfo=timezone.utc)
                if _now_utc > vu and gate.get("eligibility") == "active":
                    gate["eligibility"] = "ended"
                    logger.info(
                        "protocol_engine: gate valid_until expired for '%s' %s enc=%d — eligibility ended",
                        pname, cpmrn, encounter,
                    )
    try:
        gate_block = _build_gate_block(matched_gate, stored_gates, snapshot_at)
    except Exception:
        logger.exception("protocol_engine: gate block build failed for %s enc=%d", cpmrn, encounter)
        gate_block = ""

    return guidance_block, gate_block, matched_gate
