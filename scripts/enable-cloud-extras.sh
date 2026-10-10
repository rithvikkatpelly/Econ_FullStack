#!/usr/bin/env bash
# Switch on the three opt-in Google Cloud features on the deployed app, then
# redeploy. Safe to re-run: every step skips what already exists.
#
#   1. MCP server over HTTP  — its own runtime service account (reads the FRED
#      secret, nothing else) and the MCP_SERVICE repo variable, so deploy.yml
#      deploys it as a third Cloud Run service.
#   2. Cloud Trace           — the Telemetry API and permission for the API's
#      runtime account to write traces; CLOUD_TRACE=1.
#   3. Shared rate limits    — the (default) Firestore database (free tier),
#      a TTL policy that deletes idle buckets, and read/write for the API's
#      runtime account; RATE_LIMIT_BACKEND=firestore.
#
# Needs: gcloud signed in as a project owner, gh signed in with access to the
# repo. Usage:
#   gcloud auth login rrkatpelly@ucdavis.edu
#   scripts/enable-cloud-extras.sh
set -euo pipefail

PROJECT="${PROJECT:-econ-fullstack-510918}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-rithvikkatpelly/Econ_FullStack}"
export CLOUDSDK_CORE_PROJECT="$PROJECT"

DEPLOYER="github-deployer@${PROJECT}.iam.gserviceaccount.com"
API_SA="econ-api-runtime@${PROJECT}.iam.gserviceaccount.com"
MCP_SA="econ-mcp-runtime@${PROJECT}.iam.gserviceaccount.com"

say() { printf '\n== %s\n' "$*"; }

say "Checking gcloud and gh"
gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null
gh auth status >/dev/null

say "1/3 MCP server over HTTP"
gcloud iam service-accounts describe "$MCP_SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create econ-mcp-runtime \
    --display-name="Econ MCP server (Cloud Run runtime)"
gcloud iam service-accounts add-iam-policy-binding "$MCP_SA" \
  --member="serviceAccount:${DEPLOYER}" --role=roles/iam.serviceAccountUser --quiet >/dev/null
gcloud secrets add-iam-policy-binding fred-api-key \
  --member="serviceAccount:${MCP_SA}" --role=roles/secretmanager.secretAccessor --quiet >/dev/null
gh variable set MCP_SERVICE_ACCOUNT -R "$REPO" -b "$MCP_SA"
gh variable set MCP_SERVICE -R "$REPO" -b econ-data-mcp

say "2/3 Cloud Trace"
gcloud services enable telemetry.googleapis.com cloudtrace.googleapis.com
# cloudtrace.agent writes traces; telemetry.tracesWriter is the Telemetry
# API's own role (what ADK's exporter calls). Grant both; the second is
# skipped if this project doesn't offer it yet.
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${API_SA}" --role=roles/cloudtrace.agent \
  --condition=None --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${API_SA}" --role=roles/telemetry.tracesWriter \
  --condition=None --quiet >/dev/null 2>&1 ||
  echo "   (roles/telemetry.tracesWriter not available; cloudtrace.agent granted)"
gh variable set CLOUD_TRACE -R "$REPO" -b 1

say "3/3 Shared rate limits in Firestore"
gcloud services enable firestore.googleapis.com
gcloud firestore databases describe --database='(default)' >/dev/null 2>&1 ||
  gcloud firestore databases create --database='(default)' \
    --location="$REGION" --type=firestore-native
gcloud firestore fields ttls update expires_at \
  --collection-group=rate_limits --enable-ttl --async --quiet >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${API_SA}" --role=roles/datastore.user \
  --condition=None --quiet >/dev/null
gh variable set RATE_LIMIT_BACKEND -R "$REPO" -b firestore

say "Redeploying"
gh workflow run deploy.yml -R "$REPO" --ref main
echo "Watch it with: gh run watch -R $REPO \$(gh run list -R $REPO -w 'Deploy to Cloud Run' -L 1 --json databaseId --jq '.[0].databaseId')"
