"""
Step 0 – Export patient JSON files from the GCP data-pipelines VM.

Flow:
  1. SSH: create a single remote temp dir  (mktemp -d).
  2. SCP: upload export_patient_data.py into it.
  3. SSH: run the Docker container → writes JSON files into tmp/out/.
  4. SCP: download tmp/out/ to local_cache_dir/<slug>/.
  5. SSH: delete the temp dir (cleanup).

Nothing is left on the remote VM after the export.

If the local cache already contains all three files the export is skipped
entirely (no SSH at all).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_EXPORT_SCRIPT = Path(__file__).parent / "export_patient_data.py"
_REQUIRED_FILES = {"patients.json", "tasks.json", "chat.json"}


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _run(cmd: list[str], *, desc: str) -> str:
    """Run a subprocess, log the description, return stdout. Raises on failure."""
    logger.info("  [gcloud] %s", desc)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({desc}):\n"
            f"  stdout: {result.stdout.strip()}\n"
            f"  stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _ssh(instance: str, zone: str, command: str, *, desc: str) -> str:
    return _run(
        ["gcloud", "compute", "ssh", instance,
         f"--zone={zone}", "--quiet",
         f"--command={command}"],
        desc=desc,
    )


def _scp_to(instance: str, zone: str, local: str, remote: str) -> None:
    _run(
        ["gcloud", "compute", "scp",
         local, f"{instance}:{remote}",
         f"--zone={zone}", "--quiet"],
        desc=f"upload {Path(local).name}",
    )


def _scp_from(instance: str, zone: str, remote: str, local: str) -> None:
    _run(
        ["gcloud", "compute", "scp", "--recurse",
         f"{instance}:{remote}", local,
         f"--zone={zone}", "--quiet"],
        desc=f"download results",
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def export(
    cpmrn: str,
    encounter: int,
    local_cache_dir: Path,
    *,
    gcp_project: str,
    gcp_instance: str,
    gcp_zone: str,
    remote_env_file: str,
    docker_image: str,
) -> Path:
    """
    Export patient data from the GCP VM into a local cache directory.

    Returns the local directory containing patients.json, tasks.json, chat.json.
    Skips the export (no SSH) if all three files are already cached locally.

    The remote VM is left completely clean — a mktemp dir is created at the
    start and deleted at the end.
    """
    slug      = f"{cpmrn}_{encounter}"
    local_out = local_cache_dir / slug

    # ── Cache check ────────────────────────────────────────────────────────────
    if local_out.is_dir() and _REQUIRED_FILES <= {f.name for f in local_out.iterdir()}:
        logger.info("  [cache] %s already cached – skipping GCP export", slug)
        return local_out

    local_out.mkdir(parents=True, exist_ok=True)

    instance = gcp_instance
    zone     = gcp_zone

    # ── Step 1: Set GCP project ────────────────────────────────────────────────
    _run(
        ["gcloud", "config", "set", "project", gcp_project, "--quiet"],
        desc=f"set project {gcp_project}",
    )

    # ── Step 2: Create remote temp dir ────────────────────────────────────────
    tmp = _ssh(instance, zone, "mktemp -d", desc="create remote tmp dir")
    if not tmp.startswith("/"):
        raise RuntimeError(f"mktemp returned unexpected value: {tmp!r}")

    remote_script = f"{tmp}/export_patient_data.py"
    remote_out    = f"{tmp}/out"

    try:
        # ── Step 3: Upload export script ──────────────────────────────────────
        _scp_to(instance, zone, str(_EXPORT_SCRIPT), remote_script)

        # ── Step 4: Create output subdir + run Docker ─────────────────────────
        docker_cmd = (
            f"mkdir -p {remote_out} && "
            f"sudo docker run --rm "
            f"--env-file {remote_env_file} "
            f"-v {tmp}:/home/scripts:ro "
            f"-v {remote_out}:/home/data/jsons "
            f"-w /home -e PYTHONPATH=/home "
            f"--entrypoint python "
            f"{docker_image} "
            f"/home/scripts/export_patient_data.py "
            f"--cpmrn {cpmrn} --encounters {encounter}"
        )
        _ssh(instance, zone, docker_cmd, desc=f"docker export {cpmrn}/{encounter}")

        # ── Step 5: Download results ──────────────────────────────────────────
        # remote_out/ contains patients.json, tasks.json, chat.json directly.
        # SCP --recurse copies the directory itself; we want its *contents*,
        # so we SCP each file individually.
        for fname in _REQUIRED_FILES:
            _scp_from(
                instance, zone,
                f"{remote_out}/{fname}",
                str(local_out),
            )

    finally:
        # ── Step 6: Cleanup temp dir (always, even on failure) ────────────────
        try:
            _ssh(instance, zone, f"rm -rf {tmp}", desc="cleanup remote tmp dir")
        except Exception as cleanup_exc:
            logger.warning("  [cleanup] failed to remove %s: %s", tmp, cleanup_exc)

    missing = _REQUIRED_FILES - {f.name for f in local_out.iterdir()}
    if missing:
        raise RuntimeError(
            f"Export appeared to succeed but files are missing: {missing}"
        )

    logger.info("  [export] %s → %s", slug, local_out)
    return local_out
