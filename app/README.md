# LLM Wiki App

A local web app that wraps the LLM Wiki with a proper UI for ingesting sources, chatting with your wiki, and tracking token costs.

## Prerequisites

- Python 3.11+
- Node.js 18+
- `uv` — install with `pip install uv`
- An Anthropic API key
- Obsidian installed with the `llm_wiki` vault open

## Setup

### 1. Configure environment

```bash
cd app
cp .env.example .env
```

Edit `.env` and set:
- `ANTHROPIC_API_KEY=sk-ant-...`
- `WIKI_ROOT=/Users/drdileepunni/github_/llm_wiki`

### 2. Install Python dependencies

```bash
cd app
uv sync
```

### 3. Install frontend dependencies

```bash
cd app/frontend
npm install
```

### 4. Configure Obsidian vault name (optional)

Edit `app/frontend/.env`:
```
VITE_VAULT_NAME=llm_wiki
```
This controls the `obsidian://` deep links in the Chat page.

---

## Run

Open **two terminals**:

**Terminal 1 — Backend:**
```bash
cd app
uv run uvicorn backend.main:app --reload --port 8000
```

**Terminal 2 — Frontend:**
```bash
cd app/frontend
npm run dev
```

Open **http://localhost:5173**

---

## Usage

| Tab | What it does |
|---|---|
| **Ingest** | Drop a PDF/Markdown or enter a PubMed ID → Claude processes it into wiki pages |
| **Chat** | Ask anything → cited answers with wiki links → optionally save to `wiki/queries/` |
| **Dashboard** | Track token usage, costs, projections, and operation history |

### Wiki Links in Chat
`[[Entity Name]]` badges in chat answers open directly in Obsidian via deep links.
Click them to jump straight to the relevant page in your vault.

### Filing Answers
After a good chat answer, click **"File this answer ↗"** to save it to `wiki/queries/` and update `index.md` automatically.

---

## Architecture

```
Backend (FastAPI, port 8000)
  /api/ingest/file    — upload PDF/MD → Claude → wiki pages written
  /api/ingest/pubmed  — PMID → NCBI fetch → Claude → wiki pages written
  /api/chat/          — question → 2-step Claude pipeline → cited answer
  /api/chat/file      — save answer to wiki/queries/
  /api/dashboard/stats, /log, /timeseries — SQLite token log

Frontend (Vite/React, port 5173)
  /ingest   — file upload + PubMed tab
  /chat     — chat UI with wiki link rendering
  /dashboard — cost tracking + charts
```

---

## Obsidian remains primary browser

This app **writes** wiki pages. Use Obsidian to **read** them.
The Graph View in Obsidian shows the growing link structure as you ingest more sources.
