#!/usr/bin/env bash
# Run the live pipeline for ONE patient against the deployed service and pretty-print
# the debug dump. Service stays private — uses your gcloud identity for Cloud Run auth
# plus the ALERT_FEEDBACK_TOKEN secret as the endpoint guard.
#
# Usage: scripts/debug_run_one.sh CPMRN [ENCOUNTER]
set -euo pipefail

CPMRN="${1:?usage: debug_run_one.sh CPMRN [ENCOUNTER]}"
ENCOUNTER="${2:-1}"
URL="https://cds-pipeline-5w6sy57lvq-el.a.run.app"

TOKEN="$(gcloud secrets versions access latest --secret=ALERT_FEEDBACK_TOKEN)"
ID_TOKEN="$(gcloud auth print-identity-token)"

curl -s -X POST "$URL/debug/run-one" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "X-Debug-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"cpmrn\": \"$CPMRN\", \"encounter\": $ENCOUNTER}" \
  | python3 -m json.tool
