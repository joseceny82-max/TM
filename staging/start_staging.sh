#!/usr/bin/env bash
# staging/start_staging.sh — Arranca dashboard en :5056 con datos sintéticos.
# Aísla por completo de producción (datos en staging_data/, port distinto).

set -e

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════"
echo "  TM PERROS — Staging Environment"
echo "═══════════════════════════════════════════════════"

# 1. Seed staging data si no existe
if [ ! -f "staging_data/events.json" ]; then
    echo ""
    echo "Seeding staging data..."
    python staging/seed_synthetic_data.py
fi

# 2. Configurar env vars
export TM_ENV=staging
export TM_PORT=5056
export TM_DATA_DIR=staging_data
export TM_AUTO_WARMING=0          # No warming en staging
export TM_IMAP_MADRE_PWD=         # Sin IMAP real en staging
export TM_SECRET_KEY=staging-key-change-me-${RANDOM}

echo ""
echo "Configuración:"
echo "  TM_ENV         = $TM_ENV"
echo "  TM_PORT        = $TM_PORT"
echo "  TM_DATA_DIR    = $TM_DATA_DIR"
echo "  TM_AUTO_WARMING= $TM_AUTO_WARMING"
echo ""
echo "Dashboard staging arrancando en http://127.0.0.1:5056"
echo "Login: admin / admin"
echo ""

# 3. Arrancar dashboard
exec python dashboard.py
