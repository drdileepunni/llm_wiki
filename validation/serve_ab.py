#!/usr/bin/env python3
"""
Serve the A/B reviewer app and (optionally) launch ngrok.

Usage:
  python serve_ab.py                              # auto-find latest ab_results_*.json
  python serve_ab.py --results ab_results_xyz.json
  python serve_ab.py --port 8081 --ngrok
  python serve_ab.py --list                       # list available result files

Reviewer ratings are saved to results/ab_reviewer_<name>_<ts>.json on POST /submit_ab.
The ab_results JSON is served on GET /ab_data for the frontend.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

VALIDATION_DIR = Path(__file__).parent
REPO_ROOT      = VALIDATION_DIR.parent
DATA_DIR       = VALIDATION_DIR / "data"
RESULTS_DIR    = VALIDATION_DIR / "results"
TRACES_DIR     = REPO_ROOT / "app" / "traces"

_ab_data: dict = {}   # loaded once at startup


# ── Trace lookup ───────────────────────────────────────────────────────────
def _find_chat_trace(run_id: str) -> dict | None:
    """Search chat_*.jsonl files for a matching run_id."""
    if not TRACES_DIR.exists():
        return None
    for f in sorted(TRACES_DIR.glob("chat_*.jsonl"), reverse=True):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip() or run_id not in line:
                    continue
                d = json.loads(line)
                if d.get("run_id") == run_id:
                    return d
        except Exception:
            continue
    return None


def _find_chat_trace_by_content(recommendations: list) -> dict | None:
    """Fallback: match a chat trace by comparing its final.immediate_next_steps
    to the order_gen recommendations list. Used when parent_run_id is missing."""
    if not recommendations or not TRACES_DIR.exists():
        return None
    recs_set = tuple(recommendations)
    for f in sorted(TRACES_DIR.glob("chat_*.jsonl"), reverse=True):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                steps = (d.get("final") or {}).get("immediate_next_steps", [])
                if tuple(steps) == recs_set:
                    return d
        except Exception:
            continue
    return None


def _find_trace(run_id: str) -> dict:
    """Search order_gen_*.jsonl files for a matching run_id."""
    if not TRACES_DIR.exists():
        return {"found": False, "run_id": run_id, "error": "traces dir not found"}
    for f in sorted(TRACES_DIR.glob("order_gen_*.jsonl"), reverse=True):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip() or run_id not in line:
                    continue
                d = json.loads(line)
                if d.get("run_id") == run_id:
                    parent_run_id = d.get("parent_run_id")
                    if parent_run_id:
                        chat_trace = _find_chat_trace(parent_run_id)
                    else:
                        chat_trace = _find_chat_trace_by_content(d.get("recommendations", []))
                    return {
                        "found":             True,
                        "run_id":            run_id,
                        "weight_resolution": d.get("weight_resolution"),
                        "recommendations":   d.get("recommendations", []),
                        "phase0":            d.get("phase0"),
                        "phase1":            d.get("phase1"),
                        "chat_trace":        chat_trace,
                    }
        except Exception:
            continue
    return {"found": False, "run_id": run_id}


def _find_latest_results() -> Path | None:
    files = sorted(DATA_DIR.glob("ab_results_*.json"), reverse=True)
    return files[0] if files else None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VALIDATION_DIR), **kwargs)

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ── GET /ab_data — serve the loaded results JSON ──────────────────────
    def do_GET(self):
        if self.path == "/ab_data":
            self._send_json(_ab_data)
            return
        # ── GET /why?run_id=<uuid> — order provenance ─────────────────────
        if self.path.startswith("/why"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            run_id = (params.get("run_id", [""])[0]).strip()
            if not run_id:
                self._send_json({"error": "run_id required"}, 400)
                return
            self._send_json(_find_trace(run_id))
            return
        # Redirect bare / to the reviewer
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/ab_reviewer.html")
            self.end_headers()
            return
        # Silence favicon — browser always requests it, we don't have one
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    # ── POST /submit_ab — save reviewer rating file ───────────────────────
    def do_POST(self):
        if self.path != "/submit_ab":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)

        try:
            data = json.loads(body)
        except Exception:
            self.send_error(400, "Invalid JSON")
            return

        name = data.get("reviewer_name", "unknown").replace(" ", "_").lower()
        ts   = int(time.time())
        RESULTS_DIR.mkdir(exist_ok=True)
        out  = RESULTS_DIR / f"ab_reviewer_{name}_{ts}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"  [submit] saved → {out.name}  ({data.get('n_cases',0)} cases)")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        # Convert all args to str so HTTPStatus enums (Python 3.14) don't cause TypeError
        str_args = [str(a) for a in args]
        if "POST" in (str_args[0] if str_args else "") or (str_args[1] if len(str_args) > 1 else "").startswith("4"):
            print(f"  [{self.address_string()}] {fmt % args}")


def start_ngrok(port: int):
    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        import urllib.request
        try:
            with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=5) as r:
                tunnels = json.loads(r.read())["tunnels"]
                for t in tunnels:
                    if t.get("proto") == "https":
                        url = t["public_url"]
                        print(f"\n  ┌─────────────────────────────────────────")
                        print(f"  │  ngrok URL  →  {url}/ab_reviewer.html")
                        print(f"  │  Share with reviewers (blinded)")
                        print(f"  └─────────────────────────────────────────\n")
                        return proc
        except Exception:
            print("  ngrok started but could not read tunnel URL — check http://localhost:4040")
        return proc
    except FileNotFoundError:
        print("  ngrok not found — skipping. Install from https://ngrok.com/download")
        return None


def main():
    global _ab_data

    ap = argparse.ArgumentParser(description="Serve the A/B blinded reviewer UI")
    ap.add_argument("--results", default=None,
                    help="ab_results_*.json filename (or path). Default: latest in data/")
    ap.add_argument("--port",  type=int, default=8081)
    ap.add_argument("--ngrok", action="store_true")
    ap.add_argument("--list",  action="store_true", help="List available result files and exit")
    args = ap.parse_args()

    # ── List mode ─────────────────────────────────────────────────────────
    if args.list:
        files = sorted(DATA_DIR.glob("ab_results_*.json"), reverse=True)
        if not files:
            print("  No ab_results_*.json files found in data/")
        else:
            print(f"  {'File':<40}  Cases")
            for f in files:
                try:
                    d = json.loads(f.read_text())
                    n = d.get("n_cases", len(d.get("cases", [])))
                except Exception:
                    n = "?"
                print(f"  {f.name:<40}  {n}")
        sys.exit(0)

    # ── Resolve results file ───────────────────────────────────────────────
    if args.results:
        p = Path(args.results)
        if not p.is_absolute():
            p = DATA_DIR / p
    else:
        p = _find_latest_results()

    if p is None or not p.exists():
        print("  ✗ No ab_results file found.")
        print("  Run first:  python validation/run_ab_test.py")
        print("  Or specify: python serve_ab.py --results ab_results_<ts>.json")
        sys.exit(1)

    # ── Load data ─────────────────────────────────────────────────────────
    try:
        _ab_data = json.loads(p.read_text())
        n = _ab_data.get("n_cases", len(_ab_data.get("cases", [])))
        kb = _ab_data.get("kb", "?")
        arm_a = _ab_data.get("arm_a_config", {})
        arm_b = _ab_data.get("arm_b_config", {})
        print(f"\n  AB results   → {p.name}")
        print(f"  Cases        : {n}")
        print(f"  KB           : {kb}")
        print(f"  Arm A        : {arm_a.get('grounding_mode')}  model={arm_a.get('model')}")
        print(f"  Arm B        : {arm_b.get('grounding_mode')}  model={arm_b.get('model')}")
    except Exception as e:
        print(f"  Could not read results file: {e}")
        sys.exit(1)

    # ── Start server ──────────────────────────────────────────────────────
    server    = HTTPServer(("", args.port), Handler)
    ngrok_proc = None
    if args.ngrok:
        ngrok_proc = start_ngrok(args.port)

    local_url = f"http://localhost:{args.port}/ab_reviewer.html"
    print(f"\n  Local URL  →  {local_url}")
    print(f"  Results    →  {RESULTS_DIR}/")
    print(f"  Analyse:      python validation/compute_ab_winner.py")
    print(f"\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping…")
        server.shutdown()
        if ngrok_proc:
            ngrok_proc.terminate()


if __name__ == "__main__":
    main()
