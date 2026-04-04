# LLM Wiki — Build Plan for Claude Code

> Hand this entire document to Claude Code. It contains everything needed to scaffold and initialize the system.

---

## What You're Building

A personal knowledge base where **you source, the LLM writes**. Unlike RAG (which re-derives answers from scratch every time), this system incrementally builds a persistent, interlinked wiki of markdown files. Every source you ingest makes the wiki smarter. Every good answer gets filed back in. Knowledge compounds.

Reference: [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

---

## Phase 1 — Scaffold the Directory Structure

Create the following directory tree. Do not add any content yet — just the structure and placeholder files.

```
llm-wiki/
├── CLAUDE.md               ← LLM operating instructions (written in Phase 2)
├── raw/                    ← Source documents. Immutable. LLM reads, never writes.
│   └── assets/             ← Downloaded images from clipped articles
├── wiki/
│   ├── index.md            ← Master catalog of all wiki pages
│   ├── log.md              ← Append-only chronological activity log
│   ├── overview.md         ← High-level synthesis of the entire knowledge base
│   ├── sources/            ← One page per ingested source
│   ├── entities/           ← Named things: people, drugs, diseases, trials, organizations
│   ├── concepts/           ← Ideas, mechanisms, frameworks, controversies
│   └── queries/            ← Filed answers to queries worth keeping
└── tools/
    └── search.sh           ← Simple grep-based search helper (written in Phase 3)
```

Initialize this as a git repo:
```bash
git init
echo "raw/assets/" >> .gitignore
git add .
git commit -m "init: scaffold llm-wiki structure"
```

---

## Phase 2 — Write CLAUDE.md

This is the most important file. It is your operating manual — the LLM reads this at the start of every session. Write it with the following sections:

---

### 2.1 Identity and Role

```markdown
# CLAUDE.md — LLM Wiki Operating Instructions

You are the maintainer of this wiki. Your job is to write and maintain all files
in the `wiki/` directory. You read from `raw/` but never modify it. The human
sources documents and asks questions. You do all the bookkeeping.

Read this file at the start of every session before taking any action.
After reading, confirm: "Wiki loaded. [N] pages indexed. Last activity: [date from log.md]"
```

---

### 2.2 Wiki Conventions

```markdown
## Conventions

### Page frontmatter (every wiki page must have this)
---
title: [Page Title]
type: [source | entity | concept | query | overview]
tags: [tag1, tag2]
created: [YYYY-MM-DD]
updated: [YYYY-MM-DD]
sources: [list of source slugs that contributed to this page]
---

### Links
Always use wiki-style links: [[Page Title]]
Never use relative paths like ../entities/foo.md

### Cross-references
When you mention an entity or concept that has its own page, always link it.
When you create or update a page, check index.md for related pages and add links bidirectionally.

### Page naming
- sources/: slug derived from author + year + keyword, e.g. `rituximab-anca-2023.md`
- entities/: lowercase hyphenated, e.g. `mpo-anca-vasculitis.md`
- concepts/: lowercase hyphenated, e.g. `complement-activation.md`
- queries/: date + short slug, e.g. `2026-04-04-mpo-vs-pr3-prognosis.md`
```

---

### 2.3 Ingest Workflow

```markdown
## Ingest Workflow

When the human says "ingest [filename]", follow these steps exactly:

1. **Read the source** from raw/. If it is a PDF, extract text. Note any images to review separately.

2. **Discuss** — summarize the 3-5 key takeaways and ask the human: "Anything to emphasize or flag before I file this?"

3. **Write the source page** at wiki/sources/[slug].md. Include:
   - Full citation
   - 3-5 sentence abstract in your own words
   - Key findings (bulleted)
   - Entities mentioned (linked)
   - Concepts mentioned (linked)
   - Open questions this raises

4. **Update entity pages** — for each named entity mentioned, update or create its page in wiki/entities/. Add a section or sentence reflecting what this source says about the entity.

5. **Update concept pages** — same for concepts in wiki/concepts/.

6. **Flag contradictions** — if anything in this source contradicts an existing wiki page, add a `## Contradictions` section to the relevant page noting both positions and their sources.

7. **Update index.md** — add the new source page and any new entity/concept pages to the catalog.

8. **Append to log.md** — format: `## [YYYY-MM-DD] ingest | [Source Title]`

A single source should typically touch 5-15 wiki pages. If it touches fewer than 5, you probably missed something.
```

---

### 2.4 Query Workflow

```markdown
## Query Workflow

When the human asks a question:

1. Read index.md to identify the most relevant pages.
2. Read those pages in full.
3. Synthesize an answer with [[wiki links]] and inline citations to source slugs.
4. Ask: "Should I file this answer?" If yes, save it to wiki/queries/[date]-[slug].md and update index.md.

Answers can take different forms depending on the question:
- Narrative prose for explanatory questions
- Comparison table for head-to-head questions
- Timeline for historical questions
- Decision tree for clinical/practical questions
```

---

### 2.5 Lint Workflow

```markdown
## Lint Workflow

When the human says "lint the wiki", do the following:

1. **Orphan pages** — find pages in wiki/ with no inbound links from other wiki pages.
2. **Stale claims** — look for assertions that are contradicted by newer sources (check log.md for order).
3. **Missing pages** — find entity or concept names mentioned in [[links]] that don't have their own page yet. List them.
4. **Missing cross-references** — find pages that mention the same entity/concept but don't link to each other.
5. **Data gaps** — suggest 3-5 questions worth investigating that the current wiki can't answer well.

Output a lint report. Ask the human which issues to fix before acting.
```

---

### 2.6 Session Start Checklist

```markdown
## Session Start

At the beginning of every session:
1. Read this file (CLAUDE.md)
2. Read wiki/index.md
3. Read the last 5 entries of wiki/log.md
4. Confirm readiness: "Wiki loaded. [N] pages. Last activity: [date] — [what happened]"

Do not take any action until you have done this.
```

---

## Phase 3 — Initialize index.md and log.md

Write the following starter content:

**wiki/index.md**
```markdown
---
title: Wiki Index
updated: [today's date]
---

# Wiki Index

## Sources
(none yet)

## Entities
(none yet)

## Concepts
(none yet)

## Queries
(none yet)
```

**wiki/log.md**
```markdown
---
title: Activity Log
---

# Activity Log

## [today's date] init | Wiki initialized
Scaffolded directory structure. CLAUDE.md written. Ready for first ingest.
```

**wiki/overview.md**
```markdown
---
title: Overview
type: overview
updated: [today's date]
---

# Overview

*This page is maintained by the LLM and updated after every ingest and lint pass.*

## What this wiki covers
(to be filled after first few ingests)

## Current state of knowledge
(to be filled)

## Open questions
(to be filled)

## Key entities
(to be filled)

## Key concepts
(to be filled)
```

---

## Phase 4 — Write tools/search.sh

A simple shell script so Claude Code can search the wiki without embedding infrastructure:

```bash
#!/bin/bash
# Usage: ./tools/search.sh "query terms"
# Searches wiki/ directory for matching pages, ranked by match count

QUERY="$*"
WIKI_DIR="$(dirname "$0")/../wiki"

echo "Searching wiki for: $QUERY"
echo "---"

grep -ril "$QUERY" "$WIKI_DIR" | while read -r file; do
    count=$(grep -ic "$QUERY" "$file")
    echo "$count $file"
done | sort -rn | head -20 | while read -r count file; do
    title=$(grep "^title:" "$file" | head -1 | sed 's/title: //')
    echo "[$count matches] $title — $file"
done
```

Make it executable: `chmod +x tools/search.sh`

---

## Phase 5 — Smoke Test

Once the scaffold is complete, run the following test ingest to verify everything works:

1. Create a file `raw/test-source.md` with 2-3 paragraphs of text on any topic relevant to your domain.
2. Tell Claude Code: **"Ingest raw/test-source.md"**
3. Verify it:
   - Created `wiki/sources/[slug].md`
   - Created or updated at least 2 entity or concept pages
   - Updated `wiki/index.md`
   - Appended to `wiki/log.md`
   - Did NOT modify anything in `raw/`

If all five are true, the system is working.

---

## Phase 6 — Obsidian Setup (do this yourself, 10 minutes)

1. Open Obsidian → "Open folder as vault" → select `llm-wiki/`
2. Install plugins (Settings → Community Plugins):
   - **Dataview** — lets you query page frontmatter as a database
   - **Obsidian Git** — auto-commits on save
3. Settings → Files and links:
   - Set "Default location for new attachments" → `raw/assets/`
4. Install **Obsidian Web Clipper** browser extension — clips web articles to markdown directly into `raw/`
5. Use the Graph View (left sidebar) to see the shape of your wiki as it grows

---

## How to Use Day-to-Day

| What you want to do | What you say to Claude Code |
|---|---|
| Add a new paper/article | `"Ingest raw/[filename]"` |
| Ask a question | Just ask it naturally |
| Save a good answer | `"File that answer"` |
| Health check | `"Lint the wiki"` |
| See what's in the wiki | Open Obsidian, check Graph View |
| Find something | `./tools/search.sh "keyword"` |

---

## Suggested Domain Configuration (Rheumatology Research)

When you write your first real `CLAUDE.md`, add this domain-specific guidance:

```markdown
## Domain: Rheumatology / Vasculitis Research

### Key entity types to always extract and page:
- Diseases (e.g. MPO-AAV, GPA, MPA, EGPA, SLE, APS)
- Drugs / biologics (e.g. rituximab, avacopan, cyclophosphamide)
- Clinical trials (e.g. RAVE, RITUXVAS, ADVOCATE)
- Biomarkers (e.g. MPO-ANCA, PR3-ANCA, complement levels)
- Outcome measures (e.g. BVAS, VDI, SLEDAI)
- Guidelines (e.g. ACR/EULAR 2022 vasculitis guidelines)

### Key concept types:
- Pathophysiology mechanisms
- Treatment controversies
- Guideline gaps / open questions
- Subgroup differences (e.g. MPO vs PR3 prognosis)

### Citation format:
Author et al., Journal, Year. PMID: [if available]
```

---

## What Claude Code Should Do With This Document

1. Read the entire document
2. Execute Phases 1–4 in order
3. Run the Phase 5 smoke test
4. Report back with: directory tree created, files written, smoke test result
5. Ask: "What's the first real source you want to ingest?"