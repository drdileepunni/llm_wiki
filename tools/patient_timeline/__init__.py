"""
patient_timeline — end-to-end pipeline for generating a clinical timeline
and evaluation snapshots for a single patient encounter.

Public API
----------
    from tools.patient_timeline import generate

    output_dir = generate(
        "INKLBLR1234/1",          # CPMRN/encounter
        gemini_notes=True,        # extract clinical events from note free-text
        snapshots=True,           # generate 5 evaluation snapshot folders
    )
    # → Path(".../timelines/INKLBLR1234_1")

The function orchestrates four steps:

  Step 0  Export  – pull patients.json / tasks.json / chat.json from the
                    GCP data-pipelines VM (skipped when already cached or
                    when json_dir is supplied directly).
  Step 1  Build   – run six extractors → full timeline CSV.
  Step 2  Truncate – filter to clinically meaningful rows → truncated CSV.
  Step 3  Snapshots – five evaluation snapshot folders (optional).

Output layout
-------------
    <output_dir>/<slug>/
        <slug>_timeline_full.csv
        <slug>_timeline_truncated.csv
        snapshot_1/
            snapshot.csv
            question.txt
            answer.txt
        …
        snapshot_5/
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger(__name__)


def generate(
    patient: str,
    *,
    # -- optional overrides --
    json_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    cache_dir: Optional[Path] = None,
    # -- feature flags --
    gemini_notes: bool = True,
    snapshots: bool = True,
    force: bool = False,
    # -- GCP / API overrides (fall back to config defaults) --
    gemini_api_key: Optional[str] = None,
    gcp_project: Optional[str] = None,
    gcp_instance: Optional[str] = None,
    gcp_zone: Optional[str] = None,
    remote_env_file: Optional[str] = None,
    docker_image: Optional[str] = None,
) -> Path:
    """
    Generate a clinical timeline (and optional evaluation snapshots) for one
    patient encounter.

    Parameters
    ----------
    patient:
        Patient string in ``"CPMRN/encounter"`` format, e.g. ``"INKLBLR1234/1"``.
    json_dir:
        If supplied, skip the GCP export and use this directory directly.
        Must contain patients.json, tasks.json, chat.json.
    output_dir:
        Root directory for timeline output.  Defaults to
        ``<repo_root>/timelines``.
    cache_dir:
        Directory for raw JSON downloads from GCP.  Defaults to
        ``<repo_root>/.timeline_cache``.
    gemini_notes:
        Whether to call Gemini to extract clinical findings from note text.
        Adds significant latency but improves timeline richness.  Default True.
    snapshots:
        Whether to generate the five evaluation snapshot folders.  Default True.
    force:
        Re-run even if output already exists.
    gemini_api_key:
        Override for the Gemini API key (default: ``GOOGLE_API_KEY`` env var).
    gcp_project / gcp_instance / gcp_zone / remote_env_file /
    remote_script_dir / remote_data_dir / docker_image:
        GCP overrides; each falls back to the value in config.py.

    Returns
    -------
    Path
        The patient output directory (e.g. ``timelines/INKLBLR1234_1/``).
    """
    # ── Parse patient string ──────────────────────────────────────────────────
    if "/" not in patient:
        raise ValueError(
            f"patient must be in 'CPMRN/encounter' format, got: {patient!r}"
        )
    cpmrn, enc_str = patient.split("/", 1)
    try:
        encounter = int(enc_str)
    except ValueError:
        raise ValueError(
            f"encounter must be an integer, got: {enc_str!r}"
        )

    slug = f"{cpmrn}_{encounter}"

    # ── Resolve paths ─────────────────────────────────────────────────────────
    out_root    = Path(output_dir)  if output_dir else config.DEFAULT_OUTPUT_DIR
    patient_dir = out_root / slug
    cache_root  = Path(cache_dir)   if cache_dir  else config.DEFAULT_CACHE_DIR

    full_csv     = patient_dir / f"{slug}_timeline_full.csv"
    trunc_csv    = patient_dir / f"{slug}_timeline_truncated.csv"

    patient_dir.mkdir(parents=True, exist_ok=True)

    # ── Resolve API / GCP settings ────────────────────────────────────────────
    api_key = gemini_api_key or config.GEMINI_API_KEY

    gcp_kw = dict(
        gcp_project     = gcp_project     or config.GCP_PROJECT,
        gcp_instance    = gcp_instance    or config.GCP_INSTANCE,
        gcp_zone        = gcp_zone        or config.GCP_ZONE,
        remote_env_file = remote_env_file or config.REMOTE_ENV_FILE,
        docker_image    = docker_image    or config.DOCKER_IMAGE,
    )

    # ── Step 0: Export ────────────────────────────────────────────────────────
    if json_dir is None:
        logger.info("[%s] Step 0 – exporting from GCP …", slug)
        from .exporter import export
        json_dir = export(
            cpmrn, encounter, cache_root,
            **gcp_kw,
        )
    else:
        json_dir = Path(json_dir)
        logger.info("[%s] Step 0 – using local json_dir: %s", slug, json_dir)

    # ── Step 1: Build full timeline ───────────────────────────────────────────
    if force or not full_csv.exists():
        logger.info("[%s] Step 1 – building full timeline …", slug)
        from .timeline import build
        build(json_dir, cpmrn, full_csv, gemini_enabled=gemini_notes)
    else:
        logger.info("[%s] Step 1 – full timeline already exists, skipping", slug)

    # ── Step 2: Truncate ──────────────────────────────────────────────────────
    if force or not trunc_csv.exists():
        logger.info("[%s] Step 2 – truncating timeline …", slug)
        from .truncate import truncate
        df = truncate(full_csv, trunc_csv)
    else:
        logger.info("[%s] Step 2 – truncated timeline already exists, skipping", slug)
        import pandas as pd
        df = pd.read_csv(trunc_csv, dtype=str)

    logger.info("[%s] Truncated timeline: %d rows", slug, len(df))

    # ── Step 3: Snapshots ─────────────────────────────────────────────────────
    if snapshots:
        snap1_dir = patient_dir / "snapshot_1"
        if force or not snap1_dir.exists():
            if not api_key:
                logger.warning(
                    "[%s] Step 3 – GEMINI_API_KEY not set; skipping snapshots", slug
                )
            else:
                logger.info("[%s] Step 3 – generating evaluation snapshots …", slug)
                from .snapshots import ask_gemini, save_snapshots
                snap_data = ask_gemini(df, api_key)
                save_snapshots(df, snap_data, patient_dir)
        else:
            logger.info("[%s] Step 3 – snapshots already exist, skipping", slug)
    else:
        logger.info("[%s] Step 3 – snapshots disabled", slug)

    logger.info("[%s] Done → %s", slug, patient_dir)
    return patient_dir
