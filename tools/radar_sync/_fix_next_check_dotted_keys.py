"""
One-time cleanup: merge stray top-level "next_check.<field>" keys (created by the
now-fixed dotted-path $set bug in gcs_store.py) into the real nested next_check
dict, then remove the bogus flat key.

Policy:
  - last_care_gap_alert_at: always merged in (nothing else ever writes this field,
    so there's no "superseded by a newer value" case to worry about).
  - due_after: only merged in if the bogus value is chronologically LATER than
    the due_after already in next_check — otherwise a later full next_check
    rebuild has already superseded it, and merging the older value in would
    wrongly push the deadline backward. In that case the bogus key is just
    dropped.
  - If next_check itself is missing/None (problem since resolved), the bogus
    key is orphaned data — dropped without merging anywhere.

Run:
  source .venv/bin/activate
  GCS_BUCKET=patientview-cds-pipeline-ops python3 tools/radar_sync/_fix_next_check_dotted_keys.py
"""
import sys
import json
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parents[2]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.services.gcs_store import get_gcs_db


def _parse(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def main():
    db = get_gcs_db()
    bucket = db["patient_problems"]._bucket

    fixed_blobs = 0
    fixed_docs = 0
    dropped = 0

    for blob in bucket.list_blobs(prefix="patient_problems/"):
        if not blob.name.endswith(".json"):
            continue
        arr = json.loads(blob.download_as_text(encoding="utf-8"))
        if not isinstance(arr, list):
            continue

        changed = False
        for problem in arr:
            bogus_keys = [k for k in list(problem.keys()) if isinstance(k, str) and k.startswith("next_check.")]
            if not bogus_keys:
                continue
            nc = problem.get("next_check")
            for k in bogus_keys:
                value = problem.pop(k)
                subfield = k[len("next_check."):]
                changed = True

                if not isinstance(nc, dict):
                    print("DROP (no next_check to merge into):", blob.name, problem.get("problem_name"), k, value)
                    dropped += 1
                    continue

                if subfield == "due_after" and "due_after" in nc:
                    try:
                        if _parse(value) <= _parse(nc["due_after"]):
                            print("DROP (superseded by newer rebuild):", blob.name, problem.get("problem_name"),
                                  k, value, "<=", nc["due_after"])
                            dropped += 1
                            continue
                    except Exception:
                        pass

                nc[subfield] = value
                print("MERGED:", blob.name, problem.get("problem_name"), k, "->", value)
                fixed_docs += 1

        if changed:
            blob.upload_from_string(json.dumps(arr, default=str), content_type="application/json")
            fixed_blobs += 1

    print(f"--- Done: {fixed_blobs} blob(s) rewritten, {fixed_docs} field(s) merged, {dropped} superseded/orphaned key(s) dropped ---")


if __name__ == "__main__":
    main()
