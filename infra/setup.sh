#!/usr/bin/env bash
# setup.sh — Provisiona GCP desde cero para prediction-intelligence
# Ejecutar UNA SOLA VEZ antes del primer deploy.
# Orden: bot primero, luego agentes (evita chicken-and-egg con TELEGRAM_BOT_URL).
#
# Prerrequisitos:
#   - gcloud CLI instalado y autenticado (gcloud auth login)
#   - firebase CLI instalado (npm install -g firebase-tools)
#   - Proyecto GCP ya creado con billing activado
#
# Uso:
#   chmod +x infra/setup.sh
#   ./infra/setup.sh

set -euo pipefail

PROJECT="prediction-intelligence"
REGION="europe-west1"

echo "=== Prediction Intelligence — Setup GCP ==="
echo "Proyecto: $PROJECT | Region: $REGION"
echo ""

# --- 1. Habilitar APIs necesarias ---
echo "[1/7] Habilitando APIs de GCP..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  --project="$PROJECT"
echo "    APIs habilitadas."

# --- 2. Crear base de datos Firestore en modo nativo ---
echo "[2/7] Creando Firestore (modo nativo)..."
gcloud firestore databases create \
  --location="$REGION" \
  --project="$PROJECT" \
  --quiet || echo "    Firestore ya existe, continuando."

# --- 3. Crear service account para GitHub Actions ---
echo "[3/7] Creando service account github-deployer..."
gcloud iam service-accounts create github-deployer \
  --display-name="GitHub Actions Deployer" \
  --project="$PROJECT" \
  --quiet || echo "    Service account ya existe, continuando."

SA_EMAIL="github-deployer@${PROJECT}.iam.gserviceaccount.com"

# Asignar los 5 roles necesarios para CI/CD
echo "    Asignando roles..."
for ROLE in \
  roles/run.developer \
  roles/iam.serviceAccountUser \
  roles/storage.admin \
  roles/cloudbuild.builds.editor \
  roles/firebase.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" \
    --quiet
done
echo "    Roles asignados."

# --- 4. Conceder acceso Firestore al Compute Engine default SA (usado por Cloud Run) ---
echo "[4/7] Concediendo acceso Firestore a Cloud Run..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${COMPUTE_SA}" \
  --role="roles/datastore.user" \
  --quiet
echo "    Acceso Firestore concedido a ${COMPUTE_SA}."

# --- 5. Exportar credenciales del service account para GitHub Secret GCP_SA_KEY ---
echo "[5/7] Exportando credenciales de github-deployer..."
KEY_FILE="gcp-sa-key.json"
gcloud iam service-accounts keys create "$KEY_FILE" \
  --iam-account="$SA_EMAIL" \
  --project="$PROJECT"
echo ""
echo "    IMPORTANTE: Copia el contenido de $KEY_FILE como valor del GitHub Secret GCP_SA_KEY"
echo "    Luego borra el archivo: rm $KEY_FILE"
echo ""

# --- 6. Deployar Firestore security rules ---
echo "[6/7] Deployando Firestore security rules..."
npx --yes firebase-tools deploy \
  --only firestore:rules \
  --project "$PROJECT" \
  --non-interactive
echo "    Firestore rules deployadas."

# --- 7. Instrucciones del primer deploy ---
echo ""
echo "[7/7] Instrucciones para el primer deploy:"
echo ""
echo "  1. Copia GCP_SA_KEY de $KEY_FILE a GitHub Secrets"
echo "  2. Configura todos los GitHub Secrets del .env.example"
echo "  3. Ejecuta en orden:"
echo "     make deploy-bot"
echo "     export TELEGRAM_BOT_URL=\$(gcloud run services describe telegram-bot --format='value(status.url)' --region=$REGION)"
echo "     # Anadir TELEGRAM_BOT_URL como GitHub Secret"
echo "     make deploy-sports deploy-poly deploy-dashboard"
echo "     make set-webhook"
echo ""
echo "  4. Configura webhook de Telegram manualmente o con: make set-webhook"
echo "  5. Ejecuta backtesting inicial (UNA SOLA VEZ):"
echo "     curl -X POST \$SPORTS_AGENT_URL/run-backtest -H 'x-cloud-token: \$CLOUD_RUN_TOKEN'"
echo "     curl -X POST \$POLY_AGENT_URL/run-poly-backtest -H 'x-cloud-token: \$CLOUD_RUN_TOKEN'"
echo ""
echo "=== Setup completado ==="
