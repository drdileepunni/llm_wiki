"""
Command-line entry point for the patient_timeline tool.

Usage
-----
    python -m tools.patient_timeline CPMRN/encounter [options]

Examples
--------
    # Full pipeline (export → build → truncate → snapshots):
    python -m tools.patient_timeline INKLBLR1234/1

    # Multiple patients:
    python -m tools.patient_timeline INKLBLR1234/1 INKLBLR5678/2

    # Skip the GCP export (JSON files already on disk):
    python -m tools.patient_timeline INKLBLR1234/1 --json-dir /path/to/jsons

    # Build timeline only (no snapshots):
    python -m tools.patient_timeline INKLBLR1234/1 --no-snapshots

    # Build timeline without Gemini note extraction:
    python -m tools.patient_timeline INKLBLR1234/1 --no-gemini-notes

    # Custom output directory:
    python -m tools.patient_timeline INKLBLR1234/1 --output-dir /data/timelines

    # Force re-run even if output exists:
    python -m tools.patient_timeline INKLBLR1234/1 --force
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tools.patient_timeline",
        description=(
            "Generate a clinical timeline and evaluation snapshots "
            "for one or more patient encounters."
        ),
    )
    p.add_argument(
        "patients",
        nargs="+",
        metavar="CPMRN/encounter",
        help='Patient identifiers in "CPMRN/encounter" format, e.g. INKLBLR1234/1',
    )

    # ── Output / cache paths ──────────────────────────────────────────────────
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        help=(
            "Root directory for timeline output.  "
            "Defaults to <repo_root>/timelines."
        ),
    )
    p.add_argument(
        "--cache-dir",
        metavar="DIR",
        help=(
            "Directory for raw JSON downloads cached from GCP.  "
            "Defaults to <repo_root>/.timeline_cache."
        ),
    )
    p.add_argument(
        "--json-dir",
        metavar="DIR",
        help=(
            "Use this local directory instead of exporting from GCP.  "
            "Must contain patients.json, tasks.json, chat.json.  "
            "Only valid when a single patient is specified."
        ),
    )

    # ── Feature flags ─────────────────────────────────────────────────────────
    p.add_argument(
        "--no-gemini-notes",
        action="store_true",
        default=False,
        help="Skip Gemini note-event extraction (faster, less rich timeline).",
    )
    p.add_argument(
        "--no-snapshots",
        action="store_true",
        default=False,
        help="Skip evaluation snapshot generation.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Re-run all steps even if output already exists.",
    )

    # ── GCP overrides ─────────────────────────────────────────────────────────
    p.add_argument("--gcp-project",     metavar="STR",  help="GCP project ID override.")
    p.add_argument("--gcp-instance",    metavar="STR",  help="GCP compute instance name override.")
    p.add_argument("--gcp-zone",        metavar="STR",  help="GCP zone override.")
    p.add_argument("--remote-env-file", metavar="PATH", help="Path to .env on remote VM (default: ~/.env).")
    p.add_argument("--docker-image",    metavar="STR",  help="Docker image override.")

    # ── Logging ───────────────────────────────────────────────────────────────
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG logging.",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args   = parser.parse_args(argv)

    logging.basicConfig(
        level   = logging.DEBUG if args.verbose else logging.INFO,
        format  = "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt = "%H:%M:%S",
    )

    # Validate --json-dir usage
    if args.json_dir and len(args.patients) > 1:
        parser.error("--json-dir can only be used with a single patient.")

    from tools.patient_timeline import generate  # noqa: PLC0415

    gcp_kw: dict = {}
    for attr, kw in [
        ("gcp_project",     "gcp_project"),
        ("gcp_instance",    "gcp_instance"),
        ("gcp_zone",        "gcp_zone"),
        ("remote_env_file", "remote_env_file"),
        ("docker_image",    "docker_image"),
    ]:
        val = getattr(args, attr.replace("-", "_"), None)
        if val:
            gcp_kw[kw] = val

    errors: list[str] = []

    for patient in args.patients:
        try:
            out = generate(
                patient,
                json_dir      = Path(args.json_dir) if args.json_dir else None,
                output_dir    = Path(args.output_dir) if args.output_dir else None,
                cache_dir     = Path(args.cache_dir) if args.cache_dir else None,
                gemini_notes  = not args.no_gemini_notes,
                snapshots     = not args.no_snapshots,
                force         = args.force,
                **gcp_kw,
            )
            print(f"✓  {patient}  →  {out}")
        except Exception as exc:
            logging.error("✗  %s  →  %s", patient, exc)
            errors.append(patient)

    if errors:
        logging.error("Failed patients: %s", ", ".join(errors))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
