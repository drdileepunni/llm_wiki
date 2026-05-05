"""
VM control endpoints for the self-hosted MedGemma instances.
Delegates to gcloud CLI so existing ADC/gcloud auth on the host machine is used.
"""

import subprocess
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/vm", tags=["vm"])
log = logging.getLogger("wiki.vm")

_PROJECT  = "patientview-9uxml"

_GPU_INSTANCE = "medgemma"
_GPU_ZONE     = "us-east1-b"

_CPU_INSTANCE = "medgemma-cpu"
_CPU_ZONE     = "us-central1-a"

# ── Shared helpers ────────────────────────────────────────────────────────────


def _gcloud(instance: str, zone: str, *args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = ["gcloud", "compute", "instances", *args,
           instance, f"--zone={zone}", f"--project={_PROJECT}"]
    log.info("VM cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def _get_status(instance: str, zone: str) -> str:
    """Return one of: RUNNING, TERMINATED, STAGING, STOPPING, unknown."""
    try:
        result = subprocess.run(
            [
                "gcloud", "compute", "instances", "describe", instance,
                f"--zone={zone}", f"--project={_PROJECT}",
                "--format=value(status)",
            ],
            capture_output=True, text=True, timeout=20,
        )
        return result.stdout.strip() or "unknown"
    except Exception as exc:
        log.warning("VM status check failed (%s): %s", instance, exc)
        return "unknown"


# ── GPU (original) instance endpoints ────────────────────────────────────────


@router.get("/status")
def vm_status():
    return {"status": _get_status(_GPU_INSTANCE, _GPU_ZONE)}


@router.post("/start")
def vm_start():
    current = _get_status(_GPU_INSTANCE, _GPU_ZONE)
    if current == "RUNNING":
        return {"status": "RUNNING", "message": "already running"}
    rc, out, err = _gcloud(_GPU_INSTANCE, _GPU_ZONE, "start", timeout=120)
    if rc != 0:
        log.error("GPU VM start failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud start failed")
    log.info("GPU VM start initiated")
    return {"status": "STAGING", "message": "start initiated"}


@router.post("/stop")
def vm_stop():
    current = _get_status(_GPU_INSTANCE, _GPU_ZONE)
    if current == "TERMINATED":
        return {"status": "TERMINATED", "message": "already stopped"}
    rc, out, err = _gcloud(_GPU_INSTANCE, _GPU_ZONE, "stop", timeout=120)
    if rc != 0:
        log.error("GPU VM stop failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud stop failed")
    log.info("GPU VM stop initiated")
    return {"status": "STOPPING", "message": "stop initiated"}


# ── CPU (backup) instance endpoints ──────────────────────────────────────────


@router.get("/cpu/status")
def cpu_vm_status():
    return {"status": _get_status(_CPU_INSTANCE, _CPU_ZONE)}


@router.post("/cpu/start")
def cpu_vm_start():
    current = _get_status(_CPU_INSTANCE, _CPU_ZONE)
    if current == "RUNNING":
        return {"status": "RUNNING", "message": "already running"}
    rc, out, err = _gcloud(_CPU_INSTANCE, _CPU_ZONE, "start", timeout=120)
    if rc != 0:
        log.error("CPU VM start failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud start failed")
    log.info("CPU VM start initiated")
    return {"status": "STAGING", "message": "start initiated"}


@router.post("/cpu/stop")
def cpu_vm_stop():
    current = _get_status(_CPU_INSTANCE, _CPU_ZONE)
    if current == "TERMINATED":
        return {"status": "TERMINATED", "message": "already stopped"}
    rc, out, err = _gcloud(_CPU_INSTANCE, _CPU_ZONE, "stop", timeout=120)
    if rc != 0:
        log.error("CPU VM stop failed: %s", err)
        raise HTTPException(status_code=500, detail=err.strip() or "gcloud stop failed")
    log.info("CPU VM stop initiated")
    return {"status": "STOPPING", "message": "stop initiated"}


# ── Active instance selection ─────────────────────────────────────────────────


class ActiveInstanceBody(BaseModel):
    instance: str  # "gpu" | "cpu"


@router.get("/active")
def get_active_instance():
    from .. import state
    return {"instance": state.active_medgemma}


@router.post("/active")
def set_active_instance(body: ActiveInstanceBody):
    if body.instance not in ("gpu", "cpu"):
        raise HTTPException(status_code=400, detail="instance must be 'gpu' or 'cpu'")
    from .. import state
    state.set_active(body.instance)
    log.info("Active MedGemma instance set to: %s", body.instance)
    return {"instance": state.active_medgemma}
