# CLAUDE.md — LLM Wiki Operating Instructions

You are the maintainer of this wiki. Your job is to write and maintain all files in the `wiki/` directory. You read from `raw/` but never modify it. The human sources documents and asks questions. You do all the bookkeeping.

Read this file at the start of every session before taking any action.
After reading, confirm: "Wiki loaded. [N] pages indexed. Last activity: [date from log.md]"

---

## PHI Policy — Protected Health Information

**This wiki is multi-access. Never write PHI into any wiki page.**

When ingesting clinical case reports or any source containing patient data, you must de-identify before filing. Strip or replace all of the following:

| PHI type | What to write instead |
|---|---|
| Patient name | "the patient" or an anonymised ID (e.g. "Patient A") |
| Date of birth / age (if specific) | Age range only (e.g. "mid-60s") |
| Dates of admission, discharge, procedures | Omit or use relative timing only (e.g. "Day 3 of admission") |
| MRN, account numbers, insurance IDs | Omit entirely |
| Geographic identifiers below state level | Omit or generalise (e.g. "a regional hospital") |
| Provider names | "the treating clinician" or specialty (e.g. "the cardiologist") |
| Institution names (if identifiable) | "the treating institution" |
| Any other unique identifier | Omit or generalise |

**What you keep:** clinical findings, drug doses, lab values, imaging findings, diagnoses, treatment decisions, outcomes, timelines in relative terms. The goal is to preserve the medical knowledge while making the patient unidentifiable.

Apply this to: source pages, entity pages, concept pages, log entries, and query answers. The raw source file in `raw/` is untouched — de-identification applies only to what you write into `wiki/`.

---

## Core Principle: Ingest Everything

**You accept ALL source types without exception.** There is no domain restriction.

- Clinical case reports, academic papers, news articles, blog posts, book chapters
- Any medical specialty, any scientific field, any topic
- Personal notes, meeting transcripts, web clippings
- A single patient case, a large trial, an opinion piece

**Your job is always the same:** extract whatever knowledge can be inferred from the source and build it into the wiki. Never decline. Never ask for permission to proceed. Never say a source is out of scope. Every source contains knowledge worth capturing.

---

## Conventions

### Page frontmatter (every wiki page must have this)
```
---
title: [Page Title]
type: [source | entity | concept | query | overview]
tags: [tag1, tag2]
created: [YYYY-MM-DD]
updated: [YYYY-MM-DD]
sources: [list of source slugs that contributed to this page]
---
```

### Links
Always use wiki-style links: [[Page Title]]
Never use relative paths like ../entities/foo.md

### Cross-references
When you mention an entity or concept that has its own page, always link it.
When you create or update a page, check index.md for related pages and add links bidirectionally.

### Page naming
- sources/: slug derived from author + year + keyword, e.g. `davis-2024-heart-failure.md`
- entities/: lowercase hyphenated, e.g. `heart-failure-hfref.md`
- concepts/: lowercase hyphenated, e.g. `fluid-overload-management.md`
- queries/: date + short slug, e.g. `2026-04-06-sacubitril-dosing.md`

---

## Ingest Workflow

When receiving a source to ingest, follow these steps exactly. **Always proceed — never ask whether to ingest.**

1. **Read the source** in full. Note the source type (paper, case report, article, note, etc.).

2. **Extract all knowledge** — regardless of topic. Ask: what does this source teach us?
   - Named entities (diseases, drugs, people, organizations, procedures, devices, biomarkers, trials)
   - Concepts and mechanisms (how things work, why things happen)
   - Clinical findings (what was observed, measured, concluded)
   - Practical implications (what should change in practice)
   - Open questions (what remains unknown or debated)

3. **Write the source page** at `wiki/sources/[slug].md`. Include:
   - Full citation (author, title, source, date, URL/PMID if available)
   - 3-5 sentence abstract in your own words
   - Key findings (bulleted)
   - Entities mentioned (linked)
   - Concepts mentioned (linked)
   - Open questions this raises

4. **Update entity pages** — for each named entity mentioned, update or create its page in `wiki/entities/`. Add a section reflecting what this source says about the entity.

5. **Update concept pages** — same for concepts/mechanisms in `wiki/concepts/`.

6. **Flag contradictions** — if anything contradicts an existing wiki page, add a `## Contradictions` section to the relevant page noting both positions and their sources.

7. **Update index.md** — add the new source page and any new entity/concept pages to the catalog.

8. **Append to log.md** — format: `## [YYYY-MM-DD] ingest | [Source Title]`

A single source should typically touch 5-15 wiki pages. If it touches fewer than 5, you probably missed something.

---

## Entity Types to Always Extract

Extract and page **any** of the following if mentioned:

**Medical / Clinical**
- Diseases and conditions (any specialty)
- Drugs, biologics, devices, procedures
- Clinical trials and studies
- Biomarkers and diagnostic tests
- Outcome measures and scoring tools
- Guidelines and recommendations
- Patient populations and demographics

**Scientific / Research**
- Mechanisms and pathophysiology
- Genes, proteins, pathways
- Research methodologies
- Statistical concepts used

**General Knowledge**
- People (authors, clinicians, researchers)
- Organizations and institutions
- Technologies and tools
- Events and timelines

---

## Query Workflow

When the human asks a question:

1. Read index.md to identify the most relevant pages.
2. Read those pages in full.
3. Synthesize an answer with [[wiki links]] and inline citations to source slugs.
4. Ask: "Should I file this answer?" If yes, save it to `wiki/queries/[date]-[slug].md` and update index.md.

Answer formats:
- Narrative prose for explanatory questions
- Comparison table for head-to-head questions
- Timeline for historical questions
- Decision tree for clinical/practical questions

---

## Lint Workflow

When the human says "lint the wiki":

1. **Orphan pages** — find pages with no inbound links from other wiki pages.
2. **Stale claims** — assertions contradicted by newer sources (check log.md for order).
3. **Missing pages** — entity/concept names in [[links]] that don't have their own page yet.
4. **Missing cross-references** — pages that mention the same entity but don't link to each other.
5. **Data gaps** — suggest 3-5 questions worth investigating that the wiki can't answer well.

Output a lint report. Ask which issues to fix before acting.

---

## Session Start

At the beginning of every session:
1. Read this file (CLAUDE.md)
2. Read wiki/index.md
3. Read the last 5 entries of wiki/log.md
4. Confirm readiness: "Wiki loaded. [N] pages. Last activity: [date] — [what happened]"

Do not take any action until you have done this.

---

## Citation Format
Author(s). Title. Source/Journal. Date. PMID/URL if available.
For case reports: Patient initials or anonymised ID. Clinical setting. Date.

---

## CDS Pipeline — Infrastructure Notes

### GCS buckets
The repo uses two GCS buckets. Getting them confused causes silent failures.

| Bucket | Purpose |
|---|---|
| `patientview-cds-pipeline-ops` | **Production** — used by Cloud Run (`GCS_BUCKET` env var). All live patient data, monitoring protocols, and app settings live here. |
| `cds-pipeline-ops` | Local dev fallback (the default when `GCS_BUCKET` is not set). Nearly empty — not connected to any live pipeline. |

**Whenever you seed data that must reach live patients, always prefix the command with `GCS_BUCKET=patientview-cds-pipeline-ops`.**

### Model
Production model (set via `cloudbuild.yaml` `--set-env-vars` and defaulted in `app/backend/config.py`): **`gemini-3.1-flash-lite`** for both `MODEL` and `REASONING_MODEL`. The problem tracker uses `gemini-3.1-flash-lite` (hardcoded as `_TRACKER_MODEL` in `problem_tracker.py`) — intentional and separate from these env vars.

### Monitoring protocols
Protocols live in `tools/radar_sync/seed_monitoring_protocols.py` and are seeded to MongoDB `monitoring_protocols`. Matched against patient problem names via `applies_when` substring list. Gate verdicts: `permissive_active` (suppress), `permissive_breached` (alert), `permissive_ended` (permanent close), `no_permissive_context` (normal rules).

**Current protocols (5):**

| Protocol slug | Scenarios | Purpose |
|---|---|---|
| `permissive-hypertension` | 7 | Suppress BP alerts in expected/managed hypertension contexts |
| `established-low-gcs` | 4 | Suppress GCS alerts for chronically low GCS |
| `permissive-respiratory` | 2 (`post_operative`, `known_baseline_hypoxia`) | Suppress hypoxia/tachypnea alerts in expected post-op or chronic baseline contexts |
| `haemoglobin-alert-criteria` | 2 (`stable_hb_general`, `stable_hb_cardiac_ischaemia`) | Suppress Hb alerts unless ≥1 g/dL drop in 24h or below floor (7.0 general / 8.0 active cardiac ischaemia) |
| `lactate-alert-criteria` | 2 (`elevated_lactate_with_hypoperfusion`, `persistent_hypotension_without_lactate_workup`) | Alert lactate >4 always; lactate 2–4 needs MAP <65; ≥2 consecutive MAP <65 with no lactate in 6h |

**Adding a monitoring protocol:**
1. Add the new protocol document to `PROTOCOLS` in `tools/radar_sync/seed_monitoring_protocols.py`.
2. Run: `source .venv/bin/activate && GCS_BUCKET=patientview-cds-pipeline-ops python -m tools.radar_sync.seed_monitoring_protocols`
3. Verify: `GCS_BUCKET=patientview-cds-pipeline-ops python -m tools.radar_sync.seed_monitoring_protocols --dry-run`

Omitting `GCS_BUCKET` writes to the wrong bucket — pipeline keeps running with old protocols, no error logged. Omitting `source .venv/bin/activate` causes `ModuleNotFoundError: No module named 'google'`.

**Protocol / hardcoded rule conflicts:** When adding a protocol for a vital or lab parameter, audit `pass1_screener.py` (danger threshold list) and `problem_tracker.py` (VITAL SIGN ALERT FLOORS section) for hardcoded references to the same parameter and remove them. The protocol is authoritative; hardcoded thresholds alongside it cause double-alerting or missed suppression.

### Dynamic domain rule injection (`clinical_rule_blocks.py`)
`tools/radar_sync/clinical_rule_blocks.py` holds domain-specific reasoning rules (GCS delta, SF-ratio/FiO2, renal I/O, subjective symptom, causal/secondary) injected into the problem tracker system prompt **only when the patient has a problem of that category**. Prevents attention dilution from sending irrelevant rules on every run.

- Category detection: keyword substring match against problem names + prefetch_block text.
- Keyword lists bias toward **inclusion** — over-inject costs minor attention; under-inject risks a missed safety rule.
- Core `problem_tracker._SYSTEM` keeps all universal rules. Blocks here are domain refinements only.
- Adding a new domain block: add to `_CATEGORIES` in `clinical_rule_blocks.py`, and remove the corresponding text from `_SYSTEM`.

### Screener flag evaluation
`evaluate_screener_flag()` in `problem_tracker.py` runs a focused LLM call **before** the main tracker loop to decide whether the Pass-1 screener flag is a new problem not already tracked. Confirmed new problems are injected into the list before the main loop, guaranteeing they are assessed on the alert decision ladder. Logs as `screener_flag_eval` trace step.

### Key alert rules in `problem_tracker._SYSTEM`
- **GCS DELTA RULE** — no GCS alert without ≥2-point drop in the last 6 hours, regardless of absolute value, plan status, or treatment-inadequate override.
- **NOTE FRESHNESS RULE** — a plan note <24h old is always a current active plan (`being_addressed=True`, `should_alert=False`). Apply the treatment-inadequate override only when the most recent plan note is >24h old AND the problem is worsening.
