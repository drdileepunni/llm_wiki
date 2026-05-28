#!/usr/bin/env bash
# Create a Cloud Scheduler job that triggers the CDS pipeline every hour.
# Usage: SERVICE_URL=https://... SCHEDULER_SA_EMAIL=... ./scripts/setup_cloud_scheduler.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-prod-tech-project1-bv479-zo027}"
REGION="${REGION:-asia-south1}"

: "${SERVICE_URL:?SERVICE_URL must be set to the Cloud Run service URL}"
: "${SCHEDULER_SA_EMAIL:?SCHEDULER_SA_EMAIL must be set (the SA that invokes Cloud Run)}"

gcloud scheduler jobs create http cds-pipeline-hourly \
  --project="$PROJECT_ID" \
  --location="$REGION" \
  --schedule="0 * * * *" \
  --uri="${SERVICE_URL}/trigger" \
  --http-method=POST \
  --oidc-service-account-email="$SCHEDULER_SA_EMAIL" \
  --oidc-token-audience="$SERVICE_URL" \
  --time-zone="Asia/Kolkata" \
  --attempt-deadline=1800s

echo "Cloud Scheduler job 'cds-pipeline-hourly' created."
echo "Trigger URL: ${SERVICE_URL}/trigger"
