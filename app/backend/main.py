import logging
import sys
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from .database import init_db
from .routers import ingest, chat, dashboard, wiki

# ── Logging config ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("wiki")

app = FastAPI(title="LLM Wiki")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    log.info("→ %s %s", request.method, request.url.path)
    response = await call_next(request)
    log.info("← %s %s  [%d]", request.method, request.url.path, response.status_code)
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(chat.router)
app.include_router(dashboard.router)
app.include_router(wiki.router)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {"status": "ok"}
