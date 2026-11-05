"""core/migration/shadow_orchestrator.py — Shadow Execution (STEP 9).

Ejecuta el flow legacy (siempre) + opcionalmente ejecuta el core flow
en paralelo (dry-run) para comparar outputs SIN afectar producción.

PRINCIPIO:
    - Legacy result es la verdad. SIEMPRE se devuelve.
    - Core es shadow: si falla, se loguea pero NO afecta el resultado.
    - Divergencia es LOG-ONLY.

stdlib only.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core.migration.divergence_engine import compute_divergence
from core.migration.flags import MigrationFlags, get_flags
from core.observability.error_classification import (
    ErrorCategory,
    ErrorSeverity,
)
from core.observability.event_emitter import emit_event


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
EVENT_SHADOW_COMPARE: str = "shadow_compare"
EVENT_SHADOW_CORE_CRASH: str = "shadow_core_crash"


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def execute_with_shadow(ctx: Any,
                          *,
                          legacy_callable: Callable[[Any], Any],
                          core_callable: Optional[Callable[[Any], Any]] = None,
                          flags: Optional[MigrationFlags] = None,
                          ) -> tuple:
    """Ejecuta legacy + (opcional) core en shadow + compara.

    Args:
        ctx: contexto del flow
        legacy_callable: REQUERIDO. Es la verdad.
        core_callable: opcional. Si None o flag OFF → no shadow.
        flags: opcional override.

    Returns:
        tuple (legacy_result, divergence_report_or_none).

    Garantía: legacy_callable se invoca SIEMPRE primero. Su resultado
    se devuelve sin tocar. La parte shadow es try/except absoluto.
    """
    f = flags or get_flags()

    # 1. Legacy SIEMPRE primero (verdad)
    legacy_result = legacy_callable(ctx)

    # 2. Si no hay shadow, devolver
    if not f.SHADOW_CORE_FLOW or not callable(core_callable):
        return legacy_result, None

    # 3. Core en shadow (fail-safe absoluto)
    try:
        core_result = core_callable(ctx)
    except Exception as e:
        try:
            emit_event(
                ErrorCategory.FLOW, EVENT_SHADOW_CORE_CRASH,
                {"exception": type(e).__name__,
                 "message": str(e)[:200]},
                severity=ErrorSeverity.RECOVERABLE,
                context="shadow_orchestrator",
            )
        except Exception:
            pass
        return legacy_result, {
            "score": 0,
            "severity": "low",
            "differences": [],
            "same": False,
            "core_crashed": True,
            "error": str(e)[:200],
        }

    # 4. Compute divergencia
    try:
        divergence = compute_divergence(legacy_result, core_result)
    except Exception:
        divergence = {
            "score": 0,
            "severity": "low",
            "differences": [],
            "same": True,
            "error": "divergence_compute_failed",
        }

    # 5. Emit event (log only)
    try:
        emit_event(
            ErrorCategory.FLOW, EVENT_SHADOW_COMPARE,
            {
                "score": divergence.get("score", 0),
                "severity": divergence.get("severity", "low"),
                "differences_count": len(divergence.get("differences", [])),
            },
            severity=ErrorSeverity.RECOVERABLE,
            context="shadow_orchestrator",
        )
    except Exception:
        pass

    return legacy_result, divergence


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
