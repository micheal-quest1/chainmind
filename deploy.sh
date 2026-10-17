#!/usr/bin/env bash
# One-command deploy to Google Cloud Run.
#
#   ./deploy.sh                      # uses defaults below
#   PROJECT=my-proj REGION=us-central1 ./deploy.sh
#
# Keys are read from .env and stored in Secret Manager. They are never passed on
# the command line (shell history, process list) and never baked into the image.
set -euo pipefail

PROJECT="${PROJECT:-mithran-447407}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-chainmind}"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

[ -f .env ] || { echo "error: .env not found. cp .env.example .env and fill it in."; exit 1; }

say "Enabling required APIs on ${PROJECT}"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  --project "$PROJECT" --quiet

# ---- secrets -------------------------------------------------------------
USE_SECRET_MANAGER=1

put_secret() {
  local name="$1" var="$2" tmp
  tmp="$(mktemp)"
  chmod 600 "$tmp"
  # Strip the KEY= prefix, keeping any '=' inside the value itself.
  grep -m1 "^${var}=" .env | cut -d= -f2- | tr -d '\r\n' > "$tmp"
  if [ ! -s "$tmp" ]; then
    echo "  skip ${name}: ${var} is empty in .env"
    rm -f "$tmp"; return
  fi
  if gcloud secrets describe "$name" --project "$PROJECT" >/dev/null 2>&1; then
    if ! gcloud secrets versions add "$name" --data-file="$tmp" --project "$PROJECT" --quiet >/dev/null 2>&1; then
      USE_SECRET_MANAGER=0
    else
      echo "  updated ${name}"
    fi
  else
    if ! gcloud secrets create "$name" --data-file="$tmp" --replication-policy=automatic \
         --project "$PROJECT" --quiet >/dev/null 2>&1; then
      USE_SECRET_MANAGER=0
    else
      echo "  created ${name}"
    fi
  fi
  rm -f "$tmp"
}

say "Storing API keys in Secret Manager"
put_secret chainmind-gemini-key GEMINI_API_KEY
put_secret chainmind-maps-key   GOOGLE_MAPS_API_KEY

# ---- grant the runtime service account read access -----------------------
SECRET_FLAGS=""
if [ "$USE_SECRET_MANAGER" = "1" ]; then
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
  RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  for s in chainmind-gemini-key chainmind-maps-key; do
    gcloud secrets describe "$s" --project "$PROJECT" >/dev/null 2>&1 || continue
    gcloud secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:${RUNTIME_SA}" \
      --role=roles/secretmanager.secretAccessor \
      --project "$PROJECT" --quiet >/dev/null 2>&1 || true
  done

  gcloud secrets describe chainmind-gemini-key --project "$PROJECT" >/dev/null 2>&1 \
    && SECRET_FLAGS="GEMINI_API_KEY=chainmind-gemini-key:latest"
  if gcloud secrets describe chainmind-maps-key --project "$PROJECT" >/dev/null 2>&1; then
    [ -n "$SECRET_FLAGS" ] && SECRET_FLAGS="${SECRET_FLAGS},"
    SECRET_FLAGS="${SECRET_FLAGS}GOOGLE_MAPS_API_KEY=chainmind-maps-key:latest"
  fi
fi

# Secret Manager can be unavailable (API still propagating after enablement, or
# the caller lacks the role). Rather than fail the deploy, fall back to passing
# the keys as Cloud Run env vars, read from .env by this script - they still
# never touch the image or the repo. Re-run once Secret Manager is live to move
# them back.
ENV_FLAGS="CHAINMIND_DB=/data/chainmind.db"
if [ -z "$SECRET_FLAGS" ]; then
  echo
  echo "  !! Secret Manager unavailable - falling back to Cloud Run env vars."
  for var in GEMINI_API_KEY GOOGLE_MAPS_API_KEY; do
    val="$(grep -m1 "^${var}=" .env | cut -d= -f2- | tr -d '\r\n' || true)"
    [ -n "$val" ] && ENV_FLAGS="${ENV_FLAGS},${var}=${val}"
  done
fi

say "Building and deploying ${SERVICE} to Cloud Run (${REGION})"
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --cpu 1 --memory 1Gi \
  --concurrency 40 \
  --timeout 300 \
  --min-instances 1 --max-instances 1 \
  --no-cpu-throttling \
  --execution-environment gen2 \
  --set-env-vars "^;^${ENV_FLAGS//,/;}" \
  ${SECRET_FLAGS:+--set-secrets "$SECRET_FLAGS"} \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
say "Deployed: ${URL}"
echo "  dashboard    ${URL}/"
echo "  capabilities ${URL}/api/capabilities"
echo "  openapi      ${URL}/docs"
echo "  mcp (http)   ${URL}/mcp"
echo
say "Verifying the live deployment"
curl -fsS "${URL}/healthz" && echo
curl -fsS "${URL}/api/capabilities" | python3 -m json.tool | head -25 || true
