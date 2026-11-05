"""core/observability/governance/ — M15 Governance & Human Approval Layer.

Pure function layer que sintetiza outputs de M14 (autonomy) + M13.9 (readiness)
+ M13.8 (risk) + M13.7 (drift) en un governance state operacional con:
    - workflow humano
    - escalation level
    - approval requirements
    - release recommendation

PRINCIPIO CORE:
    M15 NUNCA ejecuta, NUNCA muta, NUNCA hace override del gate.
    SOLO define el envelope de governance humano sobre M14.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.governance.purchase_governance import (
    APPROVAL_REQUIRED_BY_STATE,
    ESCALATION_LEVELS,
    GOVERNANCE_STATES,
    OPERATOR_ACTIONS,
    RELEASE_RECOMMENDATIONS,
    evaluate_governance_state,
)

# Spec alias — nombre canónico del task spec M15
compute_purchase_governance_state = evaluate_governance_state

__all__ = [
    "evaluate_governance_state",
    "compute_purchase_governance_state",
    "GOVERNANCE_STATES",
    "ESCALATION_LEVELS",
    "RELEASE_RECOMMENDATIONS",
    "OPERATOR_ACTIONS",
    "APPROVAL_REQUIRED_BY_STATE",
]
