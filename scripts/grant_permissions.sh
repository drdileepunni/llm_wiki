#!/usr/bin/env bash
# Grant IAM permissions to the cds-pipeline-runner service account.
# Run once after creating the service account.
# Usage: ./scripts/grant_permissions.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-prod-tech-project1-bv479-zo027}"
SA="cds-pipeline-runner@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${GCS_BUCKET:-cds-pipeline-ops}"
BQ_PROXY_SERVICE="bigquery-service-971880579407"
BQ_PROXY_REGION="us-central1"

echo "Granting permissions to $SA ..."

# GCS — read/write operational documents
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA" \
  --role="roles/storage.objectAdmin"

# BigQuery — write to cds_study dataset
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.jobUser"

# BQ proxy Cloud Run — invoke the shared proxy service
gcloud run services add-iam-policy-binding "$BQ_PROXY_SERVICE" \
  --region="$BQ_PROXY_REGION" \
  --member="serviceAccount:$SA" \
  --role="roles/run.invoker"

# Secret Manager — read secrets
gcloud secrets add-iam-policy-binding RADAR_READ_SERVICE_ACCOUNT \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding GOOGLE_API_KEY \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

echo "Done."
