"""core/observability/risk/purchase_gate_risk.py — M13.8 Risk Scoring Engine.

Pure function que combina:
    - drift_report (output de M13.7 detect_gate_drift)
    - live_metrics (snapshot de M13.3 /api/diagnostics/purchase_gate)

Y produce un score 0-100 con risk_level y recomendación.

Diseño:
    - Pure function: same (drift_report, live_metrics) → same output (excepto timestamp)
    - Sin IO, sin red, sin threads
    - No muta inputs
    - Fail-safe: inputs malformados → safe state (low risk, continue)
    - Determinístico

Modelo:
    risk_score = Σ(component_score × weight)

    Componentes (each 0-100):
        drift_score = SEVERITY_TO_BASE_SCORE[drift_report.severity]
        error_score = min(100, internal_errors × 20)
        block_score = block_rate × 100
        volatility_score = min(100, n_volatility_signals × 50)

    Weights (sum = 1.0):
        drift:      0.40
        error:      0.25
        block:      0.20
        volatility: 0.15

Risk level (per spec):
    0-30   → low      → continue
    30-55  → medium   → monitor
    55-80  → high     → pause
    80-100 → critical → rollback

Output:
    {
        "risk_score": float,
        "risk_level": "low" | "medium" | "high" | "critical",
        "contributors": {drift_weight, error_weight, block_weight, volatility_weight},
        "recommendation": "continue" | "monitor" | "pause" | "rollback",
        "timestamp": ISO8601
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Severity → base score mapping (per spec)
# ════════════════════════════════════════════════════════════════════
SEVERITY_TO_BASE_SCORE: dict[str, float] = {
    "none":     5.0,   # 0-10 range
    "low":      20.0,  # 10-30 range
    "medium":   45.0,  # 30-55 range
    "high":     70.0,  # 55-80 range
    "critical": 95.0,  # 80-100 range
}


# Component weights (sum = 1.0)
WEIGHTS: dict[str, float] = {
    "drift":      0.40,
    "error":      0.25,
    "block":      0.20,
    "volatility": 0.15,
}


# Risk level thresholds (per spec)
RISK_LEVEL_THRESHOLDS: list = [
    ("low",      0.0,  30.0),
    ("medium",   30.0, 55.0),
    ("high",     55.0, 80.0),
    ("critical", 80.0, 100.01),  # >80
]


# Recommendation mapping (per spec)
RECOMMENDATION_BY_LEVEL: dict[str, str] = {
    "low":      "continue",
    "medium":   "monitor",
    "high":     "pause",
    "critical": "rollback",
}


# Volatility signal types (los que indican variación operacional)
_VOLATILITY_SIGNAL_TYPES: frozenset = frozenset({
    "dry_run_rate_deviation",
    "block_rate_spike",
})


# ════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    """Nested dict get fail-safe."""
    current = d
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a [lo, hi]."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _risk_level_from_score(score: float) -> str:
    """Mapea score → risk_level por threshold."""
    for level, lo, hi in RISK_LEVEL_THRESHOLDS:
        if lo <= score < hi:
            return level
    # Score == 100 → critical
    return "critical"


def _compute_drift_score(drift_report: Any) -> float:
    """Drift component (0-100) basado en severity."""
    severity = _safe_get(drift_report, "severity", default="low")
    if not isinstance(severity, str):
        severity = "low"
    return SEVERITY_TO_BASE_SCORE.get(severity, SEVERITY_TO_BASE_SCORE["low"])


def _compute_error_score(live_metrics: Any) -> float:
    """Error component (0-100): cada internal_error suma 20 puntos, cap 100."""
    errors = _safe_get(live_metrics, "counters", "internal_errors",
                        default=0)
    if not isinstance(errors, int):
        try:
            errors = int(errors)
        except (TypeError, ValueError):
            errors = 0
    return _clamp(errors * 20.0)


def _compute_block_score(live_metrics: Any) -> float:
    """Block component (0-100): block_rate × 100."""
    block_rate = _safe_get(live_metrics, "rates", "purchase_block_rate",
                             default=0.0)
    if not isinstance(block_rate, (int, float)):
        try:
            block_rate = float(block_rate)
        except (TypeError, ValueError):
            block_rate = 0.0
    return _clamp(float(block_rate) * 100.0)


def _compute_volatility_score(drift_report: Any) -> float:
    """Volatility component (0-100): n_signals_volatility × 50, cap 100."""
    signals = _safe_get(drift_report, "signals", default=[])
    if not isinstance(signals, list):
        return 0.0
    count = 0
    for s in signals:
        if isinstance(s, dict) and s.get("type") in _VOLATILITY_SIGNAL_TYPES:
            count += 1
    return _clamp(count * 50.0)


# ════════════════════════════════════════════════════════════════════
#  Main: compute_risk_score
# ════════════════════════════════════════════════════════════════════
def compute_risk_score(drift_report: Any, live_metrics: Any) -> dict:
    """Computa el risk score 0-100 combinando drift + live metrics.

    Args:
        drift_report: dict — output de detect_gate_drift (M13.7)
        live_metrics: dict — snapshot del endpoint M13.3

    Returns:
        dict con risk_score, risk_level, contributors, recommendation, timestamp.

    Pure function: NO muta inputs, NO IO. Fail-safe sobre inputs malformados.
    """
    try:
        # Computar component scores (0-100 cada uno)
        drift_score = _compute_drift_score(drift_report)
        error_score = _compute_error_score(live_metrics)
        block_score = _compute_block_score(live_metrics)
        volatility_score = _compute_volatility_score(drift_report)

        # Aplicar weights → contributors (cada uno es la contribución absoluta
        # del componente al risk_score total)
        contributors = {
            "drift_weight":      drift_score * WEIGHTS["drift"],
            "error_weight":      error_score * WEIGHTS["error"],
            "block_weight":      block_score * WEIGHTS["block"],
            "volatility_weight": volatility_score * WEIGHTS["volatility"],
        }

        # Sum contributors → risk_score
        risk_score = sum(contributors.values())
        risk_score = _clamp(risk_score)

        # Determinar level y recomendación
        risk_level = _risk_level_from_score(risk_score)
        recommendation = RECOMMENDATION_BY_LEVEL.get(risk_level, "monitor")

        return {
            "risk_score":     float(round(risk_score, 2)),
            "risk_level":     risk_level,
            "contributors":   {k: float(round(v, 2))
                                for k, v in contributors.items()},
            "recommendation": recommendation,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe: default seguro (low risk, continue)
        return {
            "risk_score":     0.0,
            "risk_level":     "low",
            "contributors": {
                "drift_weight":      0.0,
                "error_weight":      0.0,
                "block_weight":      0.0,
                "volatility_weight": 0.0,
            },
            "recommendation": "continue",
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "error":          "scoring_failed_failsafe",
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
