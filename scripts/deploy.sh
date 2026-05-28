#!/usr/bin/env bash
# Deploy the CDS pipeline to Cloud Run via Cloud Build.
# Usage: ./scripts/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-patientview-9uxml}"
REGION="${REGION:-asia-south1}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Submitting build for project: $PROJECT_ID"
gcloud builds submit \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --config="$REPO_ROOT/cloudbuild.yaml" \
  "$REPO_ROOT"
