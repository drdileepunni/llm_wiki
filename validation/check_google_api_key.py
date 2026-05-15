#!/usr/bin/env python3
"""
Quick Google API key validator.
Tries a minimal generateContent call across several models and reports what works.

Usage:
  python validation/check_google_api_key.py
  python validation/check_google_api_key.py --key AIzaSy...
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Load .env so GOOGLE_API_KEY is available without passing --key
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / "app" / ".env")
except ImportError:
    pass

MODELS_TO_TRY = [
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
]

PING_BODY = json.dumps({
    "contents": [{"parts": [{"text": "Reply with just the word PONG."}]}],
    "generationConfig": {"maxOutputTokens": 5},
})


def check_key(api_key: str) -> None:
    try:
        import httpx
    except ImportError:
        print("httpx not installed — run: pip install httpx")
        sys.exit(1)

    base = "https://generativelanguage.googleapis.com/v1beta/models"

    # ── 1. List models (cheapest possible check) ──────────────────────────────
    print(f"\n  API key : {api_key[:12]}…{api_key[-4:]}")
    print(f"  Testing key validity via models list endpoint…")
    r = httpx.get(f"{base}?key={api_key}", timeout=10)
    if r.status_code != 200:
        err = r.json().get("error", {})
        print(f"\n  ✗ Key rejected: {err.get('message', r.text[:200])}")
        print(f"    Status: {r.status_code}  Reason: {err.get('status', '?')}\n")
        sys.exit(1)

    all_models = [m["name"].split("/")[-1] for m in r.json().get("models", [])]
    print(f"  ✓ Key is valid  ({len(all_models)} models available)\n")

    # ── 2. Check which of our target models are listed ────────────────────────
    print(f"  Checking target models:")
    for model in MODELS_TO_TRY:
        listed = model in all_models
        print(f"    {'✓' if listed else '✗'} {model}  {'(listed)' if listed else '(NOT in model list)'}")

    # ── 3. Try a live generateContent call on each listed model ──────────────
    print(f"\n  Running ping call on each listed model:")
    any_worked = False
    for model in MODELS_TO_TRY:
        if model not in all_models:
            continue
        url = f"{base}/{model}:generateContent?key={api_key}"
        try:
            r2 = httpx.post(url, content=PING_BODY,
                            headers={"Content-Type": "application/json"}, timeout=20)
            if r2.status_code == 200:
                text = (r2.json()
                        .get("candidates", [{}])[0]
                        .get("content", {})
                        .get("parts", [{}])[0]
                        .get("text", "").strip())
                print(f"    ✓ {model}  →  response: {text!r}")
                any_worked = True
            else:
                err = r2.json().get("error", {})
                print(f"    ✗ {model}  →  {r2.status_code}: {err.get('message', '?')[:80]}")
        except Exception as e:
            print(f"    ✗ {model}  →  exception: {e}")

    print()
    if any_worked:
        print("  ✅ At least one model works — API key is fully functional.\n")
    else:
        print("  ⚠️  Key is valid but no generateContent call succeeded.\n")
        print("     Possible causes:")
        print("     - Generative Language API not enabled in your GCP project")
        print("     - API key has endpoint restrictions (check GCP Console → Credentials)")
        print("     - All listed models are in preview and your project lacks access\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=None, help="API key to test (default: GOOGLE_API_KEY from .env)")
    args = ap.parse_args()

    key = args.key or os.getenv("GOOGLE_API_KEY", "")
    if not key:
        print("No API key found. Pass --key or set GOOGLE_API_KEY in app/.env")
        sys.exit(1)

    check_key(key)


if __name__ == "__main__":
    main()
