"""core/observability/autonomy/ — M14 Autonomous Decision Layer (ADVISORY ONLY).

Pure function layer que sintetiza outputs de M13.7 (drift), M13.8 (risk),
M13.9 (readiness) en una decisión advisory + confidence + operator action.

PRINCIPIO CORE:
    M14 NUNCA ejecuta, NUNCA muta, NUNCA hace override del gate.
    SOLO traduce señales acumuladas en una recomendación cuantitativa
    para el operador humano.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.autonomy.purchase_gate_autonomy import (
    AUTONOMY_LEVEL_BY_DECISION,
    BASE_CONFIDENCE_BY_DECISION,
    OPERATOR_ACTION_BY_DECISION,
    decide_purchase_autonomy,
)

# Spec alias — nombre canónico del task spec M14
compute_autonomous_purchase_decision = decide_purchase_autonomy

__all__ = [
    "decide_purchase_autonomy",
    "compute_autonomous_purchase_decision",
    "AUTONOMY_LEVEL_BY_DECISION",
    "BASE_CONFIDENCE_BY_DECISION",
    "OPERATOR_ACTION_BY_DECISION",
]
