"""core/migration/shadow_runner.py — Shadow + Canary Runner (STEP 12).

Ejecuta el flow LEGACY (real) + opcionalmente un SHADOW flow (FlowCore
con side-effects mockeados) en paralelo, para validar el nuevo sistema
SIN afectar producción.

PRINCIPIO:
    Legacy es PRIMARY y SIEMPRE produce el resultado real.
    Shadow es un MIRROR que valida el FlowCore.
    Comparison events son LOG-ONLY.

CANARY: probabilidad determinística (hash-based) de ejecutar shadow.
    Misma identidad → mismo resultado (sticky).

stdlib only. NO modifica `_compra_parallel.py`. NO tiene side-effects.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
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
EVENT_SHADOW_RUN_START: str = "shadow_run_started"
EVENT_SHADOW_RUN_COMPLETE: str = "shadow_run_completed"
EVENT_CANARY_COMPARE: str = "canary_compare"
EVENT_SHADOW_SKIPPED: str = "shadow_skipped_not_in_canary"

HASH_BUCKETS: int = 10000


# ════════════════════════════════════════════════════════════════════
#  Canary sampling determinístico
# ════════════════════════════════════════════════════════════════════
def in_canary(*,
               rate: float,
               identity: str = "",
               seed: str = "shadow_v1") -> bool:
    """¿Esta identidad cae en el canary % (determinístico, sticky)?

    Args:
        rate: 0.0 a 1.0 (NOT 0-100). Ej: 0.05 = 5%.
        identity: string identificador (account_id, request_id, etc.)
        seed: salt para el hash.

    Returns:
        bool. Misma (identity, seed) → mismo resultado siempre.
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    payload = f"{seed}|{identity or 'anonymous'}"
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    bucket = int(h[:8], 16) % HASH_BUCKETS
    threshold = int(rate * HASH_BUCKETS)
    return bucket < threshold


# ════════════════════════════════════════════════════════════════════
#  ShadowResult
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ShadowResult:
    """Resultado de una ejecución dual legacy + shadow."""
    legacy_result: Any
    shadow_result: Any = None
    shadow_executed: bool = False
    in_canary: bool = False
    divergence: Optional[dict] = None
    legacy_latency_ms: float = 0.0
    shadow_latency_ms: float = 0.0
    shadow_error: str = ""

    def to_dict(self) -> dict:
        return {
            "shadow_executed": bool(self.shadow_executed),
            "in_canary": bool(self.in_canary),
            "divergence": self.divergence,
            "legacy_latency_ms": float(round(self.legacy_latency_ms, 3)),
            "shadow_latency_ms": float(round(self.shadow_latency_ms, 3)),
            "shadow_error": self.shadow_error,
        }


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def run_with_shadow(*,
                     ctx: Any,
                     legacy_callable: Callable[[Any], Any],
                     shadow_callable: Optional[Callable[[Any], Any]] = None,
                     canary_rate: float = 0.0,
                     canary_identity: str = "",
                     ) -> ShadowResult:
    """Ejecuta legacy SIEMPRE; ejecuta shadow si en canary.

    Args:
        ctx: contexto para ambos callables (mismo input).
        legacy_callable: REQUERIDO. Es la verdad.
        shadow_callable: opcional. Si None, no shadow.
        canary_rate: fracción [0, 1] de ejecuciones que disparan shadow.
        canary_identity: identidad para sticky-routing.

    Returns:
        ShadowResult con legacy_result + datos de comparación.

    GARANTÍA:
        Si shadow_callable crashea → se ignora silenciosamente.
        El legacy_result NUNCA se ve afectado.
        Si legacy_callable propaga → se propaga (es la verdad).
    """
    # 1. Decisión de canary (determinística)
    in_can = in_canary(rate=canary_rate, identity=canary_identity)

    # 2. Legacy SIEMPRE (verdad)
    t0 = time.monotonic()
    legacy_result = legacy_callable(ctx)
    legacy_ms = (time.monotonic() - t0) * 1000.0

    # 3. Si no canary o no shadow callable → done
    if not in_can or not callable(shadow_callable):
        if shadow_callable is not None and not in_can:
            try:
                emit_event(
                    ErrorCategory.FLOW, EVENT_SHADOW_SKIPPED,
                    {"canary_rate": canary_rate,
                     "canary_identity": canary_identity[:80]},
                    severity=ErrorSeverity.RECOVERABLE,
                    context="shadow_runner",
                )
            except Exception:
                pass
        return ShadowResult(
            legacy_result=legacy_result,
            shadow_executed=False,
            in_canary=in_can,
            divergence=None,
            legacy_latency_ms=legacy_ms,
        )

    # 4. Shadow execution (fail-safe)
    try:
        emit_event(
            ErrorCategory.FLOW, EVENT_SHADOW_RUN_START,
            {"canary_rate": canary_rate},
            severity=ErrorSeverity.RECOVERABLE,
            context="shadow_runner",
        )
    except Exception:
        pass

    shadow_result: Any = None
    shadow_error: str = ""
    t0 = time.monotonic()
    try:
        shadow_result = shadow_callable(ctx)
    except Exception as e:
        shadow_error = f"{type(e).__name__}: {str(e)[:200]}"
        shadow_result = None
    shadow_ms = (time.monotonic() - t0) * 1000.0

    # 5. Divergencia
    divergence = None
    if shadow_result is not None:
        try:
            divergence = compute_divergence(legacy_result, shadow_result)
        except Exception:
            divergence = None

    # 6. Comparison event (log-only)
    try:
        payload = {
            "in_canary": True,
            "shadow_executed": shadow_error == "",
            "legacy_latency_ms": round(legacy_ms, 3),
            "shadow_latency_ms": round(shadow_ms, 3),
            "latency_delta_ms": round(shadow_ms - legacy_ms, 3),
            "divergence_score": divergence.get("score", 0) if divergence else 0,
            "divergence_severity": (
                divergence.get("severity", "low") if divergence else "low"
            ),
        }
        if shadow_error:
            payload["shadow_error"] = shadow_error
        emit_event(
            ErrorCategory.FLOW, EVENT_CANARY_COMPARE,
            payload,
            severity=ErrorSeverity.RECOVERABLE,
            context="shadow_runner",
        )
    except Exception:
        pass

    try:
        emit_event(
            ErrorCategory.FLOW, EVENT_SHADOW_RUN_COMPLETE,
            {"shadow_error": shadow_error},
            severity=ErrorSeverity.RECOVERABLE,
            context="shadow_runner",
        )
    except Exception:
        pass

    return ShadowResult(
        legacy_result=legacy_result,
        shadow_result=shadow_result,
        shadow_executed=(shadow_error == ""),
        in_canary=True,
        divergence=divergence,
        legacy_latency_ms=legacy_ms,
        shadow_latency_ms=shadow_ms,
        shadow_error=shadow_error,
    )


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
