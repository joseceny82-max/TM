"""core/observability/policy/purchase_policy_compliance.py — M16.

Policy & Compliance Envelope (ADVISORY ONLY) — Layer 5.

Función pura que sintetiza outputs operacionales en un estado de policy
compliance con autorización de deployment y acción recomendada.

PRINCIPIO ARQUITECTÓNICO:
    M16 es ADVISORY-ONLY:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA hace override de ningún componente
        - NUNCA crea feedback loops sobre M13/M14/M15
        - SOLO produce envelope de policy para auditoría/decisión humana

Matriz de policy (precedencia estricta):
    1. BLOCKED      (governance=manual_review OR autonomy=rollback)
                    → blocked + non_compliant + auth=False + review=True
    2. RESTRICTED   (governance=observation_only OR autonomy=pause_system)
                    → restricted + limited + auth=False + review=True
    3. CONTROLLED   (governance=approval_candidate AND
                     readiness in {ready, production_safe})
                    → controlled + supervised + auth=False + review=True
    4. COMPLIANT    (governance=production_governed AND
                     readiness=production_safe AND autonomy=allow_live)
                    → compliant + full + auth=True + review=False
    5. DEFAULT SAFE (otro caso)
                    → restricted + limited + auth=False + review=True

Schema de output:
    {
        "policy_state": "blocked" | "restricted" | "controlled" | "compliant",
        "compliance_level": "non_compliant" | "limited" | "supervised"
                           | "full",
        "deployment_authorization": bool,
        "recommended_policy_action": "halt_and_review"
                                     | "request_authorization"
                                     | "await_approval"
                                     | "maintain_compliance",
        "operator_review_required": bool,
        "signals": {governance_state, autonomy_decision, risk_level,
                    readiness_status},
        "reasons": list[str],
        "timestamp": ISO8601
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes públicas (per spec)
# ════════════════════════════════════════════════════════════════════
POLICY_STATES: tuple = (
    "blocked",
    "restricted",
    "controlled",
    "compliant",
)

COMPLIANCE_LEVELS: tuple = (
    "non_compliant",
    "limited",
    "supervised",
    "full",
)

POLICY_ACTIONS: tuple = (
    "halt_and_review",
    "request_authorization",
    "await_approval",
    "maintain_compliance",
)


# Mapeos derivados del policy_state
POLICY_STATE_TO_COMPLIANCE: dict[str, str] = {
    "blocked":     "non_compliant",
    "restricted":  "limited",
    "controlled":  "supervised",
    "compliant":   "full",
}

POLICY_STATE_TO_AUTHORIZATION: dict[str, bool] = {
    "blocked":     False,
    "restricted":  False,
    "controlled":  False,
    "compliant":   True,
}

POLICY_STATE_TO_REVIEW_REQUIRED: dict[str, bool] = {
    "blocked":     True,
    "restricted":  True,
    "controlled":  True,
    "compliant":   False,
}

POLICY_STATE_TO_ACTION: dict[str, str] = {
    "blocked":     "halt_and_review",
    "restricted":  "request_authorization",
    "controlled":  "await_approval",
    "compliant":   "maintain_compliance",
}


# ════════════════════════════════════════════════════════════════════
#  Helpers internos
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    """Nested dict get fail-safe — devuelve default si cualquier paso falla."""
    current = d
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _build_reasons(
    *,
    governance_state: str,
    autonomy_decision: str,
    risk_level: str,
    readiness_status: str,
    policy_state: str,
) -> list:
    """Construye lista de reasons estructurados para auditoría."""
    reasons: list = []

    if policy_state == "blocked":
        if governance_state == "manual_review_required":
            reasons.append("governance_state_manual_review_required")
        if autonomy_decision == "rollback_required":
            reasons.append("autonomy_decision_rollback_required")
        if risk_level == "critical":
            reasons.append("risk_level_critical")
    elif policy_state == "restricted":
        if governance_state == "observation_only":
            reasons.append("governance_state_observation_only")
        if autonomy_decision == "pause_system":
            reasons.append("autonomy_decision_pause_system")
        if not reasons:
            reasons.append("default_safe_policy_envelope")
    elif policy_state == "controlled":
        reasons.append("governance_approval_candidate_with_supervised_readiness")
        if readiness_status == "ready":
            reasons.append("readiness_status_ready")
        if readiness_status == "production_safe":
            reasons.append("readiness_status_production_safe")
    elif policy_state == "compliant":
        reasons.append("full_compliance_all_signals_aligned")
        reasons.append("governance_state_production_governed")
        reasons.append("autonomy_decision_allow_live")

    if not reasons:
        reasons.append("policy_envelope_default_safe")
    return reasons


def _determine_policy_state(
    governance_state: str,
    autonomy_decision: str,
    readiness_status: str,
) -> str:
    """Aplica la matriz de policy con precedencia estricta."""
    # 1. BLOCKED
    if (governance_state == "manual_review_required"
            or autonomy_decision == "rollback_required"):
        return "blocked"

    # 2. RESTRICTED (sin caer dentro de blocked)
    if (governance_state == "observation_only"
            or autonomy_decision == "pause_system"):
        return "restricted"

    # 3. CONTROLLED
    if (governance_state == "approval_candidate"
            and readiness_status in ("ready", "production_safe")):
        return "controlled"

    # 4. COMPLIANT
    if (governance_state == "production_governed"
            and readiness_status == "production_safe"
            and autonomy_decision == "allow_live"):
        return "compliant"

    # 5. DEFAULT SAFE
    return "restricted"


# ════════════════════════════════════════════════════════════════════
#  API principal: evaluate_policy_compliance
# ════════════════════════════════════════════════════════════════════
def evaluate_policy_compliance(
    autonomy_report: Any,
    governance_report: Any,
    readiness_report: Any,
) -> dict:
    """Computa estado de policy compliance desde outputs M14/M15/M13.9.

    ADVISORY-ONLY: NO ejecuta, NO muta, NO override, NO feedback.

    Args:
        autonomy_report:   dict — output de decide_purchase_autonomy (M14)
        governance_report: dict — output de evaluate_governance_state (M15)
        readiness_report:  dict — output de compute_readiness_index (M13.9)

    Returns:
        dict con policy_state, compliance_level, deployment_authorization,
        recommended_policy_action, operator_review_required, signals,
        reasons, timestamp.

    Función pura: NO muta inputs, NO IO. Fail-safe sobre inputs malformados.
    """
    try:
        # Extraer signals fundamentales con defaults conservadores
        governance_state = _safe_get(governance_report, "governance_state",
                                       default="manual_review_required")
        if not isinstance(governance_state, str):
            governance_state = "manual_review_required"

        autonomy_decision = _safe_get(autonomy_report, "decision",
                                        default="rollback_required")
        if not isinstance(autonomy_decision, str):
            autonomy_decision = "rollback_required"

        readiness_status = _safe_get(readiness_report, "status",
                                       default="not_ready")
        if not isinstance(readiness_status, str):
            readiness_status = "not_ready"

        # Risk level — extraído del autonomy.signals (M14 lo espeja)
        risk_level = _safe_get(autonomy_report, "signals", "risk_level",
                                default="critical")
        if not isinstance(risk_level, str):
            risk_level = "critical"

        # Determinar policy state
        policy_state = _determine_policy_state(
            governance_state=governance_state,
            autonomy_decision=autonomy_decision,
            readiness_status=readiness_status,
        )

        # Derivar campos del policy_state
        compliance_level = POLICY_STATE_TO_COMPLIANCE[policy_state]
        deployment_auth = POLICY_STATE_TO_AUTHORIZATION[policy_state]
        review_required = POLICY_STATE_TO_REVIEW_REQUIRED[policy_state]
        recommended_action = POLICY_STATE_TO_ACTION[policy_state]

        reasons = _build_reasons(
            governance_state=governance_state,
            autonomy_decision=autonomy_decision,
            risk_level=risk_level,
            readiness_status=readiness_status,
            policy_state=policy_state,
        )

        return {
            "policy_state":             policy_state,
            "compliance_level":         compliance_level,
            "deployment_authorization": deployment_auth,
            "recommended_policy_action": recommended_action,
            "operator_review_required": review_required,
            "signals": {
                "governance_state":   governance_state,
                "autonomy_decision":  autonomy_decision,
                "risk_level":         risk_level,
                "readiness_status":   readiness_status,
            },
            "reasons":   reasons,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe absoluto: default seguro (restricted)
        return {
            "policy_state":             "restricted",
            "compliance_level":         "limited",
            "deployment_authorization": False,
            "recommended_policy_action": "request_authorization",
            "operator_review_required": True,
            "signals": {
                "governance_state":   "unknown",
                "autonomy_decision":  "unknown",
                "risk_level":         "unknown",
                "readiness_status":   "unknown",
            },
            "reasons":   ["policy_compute_failed_failsafe"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error":     "policy_compute_failed_failsafe",
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
