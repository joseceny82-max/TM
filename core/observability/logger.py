"""core/observability/logger.py — Wrapper compatible (placeholder).

FASE 2: placeholder mínimo. En FASE 3 se conectará al módulo de logging
existente del proyecto sin romper el flujo actual.

API actual: solo `log_event` placeholder que imprime.
API futura (FASE 3):
    StructuredLogger    — clase con contexto persistente
    log_event           — helper standalone
    truncate            — helper para limitar strings
"""


def log_event(*args, **kwargs) -> None:
    """Placeholder wrapper compatible.

    FASE 3 conectará esto al `_compra_logger.log_event` real para evitar
    duplicación. Por ahora solo imprime — NO se usa desde el legacy.
    """
    print("[LOG_EVENT]", args, kwargs)
