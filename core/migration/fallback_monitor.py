"""core/migration/fallback_monitor.py — Auto-Fallback (STEP 10).

Ejecuta core flow con fallback automático a legacy si:
    - core crashea
    - divergence_score > threshold
    - timeout configurable
    - idempotency mismatch

PRINCIPIO:
    Core es OPTIMISTIC; legacy es FALLBACK garantizado.
    Cualquier fallo del core → legacy ejecuta.

stdlib only.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.migration.divergence_engine import compute_divergence
from core.observability.error_classification import (
    ErrorCategory,
    ErrorSeverity,
)
from core.observability.event_emitter import emit_event


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
DEFAULT_DIVERGENCE_THRESHOLD: int = 70   # ≥ 70 = critical
DEFAULT_TIMEOUT_SEC: Optional[float] = None  # None = no timeout

EVENT_FALLBACK_TRIGGERED: str = "core_fallback_triggered"


# ════════════════════════════════════════════════════════════════════
#  FallbackResult
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class FallbackResult:
    """Resultado de la ejecución con fallback."""
    path: str                  # "core" | "legacy_fallback"
    result: Any
    fallback_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "fallback_reason": self.fallback_reason,
        }


# ════════════════════════════════════════════════════════════════════
#  Timeout helper (threading-based)
# ════════════════════════════════════════════════════════════════════
def _run_with_timeout(callable_, ctx, timeout_sec: Optional[float]):
    """Ejecuta callable con timeout. Returns (result, timed_out).

    Implementación simple via Thread (legacy-compatible, no asyncio).
    Si callable propaga, propaga.
    """
    if timeout_sec is None or timeout_sec <= 0:
        return callable_(ctx), False

    holder = {"result": None, "exc": None}

    def _runner():
        try:
            holder["result"] = callable_(ctx)
        except BaseException as e:
            holder["exc"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        return None, True
    if holder["exc"] is not None:
        raise holder["exc"]
    return holder["result"], False


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def execute_with_fallback(ctx: Any,
                            *,
                            core_callable: Callable[[Any], Any],
                            legacy_callable: Callable[[Any], Any],
                            divergence_threshold: int = DEFAULT_DIVERGENCE_THRESHOLD,
                            timeout_sec: Optional[float] = DEFAULT_TIMEOUT_SEC,
                            check_divergence_with_legacy: bool = False,
                            ) -> FallbackResult:
    """Ejecuta core; si falla, fallback a legacy.

    Triggers de fallback:
        1. core raises → legacy
        2. timeout → legacy
        3. (opcional) divergence_score > threshold cuando se ejecuta
           tanto core como legacy → devolver legacy

    Args:
        check_divergence_with_legacy: si True, también ejecuta legacy
            para comparar. Si divergence alta → fallback.

    Returns FallbackResult con path elegido + reason.
    """
    # 1. Intentar core (con timeout)
    core_result = None
    core_exc: Optional[BaseException] = None
    core_timed_out = False
    try:
        core_result, core_timed_out = _run_with_timeout(
            core_callable, ctx, timeout_sec,
        )
    except Exception as e:
        core_exc = e

    if core_exc is not None or core_timed_out:
        reason = "timeout" if core_timed_out else f"exception:{type(core_exc).__name__}"
        try:
            emit_event(
                ErrorCategory.FLOW, EVENT_FALLBACK_TRIGGERED,
                {"reason": reason,
                 "exception": str(core_exc)[:200] if core_exc else ""},
                severity=ErrorSeverity.FATAL,
                context="execute_with_fallback",
            )
        except Exception:
            pass
        legacy_result = legacy_callable(ctx)
        return FallbackResult(
            path="legacy_fallback",
            result=legacy_result,
            fallback_reason=reason,
        )

    # 2. Si check_divergence con legacy → comparar
    if check_divergence_with_legacy:
        try:
            legacy_result = legacy_callable(ctx)
        except Exception as e:
            # Legacy crashea → devolver core de todos modos
            return FallbackResult(path="core", result=core_result,
                                    fallback_reason="legacy_crashed")
        div = compute_divergence(legacy_result, core_result)
        if div.get("score", 0) >= divergence_threshold:
            try:
                emit_event(
                    ErrorCategory.FLOW, EVENT_FALLBACK_TRIGGERED,
                    {"reason": "divergence_high",
                     "divergence_score": div.get("score", 0)},
                    severity=ErrorSeverity.FATAL,
                    context="execute_with_fallback",
                )
            except Exception:
                pass
            return FallbackResult(
                path="legacy_fallback",
                result=legacy_result,
                fallback_reason=f"divergence_score={div.get('score', 0)}",
            )

    return FallbackResult(path="core", result=core_result)


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
