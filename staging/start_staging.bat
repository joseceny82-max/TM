@echo off
REM staging\start_staging.bat — Equivalente Windows de start_staging.sh

cd /d "%~dp0\.."

echo ===================================================
echo   TM PERROS - Staging Environment
echo ===================================================

if not exist "staging_data\events.json" (
    echo.
    echo Seeding staging data...
    python staging\seed_synthetic_data.py
)

set TM_ENV=staging
set TM_PORT=5056
set TM_DATA_DIR=staging_data
set TM_AUTO_WARMING=0
set TM_IMAP_MADRE_PWD=
set TM_SECRET_KEY=staging-key-change-me-%RANDOM%

echo.
echo Configuracion:
echo   TM_ENV         = %TM_ENV%
echo   TM_PORT        = %TM_PORT%
echo   TM_DATA_DIR    = %TM_DATA_DIR%
echo   TM_AUTO_WARMING= %TM_AUTO_WARMING%
echo.
echo Dashboard staging arrancando en http://127.0.0.1:5056
echo Login: admin / admin
echo.

python dashboard.py
