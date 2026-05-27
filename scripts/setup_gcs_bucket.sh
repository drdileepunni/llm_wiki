#!/usr/bin/env bash
# Create the GCS bucket used by the CDS pipeline for operational documents.
# Usage: ./scripts/setup_gcs_bucket.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-prod-tech-project1-bv479-zo027}"
BUCKET="${GCS_BUCKET:-cds-pipeline-ops}"
REGION="${REGION:-asia-south1}"

echo "Creating bucket gs://$BUCKET in $REGION ..."

gsutil mb -p "$PROJECT_ID" -l "$REGION" "gs://$BUCKET"
gsutil uniformbucketlevelaccess set on "gs://$BUCKET"

echo "Bucket gs://$BUCKET created with uniform access control."
