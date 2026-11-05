"""core/observability/policy/ — M16 Policy & Compliance Envelope.

Capa Layer 5 puramente ADVISORY encima de M15 governance.

Pure function layer que sintetiza outputs de M14 (autonomy) + M15 (governance)
+ M13.9 (readiness) en un estado de policy compliance + autorización de
deployment + acción de policy recomendada para el operador.

PRINCIPIO CORE:
    M16 NUNCA ejecuta, NUNCA muta, NUNCA hace override.
    NUNCA introduce feedback loops sobre capas inferiores.
    SOLO interpreta outputs y produce recomendaciones de compliance.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.policy.purchase_policy_compliance import (
    COMPLIANCE_LEVELS,
    POLICY_ACTIONS,
    POLICY_STATES,
    POLICY_STATE_TO_ACTION,
    POLICY_STATE_TO_AUTHORIZATION,
    POLICY_STATE_TO_COMPLIANCE,
    POLICY_STATE_TO_REVIEW_REQUIRED,
    evaluate_policy_compliance,
)

# Alias canónico del spec M16
compute_policy_compliance_state = evaluate_policy_compliance

__all__ = [
    "evaluate_policy_compliance",
    "compute_policy_compliance_state",
    "POLICY_STATES",
    "COMPLIANCE_LEVELS",
    "POLICY_ACTIONS",
    "POLICY_STATE_TO_ACTION",
    "POLICY_STATE_TO_COMPLIANCE",
    "POLICY_STATE_TO_AUTHORIZATION",
    "POLICY_STATE_TO_REVIEW_REQUIRED",
]
