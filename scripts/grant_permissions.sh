#!/usr/bin/env bash
# Grant IAM permissions to the cds-pipeline-runner service account.
# Run once after creating the service account.
# Usage: ./scripts/grant_permissions.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-patientview-9uxml}"
SA="cds-pipeline-runner@${PROJECT_ID}.iam.gserviceaccount.com"
BUCKET="${GCS_BUCKET:-patientview-cds-pipeline-ops}"
PROD_PROJECT="${PROD_PROJECT:-prod-tech-project1-bv479-zo027}"
BQ_PROXY_SERVICE="bigquery-service-971880579407"
BQ_PROXY_REGION="us-central1"

echo "Granting permissions to $SA ..."

# GCS — read/write operational documents (bucket is global, project-agnostic)
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA" \
  --role="roles/storage.objectAdmin"

# BigQuery — write to cds_study dataset in patientview project
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/bigquery.jobUser"

# BQ proxy Cloud Run (in prod project) — invoke to read patient/analytics data
gcloud run services add-iam-policy-binding "$BQ_PROXY_SERVICE" \
  --region="$BQ_PROXY_REGION" \
  --project="$PROD_PROJECT" \
  --member="serviceAccount:$SA" \
  --role="roles/run.invoker"

# Secret Manager — secrets live in patientview project
gcloud secrets add-iam-policy-binding RADAR_READ_SERVICE_ACCOUNT \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding GEMINI_API_KEY \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

# Prod-tech BQ SA key — read prod-tech BigQuery tables directly (no proxy)
gcloud secrets add-iam-policy-binding PRODTECH_BQ_SA_KEY \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/secretmanager.secretAccessor"

echo "Done."
