#!/usr/bin/env python3
"""
Serve the reviewer app and (optionally) launch ngrok.

Usage:
  python serve.py               # serves on :8080, no ngrok
  python serve.py --ngrok       # serves on :8080 + opens ngrok tunnel
  python serve.py --port 9090   # custom port

Reviewer results are saved to results/reviewer_<name>.json on POST /submit.
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

VALIDATION_DIR = Path(__file__).parent


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VALIDATION_DIR), **kwargs)

    def do_POST(self):
        if self.path != "/submit":
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
        out  = VALIDATION_DIR / "results" / f"reviewer_{name}_{ts}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(data, indent=2))

        print(f"  [submit] saved → {out.name}")

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
        # Suppress static file noise; only log POSTs and errors
        if "POST" in (args[0] if args else "") or (args[1] if len(args) > 1 else "").startswith("4"):
            print(f"  [{self.address_string()}] {fmt % args}")


def start_ngrok(port: int):
    """Launch ngrok and print the public URL."""
    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)  # give ngrok time to connect

        # Query the ngrok local API for the tunnel URL
        import urllib.request
        try:
            with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=5) as r:
                tunnels = json.loads(r.read())["tunnels"]
                for t in tunnels:
                    if t.get("proto") == "https":
                        url = t["public_url"]
                        print(f"\n  ┌─────────────────────────────────────────")
                        print(f"  │  ngrok URL  →  {url}")
                        print(f"  │  Share this link with reviewers")
                        print(f"  └─────────────────────────────────────────\n")
                        return proc
        except Exception:
            print("  ngrok started but could not read tunnel URL.")
            print("  Check http://localhost:4040 for the public URL.")
        return proc
    except FileNotFoundError:
        print("  ngrok not found — skipping. Install from https://ngrok.com/download")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port",  type=int, default=8080)
    ap.add_argument("--ngrok", action="store_true", help="Launch ngrok tunnel")
    args = ap.parse_args()

    scenarios_path = VALIDATION_DIR / "data" / "scenarios.json"
    if not scenarios_path.exists():
        print("  ✗ data/scenarios.json not found.")
        print("  Run first:  python export_scenarios.py --kb <kb_name> --n 3")
        sys.exit(1)

    # Verify scenarios loaded OK
    try:
        meta = json.loads(scenarios_path.read_text())
        n = meta.get("n_scenarios", 0)
        print(f"\n  Scenarios loaded: {n} scenario(s)  (kb: {meta.get('kb','')})")
    except Exception as e:
        print(f"  Could not read scenarios.json: {e}")
        sys.exit(1)

    server = HTTPServer(("", args.port), Handler)

    ngrok_proc = None
    if args.ngrok:
        ngrok_proc = start_ngrok(args.port)

    local_url = f"http://localhost:{args.port}/reviewer.html"
    print(f"\n  Local URL  →  {local_url}")
    print(f"  Results    →  {VALIDATION_DIR / 'results'}/")
    print(f"\n  Press Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping server…")
        server.shutdown()
        if ngrok_proc:
            ngrok_proc.terminate()


if __name__ == "__main__":
    main()
