"""core/config.py — Configuración base (placeholder).

FASE 2: estructura inicial. NO se mueven constantes aún del legacy.

Política de migración (FASE 4):
    - Constantes genéricas (timeouts, URLs base) van aquí
    - Constantes TM-specific (regex, selectores, BUDGET_RECOVERY_IMAP_S)
      van a `bot/constants.py`
    - SOLO timeouts/polls/retries genéricos en `core/utils/timeouts.py`

Mientras tanto, los módulos legacy siguen leyendo de `_compra_constants.py`.
"""

CONFIG: dict = {}
