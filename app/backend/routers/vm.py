"""
VM control endpoints for the self-hosted MedGemma instance.
Delegates to gcloud CLI so existing ADC/gcloud auth on the host machine is used.
"""

import subprocess
import logging
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/vm", tags=["vm"])
log = logging.getLogger("wiki.vm")

_INSTANCE = "medgemma"
_ZONE     = "us-east1-b"
_PROJECT  = "patientview-9uxml"


def _gcloud(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = ["gcloud", "compute", "instances", *args,
           _INSTANCE, f"--zone={_ZONE}", f"--project={_PROJECT}"]
    log.info("VM cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _get_status() -> str:
    """Return one of: RUNNING, TERMINATED, STAGING, STOPPING, unknown."""
    try:
        result = subprocess.run(
            [
                "gcloud", "compute", "instances", "describe", _INSTANCE,
                f"--zone={_ZONE}", f"--project={_PROJECT}",
                "--format=value(status)",
            ],
            capture_output=True, text=True, timeout=20,
        )
        return result.stdout.strip() or "unknown"
    except Exception as exc:
        log.warning("VM status check failed: %s", exc)
        return "unknown"


@router.get("/status")
def vm_status():
    status = _get_status()
    return {"status": status}


@router.post("/start")
def vm_start():
    current = _get_status()
    if current == "RUNNING":
        return {"status": "RUNNING", "message": "already running"}
    rc, out, err = _gcloud("start", timeout=120)
    if rc != 0:
        log.error("VM start failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud start failed")
    log.info("VM start initiated")
    return {"status": "STAGING", "message": "start initiated"}


@router.post("/stop")
def vm_stop():
    current = _get_status()
    if current == "TERMINATED":
        return {"status": "TERMINATED", "message": "already stopped"}
    rc, out, err = _gcloud("stop", timeout=120)
    if rc != 0:
        log.error("VM stop failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud stop failed")
    log.info("VM stop initiated")
    return {"status": "STOPPING", "message": "stop initiated"}
