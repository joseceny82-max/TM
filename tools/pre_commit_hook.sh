#!/usr/bin/env bash
# tools/pre_commit_hook.sh — Hook pre-commit que rechaza nuevos *.py sin self_test().
#
# Instalación:
#   ln -sf ../../tools/pre_commit_hook.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# Skip puntual:
#   git commit --no-verify  (úsalo SOLO para hotfixes urgentes)
#
# Reglas:
#   - Aplica a archivos *.py NUEVOS (status A) en staging
#   - Excluye:
#       _*.py        (scripts de utilidad)
#       tests/*.py   (tests pytest)
#       __init__.py  (módulos vacíos)
#       conftest.py  (config pytest)
#   - Acepta el archivo si contiene "def self_test(" o si es <50 LOC
#     (módulos minúsculos como helpers no necesitan self_test)
#
# Salida:
#   0  → todo OK, commit prosigue
#   1  → faltan self_test() → bloquea commit con mensaje

set -e

# Colores
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Solo archivos PYTHON añadidos (A) o modificados (M) en staging
NEW_FILES=$(git diff --cached --name-only --diff-filter=AM | grep -E '\.py$' || true)

if [ -z "$NEW_FILES" ]; then
    exit 0
fi

OFFENDERS=()
for f in $NEW_FILES; do
    bn=$(basename "$f")

    # Excludes
    case "$bn" in
        _*) continue ;;
        __init__.py) continue ;;
        conftest.py) continue ;;
    esac
    case "$f" in
        tests/*) continue ;;
        tools/*) continue ;;
        staging/*) continue ;;
        .github/*) continue ;;
    esac

    # No existe = ya borrado
    [ -f "$f" ] || continue

    # LOC < 50 → exento (helpers minúsculos)
    loc=$(wc -l < "$f")
    if [ "$loc" -lt 50 ]; then
        continue
    fi

    # Verificar self_test
    if ! grep -qE '^def self_test\(' "$f"; then
        OFFENDERS+=("$f ($loc LOC)")
    fi
done

if [ ${#OFFENDERS[@]} -gt 0 ]; then
    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}  ❌ COMMIT BLOQUEADO: archivos sin def self_test()${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    for f in "${OFFENDERS[@]}"; do
        echo -e "  ${YELLOW}•${NC} $f"
    done
    echo ""
    echo -e "${YELLOW}Política de proyecto: módulos >50 LOC deben tener una función${NC}"
    echo -e "${YELLOW}\`def self_test() -> dict\` que valide su funcionamiento básico.${NC}"
    echo ""
    echo -e "Skip puntual: ${GREEN}git commit --no-verify${NC} (solo hotfixes urgentes)"
    echo ""
    exit 1
fi

echo -e "${GREEN}[pre-commit] OK${NC} (${#NEW_FILES[@]} archivos validados)"
exit 0
