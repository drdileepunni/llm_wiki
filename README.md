# LLM Wiki — Personal Knowledge Base

A system where **you source, the LLM writes**. Every document you ingest builds a persistent, interlinked wiki. Knowledge compounds.

Based on [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

---

## Quick Start

### 1. Set Up Obsidian (10 minutes)
Follow [`OBSIDIAN_SETUP.md`](./OBSIDIAN_SETUP.md)

### 2. Ingest Your First Source
Tell Claude Code: `"Ingest raw/[filename]"`

The system will:
- Extract key entities and concepts
- Create/update wiki pages
- Cross-link everything
- Log the activity

### 3. Ask Questions
Ask anything naturally. Claude will:
- Search the wiki
- Synthesize an answer
- Offer to file it for future reference

---

## Directory Structure

```
llm-wiki/
├── CLAUDE.md                 ← LLM operating instructions (read this first)
├── raw/                      ← Your source documents (immutable)
│   └── assets/               ← Clipped images from articles
├── wiki/                     ← Knowledge base
│   ├── index.md              ← Catalog of all pages
│   ├── log.md                ← Activity log (append-only)
│   ├── overview.md           ← High-level synthesis
│   ├── sources/              ← One page per ingested source
│   ├── entities/             ← Named things (diseases, drugs, trials, people, etc)
│   ├── concepts/             ← Ideas and mechanisms
│   └── queries/              ← Filed answers to interesting questions
└── tools/
    └── search.sh             ← CLI search: ./tools/search.sh "keywords"
```

---

## Commands

| What you want | What you say to Claude Code |
|---|---|
| Add a source | `"Ingest raw/[filename]"` |
| Ask a question | Ask naturally |
| Save a good answer | `"File that answer"` |
| Health check | `"Lint the wiki"` |
| Search CLI | `./tools/search.sh "keyword"` |
| See the wiki shape | Open Obsidian, Graph View |

---

## Status

✅ **Setup Complete** — Ready for first ingest
📊 **Current state**: Smoke test passed (1 source, 3 wiki pages)
🔗 **Linked pages**: 6
📝 **Conventions**: Established in [`CLAUDE.md`](./CLAUDE.md)

---

## Domain

Rheumatology / Vasculitis Research

**Key entity types:**
- Diseases (MPO-AAV, GPA, MPA, EGPA, SLE, APS)
- Drugs (rituximab, avacopan, cyclophosphamide)
- Clinical trials (RAVE, RITUXVAS, ADVOCATE)
- Biomarkers (MPO-ANCA, PR3-ANCA, complement)
- Outcome measures (BVAS, VDI, SLEDAI)
- Guidelines (ACR/EULAR vasculitis guidelines)

---

## Next: Ready to Ingest

Once Obsidian is set up, come back and share your first source!
