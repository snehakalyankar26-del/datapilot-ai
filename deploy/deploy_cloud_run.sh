#!/usr/bin/env bash
# Deploy DataPilot AI to Google Cloud Run with the Gemini key stored in Secret Manager.
# Usage:  PROJECT_ID=my-proj GEMINI_API_KEY=xxx APP_API_KEY=demo-key ./deploy/deploy_cloud_run.sh
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-asia-south1}"          # Mumbai
SERVICE="${SERVICE:-datapilot-ai}"
gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# 1. Secrets (never baked into the image or env files)
upsert_secret () {
  if gcloud secrets describe "$1" >/dev/null 2>&1; then printf "%s" "$2" | gcloud secrets versions add "$1" --data-file=-
  else printf "%s" "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=automatic; fi
}
upsert_secret gemini-api-key "${GEMINI_API_KEY:?set GEMINI_API_KEY}"
upsert_secret datapilot-app-keys "${APP_API_KEY:?set APP_API_KEY (access key for judges)}"

# 2. Least-privilege runtime service account
SA="datapilot-run@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || gcloud iam service-accounts create datapilot-run --display-name "DataPilot runtime"
for s in gemini-api-key datapilot-app-keys; do
  gcloud secrets add-iam-policy-binding "$s" --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null
done

# 3. Build from source with Cloud Build and deploy
gcloud run deploy "$SERVICE" --source . --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --cpu 1 --memory 1Gi --concurrency 40 --min-instances 0 --max-instances 3 --session-affinity --timeout 60 \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,APP_API_KEYS=datapilot-app-keys:latest" \
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL:-gemini-2.5-flash},RATE_LIMIT_PER_MIN=30"

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "ALLOWED_ORIGINS=$URL" >/dev/null
echo "Deployed: $URL"
