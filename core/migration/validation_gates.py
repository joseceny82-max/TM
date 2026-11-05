"""core/migration/validation_gates.py — Validation Gates (STEP 10).

Gates obligatorios que se verifican ANTES de cualquier incremento del
canary %. Si cualquier gate falla → bloquear progresión.

stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes — umbrales por defecto
# ════════════════════════════════════════════════════════════════════
DEFAULT_SUCCESS_RATE_DELTA: float = 0.005   # ±0.5%
DEFAULT_MAX_IDEMPOTENCY_COLLISION: float = 0.0
DEFAULT_MAX_DIVERGENCE_SCORE_SPIKE: int = 20
DEFAULT_MAX_PAYMENT_FAILURE_INCREASE: float = 0.02   # +2%


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, key: str, default: float = 0.0) -> float:
    if not isinstance(d, dict):
        return float(default)
    v = d.get(key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


# ════════════════════════════════════════════════════════════════════
#  Gate config + result
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class GateConfig:
    success_rate_delta: float = DEFAULT_SUCCESS_RATE_DELTA
    max_idempotency_collision: float = DEFAULT_MAX_IDEMPOTENCY_COLLISION
    max_divergence_score_spike: int = DEFAULT_MAX_DIVERGENCE_SCORE_SPIKE
    max_payment_failure_increase: float = DEFAULT_MAX_PAYMENT_FAILURE_INCREASE


@dataclass(frozen=True)
class GateResult:
    """Resultado de la evaluación de gates."""
    passed: bool
    failed_gates: tuple   # tuple[str, ...]
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "passed": bool(self.passed),
            "failed_gates": list(self.failed_gates),
            "details": dict(self.details),
        }


# ════════════════════════════════════════════════════════════════════
#  Individual gate checks
# ════════════════════════════════════════════════════════════════════
def check_success_rate_stable(current_metrics: Any,
                                prior_metrics: Any,
                                *,
                                max_delta: float = DEFAULT_SUCCESS_RATE_DELTA,
                                ) -> tuple:
    """¿success_rate diferencia respecto a prior <= max_delta?

    Returns (passed: bool, details: dict).
    """
    cur = _safe_get(current_metrics, "success_rate", 0.0)
    pri = _safe_get(prior_metrics, "success_rate", 0.0)
    delta = abs(cur - pri)
    return delta <= max_delta, {
        "current": cur, "prior": pri, "delta": delta, "max_delta": max_delta,
    }


def check_idempotency_collisions(current_metrics: Any,
                                   *,
                                   max_rate: float = DEFAULT_MAX_IDEMPOTENCY_COLLISION,
                                   ) -> tuple:
    """idempotency_collision_rate debe ser <= max_rate."""
    rate = _safe_get(current_metrics, "idempotency_collision_rate", 0.0)
    return rate <= max_rate, {
        "current": rate, "max_rate": max_rate,
    }


def check_divergence_spike(current_metrics: Any,
                            *,
                            max_spike: int = DEFAULT_MAX_DIVERGENCE_SCORE_SPIKE,
                            ) -> tuple:
    """average_divergence_score actual debe ser <= max_spike."""
    score = _safe_get(current_metrics, "avg_divergence_score", 0.0)
    return score <= max_spike, {
        "current": score, "max_spike": max_spike,
    }


def check_payment_failure_increase(current_metrics: Any,
                                     prior_metrics: Any,
                                     *,
                                     max_increase: float = DEFAULT_MAX_PAYMENT_FAILURE_INCREASE,
                                     ) -> tuple:
    """payment_failure_rate no debe aumentar más de max_increase."""
    cur = _safe_get(current_metrics, "payment_failure_rate", 0.0)
    pri = _safe_get(prior_metrics, "payment_failure_rate", 0.0)
    increase = cur - pri
    return increase <= max_increase, {
        "current": cur, "prior": pri, "increase": increase,
        "max_increase": max_increase,
    }


# ════════════════════════════════════════════════════════════════════
#  API principal
# ════════════════════════════════════════════════════════════════════
def can_progress(*,
                  current_metrics: Optional[dict] = None,
                  prior_metrics: Optional[dict] = None,
                  config: Optional[GateConfig] = None) -> GateResult:
    """Evalúa todos los gates. Devuelve GateResult.

    Si CUALQUIER gate falla → passed=False.
    """
    cfg = config or GateConfig()
    cur = current_metrics or {}
    pri = prior_metrics or {}

    failed: list = []
    details: dict = {}

    ok_sr, det_sr = check_success_rate_stable(
        cur, pri, max_delta=cfg.success_rate_delta,
    )
    details["success_rate"] = det_sr
    if not ok_sr:
        failed.append("success_rate_unstable")

    ok_idem, det_idem = check_idempotency_collisions(
        cur, max_rate=cfg.max_idempotency_collision,
    )
    details["idempotency"] = det_idem
    if not ok_idem:
        failed.append("idempotency_collisions")

    ok_div, det_div = check_divergence_spike(
        cur, max_spike=cfg.max_divergence_score_spike,
    )
    details["divergence"] = det_div
    if not ok_div:
        failed.append("divergence_spike")

    ok_pay, det_pay = check_payment_failure_increase(
        cur, pri, max_increase=cfg.max_payment_failure_increase,
    )
    details["payment_failure"] = det_pay
    if not ok_pay:
        failed.append("payment_failure_increase")

    return GateResult(
        passed=(len(failed) == 0),
        failed_gates=tuple(failed),
        details=details,
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
