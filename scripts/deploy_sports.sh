#!/usr/bin/env bash
# deploy_sports.sh — despliega sports-agent incluyendo shared/
#
# shared/ está en el root del repo → se copia al contexto antes del build,
# igual que deploy_polymarket.sh.
#
# Uso:
#   bash scripts/deploy_sports.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_DIR="$REPO_ROOT/services/sports-agent"
SHARED_SRC="$REPO_ROOT/shared"
SHARED_DST="$SERVICE_DIR/shared"

echo "[deploy-sports] Copiando shared/ → $SHARED_DST"
cp -r "$SHARED_SRC" "$SHARED_DST"

cleanup() {
  echo "[deploy-sports] Limpiando shared/ temporal"
  rm -rf "$SHARED_DST"
}
trap cleanup EXIT

echo "[deploy-sports] gcloud run deploy sports-agent ..."
gcloud run deploy sports-agent \
  --source "$SERVICE_DIR" \
  --region europe-west1 \
  --project prediction-intelligence \
  --account pejocanal@gmail.com \
  --quiet

echo "[deploy-sports] Deploy completado."
