#!/usr/bin/env bash
# deploy_polymarket.sh — despliega polymarket-agent incluyendo shared/
#
# El Docker build context es services/polymarket-agent/.
# shared/ está en el root del repo → hay que copiarlo antes del build.
# Se limpia automáticamente tras el deploy.
#
# Uso:
#   bash scripts/deploy_polymarket.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_DIR="$REPO_ROOT/services/polymarket-agent"
SHARED_SRC="$REPO_ROOT/shared"
SHARED_DST="$SERVICE_DIR/shared"

echo "[deploy-polymarket] Copiando shared/ → $SHARED_DST"
cp -r "$SHARED_SRC" "$SHARED_DST"

cleanup() {
  echo "[deploy-polymarket] Limpiando shared/ temporal"
  rm -rf "$SHARED_DST"
}
trap cleanup EXIT

echo "[deploy-polymarket] gcloud run deploy ..."
gcloud run deploy polymarket-agent \
  --source "$SERVICE_DIR" \
  --region europe-west1 \
  --project prediction-intelligence \
  --account pejocanal@gmail.com \
  --quiet

echo "[deploy-polymarket] Deploy completado."
