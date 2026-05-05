import logging
import sys
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from .database import init_db
from .routers import ingest, chat, dashboard, wiki, kbs, resolve, assess, clinical_assess, learn, order_gen, viva, logs, graph, vm

# ── Logging config ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("wiki")

app = FastAPI(title="LLM Wiki")


_POLL_PREFIXES = (
    "/api/viva/viva_",
    "/api/learn/jobs/",
    "/api/clinical-assess/jobs/",
    "/api/resolve/batch/",
    "/api/resolve/jobs/",
    "/health",
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    is_poll = request.method == "GET" and any(request.url.path.startswith(p) for p in _POLL_PREFIXES)
    if not is_poll:
        log.info("→ %s %s", request.method, request.url.path)
    response = await call_next(request)
    if not is_poll:
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
app.include_router(kbs.router)
app.include_router(resolve.router)
app.include_router(assess.router)
app.include_router(clinical_assess.router)
app.include_router(learn.router)
app.include_router(order_gen.router)
app.include_router(viva.router)
app.include_router(logs.router)
app.include_router(graph.router)
app.include_router(vm.router)

@app.on_event("startup")
def startup():
    init_db()
    _seed_canonical_registries()
    _attach_log_capture()

def _attach_log_capture():
    from .services.log_capture import capture_handler
    root = logging.getLogger()
    if capture_handler not in root.handlers:
        root.addHandler(capture_handler)
    log.info("Log capture handler attached")


def _seed_canonical_registries():
    from .config import list_kbs, get_kb
    from .services.canonical_registry import seed_registry_vectors
    for name in list_kbs():
        try:
            kb = get_kb(name)
            seed_registry_vectors(kb.wiki_dir)
        except Exception as exc:
            log.warning("Canonical registry seed failed for KB '%s': %s", name, exc)


@app.on_event("shutdown")
def shutdown():
    from .cancellation import shutdown_event
    shutdown_event.set()
    log.info("Shutdown event set — pipeline will stop at next checkpoint")

@app.get("/health")
def health():
    return {"status": "ok"}
