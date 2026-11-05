"""core/observability/risk/ — M13.8 Risk Scoring Engine.

Pure function layer que traduce señales de observabilidad
(drift_report de M13.7 + live_metrics de M13.3) en un risk score
cuantitativo y una recomendación operacional.

Principio: NO ejecuta, NO alerta, NO bloquea. SOLO cuantifica.
"""
from core.observability.risk.purchase_gate_risk import (
    RECOMMENDATION_BY_LEVEL,
    RISK_LEVEL_THRESHOLDS,
    SEVERITY_TO_BASE_SCORE,
    WEIGHTS,
    compute_risk_score,
)

# Spec alias — nombre canónico del task spec M13.8
compute_purchase_risk_score = compute_risk_score

__all__ = [
    "compute_risk_score",
    "compute_purchase_risk_score",
    "RECOMMENDATION_BY_LEVEL",
    "RISK_LEVEL_THRESHOLDS",
    "SEVERITY_TO_BASE_SCORE",
    "WEIGHTS",
]
