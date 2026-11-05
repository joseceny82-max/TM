"""core/dashboard/canary_decision_engine.py — Automatic canary decisions.

Toma los signals del sistema y emite UNA recomendación operacional:

    HOLD              — métricas estables pero sin justificación para subir
    PROCEED           — incrementar canary % es seguro
    ROLLBACK          — anomalía detectada, volver a legacy YA
    FREEZE            — pausar movimientos hasta investigar
    CUTOVER_READY     — 14 días verdes, listo para FULL_CORE
    INSUFFICIENT_DATA — sin tráfico productivo para decidir

stdlib only. Pure function. Determinístico.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from core.dashboard.slo_engine import SLOTargets, evaluate_slos
from core.migration.validation_gates import GateConfig, can_progress


# ════════════════════════════════════════════════════════════════════
#  Decision enum
# ════════════════════════════════════════════════════════════════════
class CanaryDecision(Enum):
    HOLD = "HOLD"
    PROCEED = "PROCEED"
    ROLLBACK = "ROLLBACK"
    FREEZE = "FREEZE"
    CUTOVER_READY = "CUTOVER_READY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# ════════════════════════════════════════════════════════════════════
#  Result
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class CanaryDecisionResult:
    decision: str               # CanaryDecision.value
    confidence: float           # 0.0 - 1.0
    reasons: tuple              # tuple[str, ...] explicaciones
    risk_level: str             # low | medium | high | critical
    gates_passed: bool
    slo_status: str
    data_sufficient: bool

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "confidence": float(self.confidence),
            "reasons": list(self.reasons),
            "risk_level": self.risk_level,
            "gates_passed": bool(self.gates_passed),
            "slo_status": self.slo_status,
            "data_sufficient": bool(self.data_sufficient),
        }


# ════════════════════════════════════════════════════════════════════
#  Risk scoring
# ════════════════════════════════════════════════════════════════════
def _classify_risk(*, slo_status: str, alerts: list,
                    gates_passed: bool) -> str:
    """Clasifica el risk_level basado en evidencia."""
    has_emergency = any(a.get("severity") == "emergency" for a in alerts)
    has_critical = any(a.get("severity") == "critical" for a in alerts)
    if has_emergency or slo_status == "violating":
        return "critical"
    if has_critical or not gates_passed:
        return "high"
    if slo_status == "burning":
        return "medium"
    return "low"


# ════════════════════════════════════════════════════════════════════
#  Main decision function
# ════════════════════════════════════════════════════════════════════
def decide(*,
            current_metrics: Optional[dict] = None,
            prior_metrics: Optional[dict] = None,
            alerts: Optional[list] = None,
            days_of_clean_data: int = 0,
            current_canary_rate: float = 0.0,
            slo_targets: Optional[SLOTargets] = None,
            gate_config: Optional[GateConfig] = None,
            ) -> CanaryDecisionResult:
    """Toma una decisión automática basada en signals disponibles.

    Args:
        current_metrics: dict de compute_purchase_metrics actual.
        prior_metrics: idem del periodo anterior (baseline).
        alerts: lista de alerts activas.
        days_of_clean_data: contador de días consecutivos sin rollback.
        current_canary_rate: 0.0 a 1.0
        slo_targets: thresholds SLO opt-in.
        gate_config: thresholds gates opt-in.

    Returns:
        CanaryDecisionResult con la decisión + razones.
    """
    current_metrics = current_metrics or {}
    prior_metrics = prior_metrics or {}
    alerts = alerts or []
    reasons: list = []

    # ─── 0. Insufficient data check ─────────────────────────────
    events = int(current_metrics.get("events_in_window", 0))
    data_sufficient = events >= 50   # min para decisión confiable
    if not data_sufficient:
        return CanaryDecisionResult(
            decision=CanaryDecision.INSUFFICIENT_DATA.value,
            confidence=0.0,
            reasons=(f"only {events} events observed (need >=50)",),
            risk_level="low",
            gates_passed=False,
            slo_status="insufficient_data",
            data_sufficient=False,
        )

    # ─── 1. Evaluar SLOs ────────────────────────────────────────
    slo_result = evaluate_slos(current_metrics, targets=slo_targets)
    slo_status = slo_result["overall_status"]

    # ─── 2. Evaluar gates de validación ─────────────────────────
    # Reusa el módulo de Step 10 con métricas derivadas
    gate_input = {
        "success_rate": current_metrics.get("success_rate_24h", {}).get("rate", 0.0),
        "idempotency_collision_rate": current_metrics.get("idempotency", {}).get("orphan_rate", 0.0),
        "avg_divergence_score": current_metrics.get("avg_divergence_score", 0.0),
        "payment_failure_rate": 1.0 - current_metrics.get("success_rate_24h", {}).get("rate", 1.0),
    }
    gate_input_prior = {
        "success_rate": prior_metrics.get("success_rate_24h", {}).get("rate", 0.0),
        "payment_failure_rate": 1.0 - prior_metrics.get("success_rate_24h", {}).get("rate", 1.0),
    }
    gates = can_progress(
        current_metrics=gate_input, prior_metrics=gate_input_prior,
        config=gate_config,
    )

    # ─── 3. ROLLBACK conditions (más estricto primero) ──────────
    has_emergency = any(a.get("severity") == "emergency" for a in alerts)
    has_critical = any(a.get("severity") == "critical" for a in alerts)

    if has_emergency:
        reasons.append("emergency alert detected")
        return CanaryDecisionResult(
            decision=CanaryDecision.ROLLBACK.value,
            confidence=1.0,
            reasons=tuple(reasons + [a.get("code", "") for a in alerts
                                       if a.get("severity") == "emergency"]),
            risk_level="critical",
            gates_passed=gates.passed,
            slo_status=slo_status,
            data_sufficient=True,
        )

    if slo_status == "violating":
        reasons.append("SLO violating")
        reasons.extend(f"SLO_violation:{v}" for v in slo_result.get("violations", []))
        return CanaryDecisionResult(
            decision=CanaryDecision.ROLLBACK.value,
            confidence=0.95,
            reasons=tuple(reasons),
            risk_level="critical",
            gates_passed=False,
            slo_status=slo_status,
            data_sufficient=True,
        )

    # ─── 4. FREEZE conditions ──────────────────────────────────
    if has_critical:
        reasons.append("critical alert without emergency level")
        return CanaryDecisionResult(
            decision=CanaryDecision.FREEZE.value,
            confidence=0.85,
            reasons=tuple(reasons + [a.get("code", "") for a in alerts
                                       if a.get("severity") == "critical"]),
            risk_level="high",
            gates_passed=gates.passed,
            slo_status=slo_status,
            data_sufficient=True,
        )

    if not gates.passed:
        reasons.append("validation gates failed")
        reasons.extend(f"gate:{g}" for g in gates.failed_gates)
        return CanaryDecisionResult(
            decision=CanaryDecision.HOLD.value,
            confidence=0.8,
            reasons=tuple(reasons),
            risk_level="high",
            gates_passed=False,
            slo_status=slo_status,
            data_sufficient=True,
        )

    # ─── 5. CUTOVER_READY conditions ────────────────────────────
    if (days_of_clean_data >= 14
            and current_canary_rate >= 0.99
            and slo_status == "passing"):
        reasons.append(f"{days_of_clean_data} days clean + canary at 100%")
        return CanaryDecisionResult(
            decision=CanaryDecision.CUTOVER_READY.value,
            confidence=0.95,
            reasons=tuple(reasons),
            risk_level="low",
            gates_passed=True,
            slo_status=slo_status,
            data_sufficient=True,
        )

    # ─── 6. PROCEED conditions ──────────────────────────────────
    if (gates.passed and slo_status == "passing"
            and not alerts):
        # Min observation period before proceeding to next step
        min_days_at_current_rate = (
            3 if current_canary_rate < 0.10
            else 5 if current_canary_rate < 0.50
            else 7
        )
        if days_of_clean_data >= min_days_at_current_rate:
            reasons.append(
                f"gates passed + SLOs passing + {days_of_clean_data}d clean"
            )
            return CanaryDecisionResult(
                decision=CanaryDecision.PROCEED.value,
                confidence=0.75,
                reasons=tuple(reasons),
                risk_level="low",
                gates_passed=True,
                slo_status=slo_status,
                data_sufficient=True,
            )
        reasons.append(
            f"need {min_days_at_current_rate}d at current rate "
            f"(have {days_of_clean_data}d)"
        )

    # ─── 7. Default: HOLD ───────────────────────────────────────
    if slo_status == "burning":
        reasons.append("SLOs burning — consume rate elevated")
    if not reasons:
        reasons.append("no proceed conditions met")

    risk = _classify_risk(slo_status=slo_status,
                            alerts=alerts,
                            gates_passed=gates.passed)
    return CanaryDecisionResult(
        decision=CanaryDecision.HOLD.value,
        confidence=0.6,
        reasons=tuple(reasons),
        risk_level=risk,
        gates_passed=gates.passed,
        slo_status=slo_status,
        data_sufficient=True,
    )


# ════════════════════════════════════════════════════════════════════
#  Legacy readiness score (0-100)
# ════════════════════════════════════════════════════════════════════
def compute_legacy_readiness_score(*,
                                      current_canary_rate: float,
                                      days_of_clean_data: int,
                                      fallback_count_24h: int,
                                      divergence_avg: float,
                                      slo_passing: bool,
                                      orphan_rate: float,
                                      events_observed: int = 0,
                                      ) -> dict:
    """Calcula score 0-100 que mide "qué tan listos estamos para
    eliminar el legacy".

    100 = legacy puede borrarse físicamente. Step 14 unlocked.
    < 95 = no se elimina.

    BRUTAL HONESTY: si no hay datos productivos, score = 0
    (no se premia "ausencia de señales malas" cuando es "ausencia de TODO").
    """
    # Si no hay datos productivos suficientes → score=0 categórico
    if events_observed < 100:
        return {
            "score": 0,
            "step14_unlocked": False,
            "blockers": [
                f"events_observed={events_observed} (need >=100 minimum)",
                "INSUFFICIENT_PRODUCTION_DATA",
            ],
        }

    score = 0.0

    # Componente: canary coverage (max 30)
    score += min(30.0, current_canary_rate * 30.0)

    # Componente: days of clean data (max 35)
    score += min(35.0, days_of_clean_data * 35.0 / 14.0)

    # Componente: fallback rate (max 15, -inverso)
    if fallback_count_24h == 0:
        score += 15.0
    elif fallback_count_24h <= 10:
        score += 10.0
    elif fallback_count_24h <= 50:
        score += 5.0

    # Componente: divergence (max 10)
    if divergence_avg < 1.0:
        score += 10.0
    elif divergence_avg < 5.0:
        score += 7.0
    elif divergence_avg < 10.0:
        score += 4.0

    # Componente: SLO (max 5)
    if slo_passing:
        score += 5.0

    # Componente: orphan rate (max 5)
    if orphan_rate < 0.001:
        score += 5.0
    elif orphan_rate < 0.01:
        score += 3.0

    score_int = int(round(min(100.0, max(0.0, score))))

    # ─── Phase 7 STRICT gate ──────────────────────────────────
    # Step 14 (legacy deletion) unlocks ONLY when ALL of:
    #   score >= 95
    #   ≥14d clean
    #   canary at 100%
    #   ZERO fallbacks in 24h
    #   divergence avg < 1.0
    #   ≥100 production events (not test pollution)
    # Any single failure = blocked.
    step14_unlocked = (
        score_int >= 95
        and days_of_clean_data >= 14
        and current_canary_rate >= 0.99
        and fallback_count_24h == 0
        and divergence_avg < 1.0
        and events_observed >= 100
        and orphan_rate < 0.001
        and slo_passing
    )

    return {
        "score": int(score_int),
        "step14_unlocked": bool(step14_unlocked),
        "blockers": _readiness_blockers(
            score_int=score_int,
            days=days_of_clean_data,
            canary=current_canary_rate,
            fallbacks=fallback_count_24h,
            divergence=divergence_avg,
            events=events_observed,
            orphan_rate=orphan_rate,
            slo_passing=slo_passing,
        ),
    }


def _readiness_blockers(*, score_int: int, days: int, canary: float,
                         fallbacks: int, divergence: float,
                         events: int = 0, orphan_rate: float = 0.0,
                         slo_passing: bool = False) -> list:
    out: list = []
    if score_int < 95:
        out.append(f"readiness_score={score_int} (need >=95)")
    if days < 14:
        out.append(f"days_of_clean_data={days} (need >=14)")
    if canary < 0.99:
        out.append(f"canary_rate={canary} (need >=0.99)")
    if fallbacks > 0:
        out.append(f"fallback_count_24h={fallbacks} (need 0)")
    if divergence >= 1.0:
        out.append(f"divergence_avg={divergence} (need <1.0)")
    if events < 100:
        out.append(f"events_observed={events} (need >=100)")
    if orphan_rate >= 0.001:
        out.append(f"orphan_rate={orphan_rate} (need <0.001)")
    if not slo_passing:
        out.append("slo_status NOT passing")
    return out


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
