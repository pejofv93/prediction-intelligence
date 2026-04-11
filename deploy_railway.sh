#!/usr/bin/env bash
# deploy_railway.sh — Desplegar NEXUS en Railway
# Ejecutar: bash deploy_railway.sh

set -e

echo "=== NEXUS → Railway Deploy ==="
echo ""
echo "PASO 1: Verificar Railway CLI"
if ! command -v railway &>/dev/null; then
    echo "Instalando Railway CLI..."
    npm install -g @railway/cli 2>/dev/null || \
    curl -fsSL https://railway.app/install.sh | sh
fi
railway --version

echo ""
echo "PASO 2: Login"
railway login

echo ""
echo "PASO 3: Inicializar proyecto (si es nuevo)"
railway init || true

echo ""
echo "PASO 4: Configurar variables de entorno en Railway"
# Leer del .env local y subir a Railway
if [ -f .env ]; then
    echo "Subiendo variables desde .env..."
    while IFS='=' read -r key value; do
        [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
        railway variables set "$key=$value" 2>/dev/null && echo "  ✓ $key" || true
    done < .env
fi

# Variables adicionales para producción
railway variables set NEXUS_ENV=production
railway variables set PYTHONIOENCODING=utf-8
railway variables set PYTHONUTF8=1

echo ""
echo "PASO 5: Deploy"
railway up --detach

echo ""
echo "PASO 6: Ver logs"
railway logs

echo ""
echo "=== Deploy completado ==="
echo "Panel Railway: https://railway.app/dashboard"
