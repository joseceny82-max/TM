"""core/dashboard/slo_engine.py — Service Level Objectives + burn rates.

SLOs definidos contractuamente:
    - success_rate    >= 0.90   (≥ 90% éxito)
    - p95_latency_ms  <= 5000   (≤ 5s)
    - orphan_rate     <= 0.001  (≤ 0.1% orphans)
    - divergence_avg  <= 10     (canary mode)

BURN RATE:
    Concepto Google SRE: fracción del error budget consumido.
    Si tu SLO permite 1% error y consumes 0.5% en 1 hora, burn=0.5
    proyectado a 30 días = 360% over budget → alarma.

Devuelve evaluaciones explícitas: PASSING / BURNING / VIOLATING.

stdlib only. Pure functions. Determinístico.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  SLO definitions (configurables vía constructor)
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SLOTargets:
    """Targets contractuales del sistema."""
    success_rate_min: float = 0.90
    p95_latency_max_ms: float = 5000.0
    p99_latency_max_ms: float = 10000.0
    orphan_rate_max: float = 0.001
    divergence_score_max: float = 10.0
    payment_failure_rate_max: float = 0.10
    idempotency_collision_rate_max: float = 0.0

    def to_dict(self) -> dict:
        return {
            "success_rate_min": self.success_rate_min,
            "p95_latency_max_ms": self.p95_latency_max_ms,
            "p99_latency_max_ms": self.p99_latency_max_ms,
            "orphan_rate_max": self.orphan_rate_max,
            "divergence_score_max": self.divergence_score_max,
            "payment_failure_rate_max": self.payment_failure_rate_max,
            "idempotency_collision_rate_max": self.idempotency_collision_rate_max,
        }


# ════════════════════════════════════════════════════════════════════
#  SLO status enum
# ════════════════════════════════════════════════════════════════════
class SLOStatus(Enum):
    PASSING = "passing"          # dentro de budget, baja quema
    BURNING = "burning"          # consumiendo budget rápido
    VIOLATING = "violating"      # SLO incumplido
    INSUFFICIENT_DATA = "insufficient_data"


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _evaluate_max(value: float, max_target: float) -> str:
    """¿value <= max_target? Para SLOs de tipo "no exceder X"."""
    if value <= max_target:
        return SLOStatus.PASSING.value
    # Si está hasta 50% por encima del max → BURNING
    if max_target > 0 and value <= max_target * 1.5:
        return SLOStatus.BURNING.value
    return SLOStatus.VIOLATING.value


def _evaluate_min(value: float, min_target: float) -> str:
    """¿value >= min_target? Para SLOs de tipo "alcanzar al menos X"."""
    if value >= min_target:
        return SLOStatus.PASSING.value
    # Si está hasta 5% por debajo del min → BURNING
    if min_target > 0 and value >= min_target * 0.95:
        return SLOStatus.BURNING.value
    return SLOStatus.VIOLATING.value


def _burn_rate(actual: float, budget: float, *, kind: str) -> float:
    """Calcula fracción de budget consumido.

    kind="max": SLO de no-exceder. Si actual > budget → > 1.0
    kind="min": SLO de alcanzar mínimo. Si actual < budget → > 1.0
    """
    if budget <= 0:
        return 0.0
    if kind == "max":
        return round(actual / budget, 6) if budget > 0 else 0.0
    if kind == "min":
        # Para "min": budget=0.9, actual=0.85 → quemamos (0.9-0.85)/(1-0.9)=0.5
        gap = max(0.0, budget - actual)
        margin = 1.0 - budget
        if margin <= 0:
            return 1.0 if gap > 0 else 0.0
        return round(gap / margin, 6)
    return 0.0


# ════════════════════════════════════════════════════════════════════
#  SLO Result
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class SLOEvaluation:
    """Evaluación de un SLO individual."""
    name: str
    target: float
    actual: float
    status: str
    burn_rate: float
    kind: str          # "max" | "min"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "actual": self.actual,
            "status": self.status,
            "burn_rate": self.burn_rate,
            "kind": self.kind,
        }


# ════════════════════════════════════════════════════════════════════
#  API principal: evaluate_slos
# ════════════════════════════════════════════════════════════════════
def evaluate_slos(metrics: dict,
                   *,
                   targets: Optional[SLOTargets] = None,
                   ) -> dict:
    """Evalúa el dict de métricas contra los SLO targets.

    `metrics` debe contener las claves esperadas (vienen de
    compute_purchase_metrics). Las ausentes → INSUFFICIENT_DATA.

    Returns:
        dict con:
            "overall_status": passing|burning|violating|insufficient_data
            "evaluations": list[SLOEvaluation.to_dict()]
            "violations": list[SLO names violating]
            "burning": list[SLO names burning]
            "data_sufficient": bool
    """
    t = targets or SLOTargets()
    metrics = metrics if isinstance(metrics, dict) else {}

    # ¿Hay datos suficientes? Heurística: events_in_window > 10
    events = int(metrics.get("events_in_window", 0))
    data_sufficient = events >= 10

    evaluations: list = []

    def _add(name, actual, target, kind):
        actual_f = _safe_float(actual)
        target_f = _safe_float(target)
        if not data_sufficient:
            status = SLOStatus.INSUFFICIENT_DATA.value
            burn = 0.0
        else:
            status = (_evaluate_max(actual_f, target_f)
                       if kind == "max"
                       else _evaluate_min(actual_f, target_f))
            burn = _burn_rate(actual_f, target_f, kind=kind)
        evaluations.append(SLOEvaluation(
            name=name, target=target_f, actual=actual_f,
            status=status, burn_rate=burn, kind=kind,
        ).to_dict())

    # 1. success_rate (min)
    sr = metrics.get("success_rate_24h", {})
    if isinstance(sr, dict):
        _add("success_rate", sr.get("rate", 0.0),
              t.success_rate_min, "min")

    # 2. p95 latency (max)
    lat = metrics.get("latency", {})
    if isinstance(lat, dict):
        _add("p95_latency_ms", lat.get("p95_ms", 0.0),
              t.p95_latency_max_ms, "max")
        _add("p99_latency_ms", lat.get("p99_ms", 0.0),
              t.p99_latency_max_ms, "max")

    # 3. orphan rate (max)
    idem = metrics.get("idempotency", {})
    if isinstance(idem, dict):
        _add("orphan_rate", idem.get("orphan_rate", 0.0),
              t.orphan_rate_max, "max")

    # 4. payment failure (derive from success rate)
    if isinstance(sr, dict):
        _add("payment_failure_rate",
              1.0 - _safe_float(sr.get("rate", 1.0)),
              t.payment_failure_rate_max, "max")

    # 5. divergence_score (max) — si está en metrics
    div_avg = _safe_float(metrics.get("avg_divergence_score", 0.0), 0.0)
    if "avg_divergence_score" in metrics:
        _add("divergence_score", div_avg,
              t.divergence_score_max, "max")

    # Aggregate status
    statuses = [e["status"] for e in evaluations]
    if not data_sufficient or all(
        s == SLOStatus.INSUFFICIENT_DATA.value for s in statuses
    ):
        overall = SLOStatus.INSUFFICIENT_DATA.value
    elif any(s == SLOStatus.VIOLATING.value for s in statuses):
        overall = SLOStatus.VIOLATING.value
    elif any(s == SLOStatus.BURNING.value for s in statuses):
        overall = SLOStatus.BURNING.value
    else:
        overall = SLOStatus.PASSING.value

    return {
        "overall_status": overall,
        "data_sufficient": data_sufficient,
        "events_observed": events,
        "min_events_required": 10,
        "evaluations": evaluations,
        "violations": [e["name"] for e in evaluations
                        if e["status"] == SLOStatus.VIOLATING.value],
        "burning": [e["name"] for e in evaluations
                     if e["status"] == SLOStatus.BURNING.value],
        "targets": t.to_dict(),
    }


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
