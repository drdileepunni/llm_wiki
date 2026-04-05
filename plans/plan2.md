# LLM Wiki App — Full Build Plan for Claude Code

> Hand this entire document to Claude Code. Read it fully before writing a single line of code.
> Build phases in order. Do not skip ahead.

---

## What You're Building

A local web application that wraps the LLM Wiki system with a proper UI. It runs entirely on the user's machine alongside their existing `llm-wiki/` directory and Obsidian vault.

**Three functions:**
1. **Ingest** — upload a PDF/Markdown file, or paste a PubMed ID, and have it processed into the wiki automatically
2. **Chat** — ask questions against the wiki, get cited answers, optionally file them back
3. **Dashboard** — track token usage and cost per operation, with projections

**Obsidian remains the wiki browser.** This app does not render wiki pages — it writes them. Wiki links in chat responses open directly in Obsidian via deep links.

---

## Full Stack

| Layer | Choice | Reason |
|---|---|---|
| Backend | Python 3.11+ + FastAPI | Lightweight, async, easy file I/O |
| Frontend | React 18 + Vite + Tailwind CSS | Fast local dev, sleek UI |
| Database | SQLite via SQLAlchemy | Zero infra, local |
| LLM | Anthropic API (`claude-sonnet-4-5`) | Direct control over token counts |
| PDF extraction | PyMuPDF (`fitz`) | Best local PDF text extraction |
| PubMed | NCBI E-utilities REST API | Free, no key needed |
| Package mgmt | `uv` for Python, `npm` for frontend | |

---

## Repository Structure

Build the following structure inside the existing `llm-wiki/` directory:

```
llm-wiki/
├── CLAUDE.md                      ← existing, unchanged
├── raw/                           ← existing, unchanged
├── wiki/                          ← existing, unchanged
│
└── app/
    ├── README.md                  ← how to run the app
    ├── pyproject.toml             ← Python deps (uv)
    ├── .env.example               ← ANTHROPIC_API_KEY, WIKI_ROOT
    ├── .env                       ← gitignored
    │
    ├── backend/
    │   ├── main.py                ← FastAPI app, route registration, CORS
    │   ├── config.py              ← settings from .env, path resolution
    │   ├── database.py            ← SQLAlchemy setup, table definitions
    │   │
    │   ├── routers/
    │   │   ├── ingest.py          ← POST /ingest/file, POST /ingest/pubmed
    │   │   ├── chat.py            ← POST /chat
    │   │   └── dashboard.py       ← GET /dashboard/stats, GET /dashboard/log
    │   │
    │   └── services/
    │       ├── extractor.py       ← PDF → text, MD → text, PubMed → text
    │       ├── ingest_pipeline.py ← orchestrates LLM ingest call + file writes
    │       ├── chat_pipeline.py   ← orchestrates two-step LLM chat call
    │       └── token_tracker.py   ← logs every API call to SQLite
    │
    └── frontend/
        ├── index.html
        ├── vite.config.js
        ├── tailwind.config.js
        ├── package.json
        └── src/
            ├── main.jsx
            ├── App.jsx            ← routing: /ingest, /chat, /dashboard
            ├── api.js             ← all fetch calls to backend
            ├── components/
            │   ├── Layout.jsx     ← sidebar nav + main content area
            │   ├── NavLink.jsx
            │   └── CostBadge.jsx  ← reusable token cost display
            └── pages/
                ├── Ingest.jsx
                ├── Chat.jsx
                └── Dashboard.jsx
```

---

## Phase 1 — Project Scaffold

### 1.1 Python backend setup

Create `app/pyproject.toml`:
```toml
[project]
name = "llm-wiki-app"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.111.0",
    "uvicorn[standard]>=0.29.0",
    "anthropic>=0.28.0",
    "pymupdf>=1.24.0",
    "sqlalchemy>=2.0.0",
    "python-multipart>=0.0.9",
    "httpx>=0.27.0",
    "python-dotenv>=1.0.0",
]
```

Create `app/.env.example`:
```
ANTHROPIC_API_KEY=sk-ant-...
WIKI_ROOT=/absolute/path/to/llm-wiki
MODEL=claude-sonnet-4-5
```

### 1.2 Frontend scaffold

```bash
cd app
npm create vite@latest frontend -- --template react
cd frontend
npm install
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init -p
npm install react-router-dom
npm install @heroicons/react
npm install recharts
```

### 1.3 Vite proxy config

In `app/frontend/vite.config.js`, proxy all `/api` requests to the backend:
```js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000'
    }
  }
})
```

### 1.4 Tailwind config

In `tailwind.config.js`:
```js
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Playfair Display"', 'serif'],
        body: ['"DM Sans"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        ink: {
          950: '#0a0a0f',
          900: '#111118',
          800: '#1a1a24',
          700: '#252533',
          600: '#323244',
        },
        accent: {
          DEFAULT: '#7c6af7',
          dim: '#5a4fd4',
          glow: 'rgba(124, 106, 247, 0.15)',
        },
        surface: '#16161f',
        border: '#2a2a3a',
        muted: '#6b6b8a',
        success: '#4ade80',
        warning: '#fbbf24',
      },
    },
  },
  plugins: [],
}
```

Add to `index.html` head:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600&family=DM+Sans:wght@300;400;500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

**Design direction:** Dark, editorial, refined. Think a research journal crossed with a code editor. Deep ink backgrounds, a single violet accent, generous whitespace, Playfair Display for headings (gives it intellectual weight), DM Sans for body (clean, not generic). No gradients except subtle glows around the accent. The UI should feel like a serious tool for serious work.

---

## Phase 2 — Backend Core

### 2.1 `backend/config.py`

```python
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WIKI_ROOT = Path(os.environ["WIKI_ROOT"])
MODEL = os.getenv("MODEL", "claude-sonnet-4-5")

RAW_DIR = WIKI_ROOT / "raw"
WIKI_DIR = WIKI_ROOT / "wiki"
CLAUDE_MD = WIKI_ROOT / "CLAUDE.md"
DB_PATH = WIKI_ROOT / "app" / "wiki.db"

# Anthropic pricing (per million tokens)
PRICING = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
}
```

### 2.2 `backend/database.py`

```python
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from datetime import datetime
from .config import DB_PATH

engine = create_engine(f"sqlite:///{DB_PATH}")
Session = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class TokenLog(Base):
    __tablename__ = "token_log"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    operation = Column(String)       # 'ingest' | 'chat' | 'lint'
    source_name = Column(String)     # file name or query snippet
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    cost_usd = Column(Float)
    model = Column(String)

def init_db():
    Base.metadata.create_all(engine)
```

### 2.3 `backend/services/token_tracker.py`

```python
from ..database import Session, TokenLog
from ..config import PRICING, MODEL
from datetime import datetime

def calculate_cost(input_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    pricing = PRICING.get(model, PRICING["claude-sonnet-4-5"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

def log_call(operation: str, source_name: str, input_tokens: int, output_tokens: int, model: str = MODEL):
    cost = calculate_cost(input_tokens, output_tokens, model)
    with Session() as session:
        entry = TokenLog(
            timestamp=datetime.utcnow(),
            operation=operation,
            source_name=source_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            model=model,
        )
        session.add(entry)
        session.commit()
    return cost
```

### 2.4 `backend/services/extractor.py`

```python
import fitz  # PyMuPDF
import httpx
from pathlib import Path

def extract_pdf(path: Path) -> str:
    doc = fitz.open(path)
    return "\n\n".join(page.get_text() for page in doc)

def extract_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def extract_pubmed(pmid: str) -> dict:
    """Fetch abstract + metadata from NCBI E-utilities. Returns dict with title, authors, abstract, citation."""
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    
    # Fetch summary
    summary_url = f"{base}/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"
    abstract_url = f"{base}/efetch.fcgi?db=pubmed&id={pmid}&rettype=abstract&retmode=text"
    
    with httpx.Client() as client:
        summary = client.get(summary_url).json()
        abstract_text = client.get(abstract_url).text
    
    doc_summary = summary.get("result", {}).get(pmid, {})
    title = doc_summary.get("title", "Unknown Title")
    authors = ", ".join(a.get("name", "") for a in doc_summary.get("authors", [])[:5])
    pub_date = doc_summary.get("pubdate", "")
    journal = doc_summary.get("source", "")
    
    citation = f"{authors}. {title}. {journal}. {pub_date}. PMID: {pmid}"
    
    return {
        "title": title,
        "citation": citation,
        "text": abstract_text,
        "pmid": pmid,
    }

def extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path)
    return extract_markdown(path)
```

---

## Phase 3 — Ingest Pipeline

### 3.1 `backend/services/ingest_pipeline.py`

The ingest pipeline:
1. Reads `CLAUDE.md` as system prompt
2. Sends source text to Claude with structured output instructions
3. Parses file operations from response
4. Writes files to wiki/
5. Logs tokens

```python
import anthropic
import json
import re
from pathlib import Path
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

INGEST_INSTRUCTIONS = """
You are ingesting a new source into the wiki. Follow the ingest workflow from CLAUDE.md exactly.

After processing the source, output your file operations as a series of JSON blocks, one per line, in this exact format:
{"op": "write", "path": "wiki/sources/slug.md", "content": "...full markdown content..."}
{"op": "write", "path": "wiki/entities/entity-name.md", "content": "..."}
{"op": "append", "path": "wiki/log.md", "content": "\\n## [DATE] ingest | Source Title\\n..."}
{"op": "update", "path": "wiki/index.md", "section": "Sources", "content": "- [[Source Title]] — one line summary"}

Rules:
- Output ONLY the JSON blocks after a line that says exactly: ===FILE_OPS===
- Before that line, write a brief summary of what you found in the source (2-3 sentences)
- Every path must be relative to the wiki root
- The write op overwrites the file entirely. The append op adds to the end. 
- Always touch: the source page, at least 2 entity/concept pages, log.md, index.md
- Use today's date for log entries
"""

def run_ingest(source_text: str, source_name: str, citation: str = "") -> dict:
    system_prompt = CLAUDE_MD.read_text()
    
    user_message = f"""
Source to ingest: {source_name}
Citation: {citation}

---SOURCE TEXT START---
{source_text[:50000]}  
---SOURCE TEXT END---

{INGEST_INSTRUCTIONS}
"""
    
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )
    
    # Log tokens
    cost = log_call(
        operation="ingest",
        source_name=source_name,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
    
    full_text = response.content[0].text
    
    # Split on the file ops marker
    parts = full_text.split("===FILE_OPS===")
    summary = parts[0].strip()
    files_written = []
    
    if len(parts) > 1:
        ops_text = parts[1].strip()
        for line in ops_text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                op = json.loads(line)
                execute_file_op(op)
                files_written.append(op["path"])
            except (json.JSONDecodeError, KeyError):
                continue
    
    return {
        "summary": summary,
        "files_written": files_written,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": cost,
    }

def execute_file_op(op: dict):
    path = WIKI_ROOT / op["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    
    if op["op"] == "write":
        path.write_text(op["content"], encoding="utf-8")
    elif op["op"] == "append":
        with open(path, "a", encoding="utf-8") as f:
            f.write(op["content"])
    elif op["op"] == "update":
        # Simple section update: find section header, insert after it
        if path.exists():
            text = path.read_text()
            section = op.get("section", "")
            new_line = op.get("content", "")
            if f"## {section}" in text:
                text = text.replace(f"## {section}\n(none yet)", f"## {section}\n{new_line}")
                text = text.replace(f"## {section}\n", f"## {section}\n{new_line}\n", 1)
            path.write_text(text)
```

### 3.2 `backend/routers/ingest.py`

```python
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pathlib import Path
import tempfile
import shutil
from ..services.extractor import extract_text, extract_pubmed
from ..services.ingest_pipeline import run_ingest
from ..config import RAW_DIR

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

@router.post("/file")
async def ingest_file(file: UploadFile = File(...)):
    # Save to raw/
    dest = RAW_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    
    # Extract text
    text = extract_text(dest)
    
    # Run pipeline
    result = run_ingest(
        source_text=text,
        source_name=file.filename,
        citation=f"File: {file.filename}"
    )
    
    return result

@router.post("/pubmed")
async def ingest_pubmed(pmid: str = Form(...)):
    data = extract_pubmed(pmid)
    
    result = run_ingest(
        source_text=data["text"],
        source_name=data["title"],
        citation=data["citation"],
    )
    
    return result
```

---

## Phase 4 — Chat Pipeline

### 4.1 `backend/services/chat_pipeline.py`

Two-step pipeline. First call identifies relevant pages from index. Second call reads those pages and answers.

```python
import anthropic
from pathlib import Path
from ..config import ANTHROPIC_API_KEY, MODEL, CLAUDE_MD, WIKI_DIR, WIKI_ROOT
from .token_tracker import log_call

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def run_chat(question: str) -> dict:
    system_prompt = CLAUDE_MD.read_text()
    index_content = (WIKI_DIR / "index.md").read_text()
    
    total_input = 0
    total_output = 0
    
    # Step 1: identify relevant pages
    step1 = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""Here is the wiki index:\n\n{index_content}\n\nQuestion: {question}\n\nList the 3-5 most relevant wiki page paths to answer this question. Output only a JSON array of paths, e.g. ["wiki/entities/foo.md", "wiki/concepts/bar.md"]. Nothing else."""
        }]
    )
    
    total_input += step1.usage.input_tokens
    total_output += step1.usage.output_tokens
    
    # Parse page list
    import json, re
    raw = step1.content[0].text.strip()
    match = re.search(r'\[.*?\]', raw, re.DOTALL)
    pages = json.loads(match.group()) if match else []
    
    # Read pages
    page_contents = []
    for page_path in pages:
        full_path = WIKI_ROOT / page_path
        if full_path.exists():
            content = full_path.read_text()
            page_contents.append(f"### {page_path}\n\n{content}")
    
    context = "\n\n---\n\n".join(page_contents)
    
    # Step 2: answer
    step2 = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""Using the following wiki pages, answer this question: {question}

Use [[wiki links]] when referencing entities or concepts that have pages.
Cite your sources inline using the page path in parentheses.
Be precise and direct. If the wiki doesn't have enough information to answer, say so clearly.

Wiki pages:
{context}"""
        }]
    )
    
    total_input += step2.usage.input_tokens
    total_output += step2.usage.output_tokens
    
    # Log combined cost
    cost = log_call(
        operation="chat",
        source_name=question[:80],
        input_tokens=total_input,
        output_tokens=total_output,
    )
    
    answer = step2.content[0].text
    
    # Extract wiki links for Obsidian deep link rendering
    import re as re2
    wiki_links = re2.findall(r'\[\[(.+?)\]\]', answer)
    
    return {
        "answer": answer,
        "pages_consulted": pages,
        "wiki_links": list(set(wiki_links)),
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": cost,
    }

def file_answer(question: str, answer: str) -> str:
    """Write a query answer back to the wiki."""
    from datetime import date
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', question[:50].lower()).strip('-')
    filename = f"{date.today().isoformat()}-{slug}.md"
    path = WIKI_DIR / "queries" / filename
    path.parent.mkdir(exist_ok=True)
    
    content = f"""---
title: "{question}"
type: query
created: {date.today().isoformat()}
---

# {question}

{answer}
"""
    path.write_text(content)
    
    # Update index
    index_path = WIKI_DIR / "index.md"
    if index_path.exists():
        text = index_path.read_text()
        entry = f"- [[{question[:60]}]] — {date.today().isoformat()}"
        text = text.replace("## Queries\n(none yet)", f"## Queries\n{entry}")
        index_path.write_text(text)
    
    return filename
```

### 4.2 `backend/routers/chat.py`

```python
from fastapi import APIRouter
from pydantic import BaseModel
from ..services.chat_pipeline import run_chat, file_answer

router = APIRouter(prefix="/api/chat", tags=["chat"])

class ChatRequest(BaseModel):
    question: str

class FileRequest(BaseModel):
    question: str
    answer: str

@router.post("/")
async def chat(req: ChatRequest):
    return run_chat(req.question)

@router.post("/file")
async def file_query(req: FileRequest):
    filename = file_answer(req.question, req.answer)
    return {"filed": True, "filename": filename}
```

---

## Phase 5 — Dashboard

### 5.1 `backend/routers/dashboard.py`

```python
from fastapi import APIRouter
from sqlalchemy import func, desc
from ..database import Session, TokenLog
from ..config import PRICING, MODEL

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

@router.get("/stats")
def get_stats():
    with Session() as session:
        rows = session.query(TokenLog).all()
        
        total_cost = sum(r.cost_usd for r in rows)
        total_input = sum(r.input_tokens for r in rows)
        total_output = sum(r.output_tokens for r in rows)
        
        ingests = [r for r in rows if r.operation == "ingest"]
        chats = [r for r in rows if r.operation == "chat"]
        
        avg_ingest_cost = sum(r.cost_usd for r in ingests) / len(ingests) if ingests else 0
        avg_chat_cost = sum(r.cost_usd for r in chats) / len(chats) if chats else 0
        
        # Projections
        proj_100_sources = avg_ingest_cost * 100
        proj_monthly_chat = avg_chat_cost * 30  # assume 30 queries/month
        
        return {
            "total_cost_usd": round(total_cost, 4),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_operations": len(rows),
            "ingest_count": len(ingests),
            "chat_count": len(chats),
            "avg_ingest_cost_usd": round(avg_ingest_cost, 4),
            "avg_chat_cost_usd": round(avg_chat_cost, 4),
            "projection_100_sources_usd": round(proj_100_sources, 2),
            "projection_monthly_chat_usd": round(proj_monthly_chat, 2),
        }

@router.get("/log")
def get_log(limit: int = 50):
    with Session() as session:
        rows = session.query(TokenLog).order_by(desc(TokenLog.timestamp)).limit(limit).all()
        return [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat(),
                "operation": r.operation,
                "source_name": r.source_name,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(r.cost_usd, 4),
                "model": r.model,
            }
            for r in rows
        ]

@router.get("/timeseries")
def get_timeseries():
    """Cumulative cost over time for chart."""
    with Session() as session:
        rows = session.query(TokenLog).order_by(TokenLog.timestamp).all()
        cumulative = 0
        result = []
        for r in rows:
            cumulative += r.cost_usd
            result.append({
                "date": r.timestamp.strftime("%Y-%m-%d"),
                "cumulative_cost": round(cumulative, 4),
                "operation": r.operation,
            })
        return result
```

### 5.2 `backend/main.py`

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .database import init_db
from .routers import ingest, chat, dashboard

app = FastAPI(title="LLM Wiki")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(chat.router)
app.include_router(dashboard.router)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"status": "ok"}
```

---

## Phase 6 — Frontend

### Design Language

- **Background:** `#0a0a0f` (near-black with blue undertone)
- **Surface:** `#16161f` (cards, panels)
- **Border:** `#2a2a3a` (subtle separators)
- **Accent:** `#7c6af7` (violet — used sparingly: active states, CTAs, highlights)
- **Text:** `#e8e8f0` primary, `#6b6b8a` muted
- **Font display:** Playfair Display (headings, logo)
- **Font body:** DM Sans (all UI text)
- **Font mono:** JetBrains Mono (token counts, paths, code)
- **Motion:** Subtle fade-ins on page load (150ms), hover transitions (100ms). Nothing flashy.

### 6.1 `src/App.jsx`

```jsx
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Ingest from './pages/Ingest'
import Chat from './pages/Chat'
import Dashboard from './pages/Dashboard'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Navigate to="/ingest" replace />} />
          <Route path="ingest" element={<Ingest />} />
          <Route path="chat" element={<Chat />} />
          <Route path="dashboard" element={<Dashboard />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
```

### 6.2 `src/components/Layout.jsx`

Sidebar on the left (fixed, 220px wide). Main content area on the right.

Sidebar contains:
- App name "llm·wiki" in Playfair Display, accent color, top left
- Nav links: Ingest, Chat, Dashboard (with Heroicons)
- At the bottom: total spend today in monospace (fetched from `/api/dashboard/stats`)

Active nav link: left border in accent color, background slightly lighter.

### 6.3 `src/pages/Ingest.jsx`

Layout: two-column. Left = input. Right = result.

**Left panel:**

Tab switcher: "Upload File" | "PubMed ID"

*Upload File tab:*
- Drag-and-drop zone (dashed border, accent on hover)
- Accepted: `.pdf`, `.md`, `.txt`
- Shows filename once dropped
- "Ingest" button (accent background)
- Loading state: spinner + "Processing with Claude..."

*PubMed ID tab:*
- Single text input: "Enter PMID e.g. 34567890"
- "Fetch & Ingest" button

**Right panel** (shown after ingest completes):

- "Summary" — the 2-3 sentence Claude summary in a card
- "Files Written" — list of paths in monospace, each one a small tag
- Cost badge: input tokens / output tokens / $X.XXXX

### 6.4 `src/pages/Chat.jsx`

Full-height chat layout. Message list scrolls. Input fixed at bottom.

Message bubbles:
- User: right-aligned, accent background, DM Sans
- Assistant: left-aligned, surface background, left border in accent (2px)

Inside assistant messages:
- `[[wiki links]]` rendered as clickable badges that open `obsidian://open?vault=llm-wiki&file=wiki/entities/entity-name` in a new tab
- Cost badge shown below each assistant message (small, muted)

Below each assistant message:
- "File this answer ↗" button — calls `/api/chat/file`, shows confirmation "Filed to wiki/queries/..."

Input area:
- Full-width textarea (grows with content, max 4 rows)
- Send on Enter, Shift+Enter for newline
- Disabled during loading

### 6.5 `src/pages/Dashboard.jsx`

Three sections:

**Section 1 — Summary Cards** (4 cards in a row)
- Total Spend
- Ingests (count + avg cost each)
- Chats (count + avg cost each)  
- Projection: 100 sources cost

**Section 2 — Cumulative Cost Chart**
- Recharts LineChart
- X axis: date, Y axis: cumulative cost USD
- Accent color line, no dots, smooth curve
- Tooltip shows date + cost

**Section 3 — Operation Log**
- Table: Timestamp | Operation | Source | Tokens In | Tokens Out | Cost
- Operation column: colored badge (ingest = violet, chat = green)
- Monospace for all numbers
- Last 50 entries, newest first

### 6.6 `src/api.js`

```js
const BASE = '/api'

export async function ingestFile(file) {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch(`${BASE}/ingest/file`, { method: 'POST', body: form })
  return res.json()
}

export async function ingestPubmed(pmid) {
  const form = new FormData()
  form.append('pmid', pmid)
  const res = await fetch(`${BASE}/ingest/pubmed`, { method: 'POST', body: form })
  return res.json()
}

export async function sendChat(question) {
  const res = await fetch(`${BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  return res.json()
}

export async function fileAnswer(question, answer) {
  const res = await fetch(`${BASE}/chat/file`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, answer }),
  })
  return res.json()
}

export async function getStats() {
  return fetch(`${BASE}/dashboard/stats`).then(r => r.json())
}

export async function getLog() {
  return fetch(`${BASE}/dashboard/log`).then(r => r.json())
}

export async function getTimeseries() {
  return fetch(`${BASE}/dashboard/timeseries`).then(r => r.json())
}
```

---

## Phase 7 — Obsidian Deep Links

Wiki links in chat responses should open in Obsidian. The format is:

```
obsidian://open?vault=VAULT_NAME&file=PATH_WITHIN_VAULT
```

The vault name is the folder name of the Obsidian vault (i.e. `llm-wiki`).
The file path is relative to the vault root, without extension.

Example: `[[MPO-ANCA Vasculitis]]` → `obsidian://open?vault=llm-wiki&file=wiki/entities/mpo-anca-vasculitis`

In the Chat component, after receiving the answer, parse all `[[...]]` occurrences and replace them with clickable `<a>` tags pointing to the Obsidian deep link. The vault name should be read from an env var `VITE_VAULT_NAME` set in `frontend/.env`.

---

## Phase 8 — README and Run Instructions

Write `app/README.md` with the following:

```markdown
# LLM Wiki App

## Prerequisites
- Python 3.11+
- Node.js 18+
- uv (`pip install uv`)
- An Anthropic API key
- Obsidian installed with the llm-wiki vault open

## Setup

1. Copy `.env.example` to `.env` and fill in your API key and wiki root path
2. Install Python deps: `uv sync`
3. Install frontend deps: `cd frontend && npm install`

## Run

Terminal 1 — Backend:
```bash
cd app
uv run uvicorn backend.main:app --reload --port 8000
```

Terminal 2 — Frontend:
```bash
cd app/frontend
npm run dev
```

Open http://localhost:5173

## Usage

- **Ingest tab**: Drop a PDF or enter a PMID → wiki updates automatically → review in Obsidian
- **Chat tab**: Ask anything → click [[wiki links]] to open pages in Obsidian → "File this answer" to save good answers
- **Dashboard**: Track token costs, see projections
```

---

## Phase 9 — Smoke Test

Once everything is built:

1. Start both servers
2. Open http://localhost:5173
3. Go to Ingest → upload `raw/test-source.md` (any markdown file)
4. Verify:
   - Summary appears in right panel
   - At least 3 files listed under "Files Written"
   - Cost badge shows non-zero numbers
   - Files actually exist in `wiki/` on disk
5. Go to Chat → ask "What sources have been ingested?"
   - Should return an answer referencing the test source
   - Cost badge appears
6. Click "File this answer" → confirm it appears in `wiki/queries/`
7. Go to Dashboard → verify the 2 operations (1 ingest, 1 chat) appear in the log

If all 7 pass, the app is working end to end.

---

## Build Order Summary

Execute in this exact order:

1. Directory scaffold + `pyproject.toml` + frontend Vite init
2. Tailwind + font config
3. `config.py` + `database.py` + `token_tracker.py`
4. `extractor.py`
5. `ingest_pipeline.py` + `routers/ingest.py`
6. `chat_pipeline.py` + `routers/chat.py`
7. `routers/dashboard.py`
8. `main.py`
9. Frontend: `App.jsx` + `Layout.jsx` + `api.js`
10. Frontend: `Ingest.jsx`
11. Frontend: `Chat.jsx` (with Obsidian deep link rendering)
12. Frontend: `Dashboard.jsx`
13. `README.md`
14. Smoke test

After smoke test passes, report back with: "App is running. All smoke tests passed. Ready for first real ingest."