#!/usr/bin/env bash
# Store the prod-tech BigQuery service account key as a GCP Secret Manager secret.
# This SA is used for READING from prod-tech-project1-bv479-zo027 BigQuery tables only.
#
# Usage:
#   KEY_FILE=/path/to/prodtech_sa_key.json ./scripts/setup_prodtech_bq_secret.sh
#
# Prerequisites:
#   - gcloud auth login with an account that has secretmanager.secrets.create
#     on patientview-9uxml
#   - The key file for streamlit-connection-sa@prod-tech-project1-bv479-zo027.iam.gserviceaccount.com
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-patientview-9uxml}"
SECRET_NAME="PRODTECH_BQ_SA_KEY"
: "${KEY_FILE:?KEY_FILE must be set to the path of the prod-tech service account JSON key file}"

echo "Creating secret '$SECRET_NAME' in project '$PROJECT_ID' ..."

# Create the secret (idempotent — will error if already exists, use || true to skip)
gcloud secrets create "$SECRET_NAME" \
  --project="$PROJECT_ID" \
  --replication-policy="automatic" 2>/dev/null || echo "Secret already exists, adding new version..."

# Store the key file as the latest version
gcloud secrets versions add "$SECRET_NAME" \
  --project="$PROJECT_ID" \
  --data-file="$KEY_FILE"

echo ""
echo "Secret '$SECRET_NAME' created/updated successfully."
echo ""
echo "Next: grant the cds-pipeline-runner SA access to read it:"
echo "  gcloud secrets add-iam-policy-binding $SECRET_NAME \\"
echo "    --project=$PROJECT_ID \\"
echo "    --member='serviceAccount:cds-pipeline-runner@${PROJECT_ID}.iam.gserviceaccount.com' \\"
echo "    --role='roles/secretmanager.secretAccessor'"
