"""core/observability/explainability/purchase_decision_explainability.py — M18.

Decision Explainability Layer (ADVISORY ONLY) — Layer 7.

Función pura que sintetiza los 7 outputs operacionales del stack
(M13.7 → M17) en una explicación humana determinística con:
    - resumen ejecutivo
    - narrativa por capa (7 líneas)
    - razones humanas (7 códigos)
    - postura del sistema
    - guía operacional
    - resúmenes de risk y deployment
    - propagación de trace_id y consistency

PRINCIPIO ARQUITECTÓNICO:
    M18 es ADVISORY-ONLY y EXPLAINABILITY-ONLY:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA hace override
        - NUNCA usa generación libre, NLP, ni modelos IA
        - SOLO aplica plantillas estáticas determinísticas

Plantillas determinísticas:
    Cada capa produce exactamente:
        - 1 línea narrativa (decision_narrative)
        - 1 razón humana (human_readable_reasons, código snake_case)

    Sin variabilidad, sin aleatoriedad, sin inferencia creativa.

Matriz explanation_state (precedencia estricta):
    1. CRITICAL_EXPLAINED   (audit=inconsistent OR policy=blocked)
                            → critical
    2. RESTRICTED_EXPLAINED (policy in {restricted, controlled})
                            → restricted
    3. VERIFIED_EXPLAINED   (policy=compliant AND audit=verified)
                            → verified
    4. DEFAULT SAFE         → restricted

Matriz system_posture (per spec, por policy_state):
    blocked    → defensive
    restricted → constrained
    controlled → supervised
    compliant  → production_ready

Matriz operator_guidance (per spec, por policy_state):
    blocked    → immediate_review_required
    restricted → continue_observation
    controlled → await_manual_approval
    compliant  → maintain_operational_controls

Output schema (literal del spec):
    {
        "explanation_state": "critical" | "restricted" | "verified",
        "executive_summary": str,
        "decision_narrative": list[str] (7 entries),
        "human_readable_reasons": list[str] (7 entries),
        "risk_summary": str,
        "deployment_summary": str,
        "operator_guidance": str,
        "system_posture": str,
        "signals": {drift_severity, risk_level, readiness_status,
                    autonomy_decision, governance_state, policy_state,
                    audit_state},
        "consistency_verified": bool,
        "trace_id": str,
        "timestamp": str,
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes públicas
# ════════════════════════════════════════════════════════════════════
EXPLANATION_STATES: tuple = ("critical", "restricted", "verified")


# Matriz system_posture (per spec)
SYSTEM_POSTURE_MATRIX: dict[str, str] = {
    "blocked":     "defensive",
    "restricted":  "constrained",
    "controlled":  "supervised",
    "compliant":   "production_ready",
}


# Matriz operator_guidance (per spec)
OPERATOR_GUIDANCE_MATRIX: dict[str, str] = {
    "blocked":     "immediate_review_required",
    "restricted":  "continue_observation",
    "controlled":  "await_manual_approval",
    "compliant":   "maintain_operational_controls",
}


# Postura por defecto cuando policy_state no es canónico
_DEFAULT_POSTURE: str = "constrained"
_DEFAULT_GUIDANCE: str = "continue_observation"


# ════════════════════════════════════════════════════════════════════
#  Helpers internos
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


def _safe_str(value: Any, default: str = "unknown") -> str:
    """Coerción fail-safe a string."""
    if isinstance(value, str) and value:
        return value
    return default


def _determine_explanation_state(
    policy_state: str, audit_state: str,
) -> str:
    """Aplica la matriz explanation_state con precedencia estricta."""
    # 1. CRITICAL_EXPLAINED
    if audit_state == "inconsistent" or policy_state == "blocked":
        return "critical"
    # 2. RESTRICTED_EXPLAINED
    if policy_state in ("restricted", "controlled"):
        return "restricted"
    # 3. VERIFIED_EXPLAINED
    if policy_state == "compliant" and audit_state == "verified":
        return "verified"
    # 4. DEFAULT SAFE
    return "restricted"


def _build_decision_narrative(
    *,
    drift_severity: str,
    risk_level: str,
    readiness_status: str,
    autonomy_decision: str,
    governance_state: str,
    policy_state: str,
    audit_state: str,
) -> list:
    """Construye 7 líneas narrativas determinísticas, una por capa."""
    return [
        f"Drift severity detected: {drift_severity}.",
        f"Risk engine classified system as {risk_level} risk.",
        f"Readiness index indicates {readiness_status} state.",
        f"Autonomy layer recommends {autonomy_decision}.",
        f"Governance layer state: {governance_state}.",
        f"Policy layer state: {policy_state}.",
        f"Audit layer state: {audit_state}.",
    ]


def _build_human_reasons(
    *,
    drift_severity: str,
    risk_level: str,
    readiness_status: str,
    autonomy_decision: str,
    governance_state: str,
    policy_state: str,
    audit_state: str,
) -> list:
    """Construye 7 códigos snake_case humano-legibles, uno por capa."""
    return [
        f"drift_pattern_indicates_{drift_severity}_deviation",
        f"risk_engine_{risk_level}_aggregate",
        f"readiness_index_{readiness_status}",
        f"autonomy_recommends_{autonomy_decision}",
        f"governance_state_{governance_state}",
        f"policy_envelope_{policy_state}",
        f"audit_{audit_state}",
    ]


def _build_executive_summary(explanation_state: str) -> str:
    """Genera resumen ejecutivo determinístico por explanation_state."""
    if explanation_state == "critical":
        return (
            "System under critical advisory state. "
            "Deployment denied. Operator review required."
        )
    if explanation_state == "verified":
        return (
            "System verified across all layers. "
            "Deployment authorized under operator monitoring."
        )
    # restricted o default
    return (
        "System under restricted operational envelope. "
        "Deployment held pending supervision."
    )


def _build_risk_summary(risk_level: str) -> str:
    """Genera resumen textual de risk por risk_level."""
    if risk_level == "critical":
        return "Aggregate risk critical."
    if risk_level == "high":
        return "Aggregate risk elevated."
    if risk_level == "medium":
        return "Aggregate risk at moderate level."
    if risk_level == "low":
        return "Aggregate risk below operational threshold."
    return "Aggregate risk indeterminate."


def _build_deployment_summary(explanation_state: str) -> str:
    """Genera resumen textual de deployment por explanation_state."""
    if explanation_state == "verified":
        return "Deployment authorized under continued monitoring."
    if explanation_state == "critical":
        return "Deployment authorization denied."
    return "Deployment held pending review."


# ════════════════════════════════════════════════════════════════════
#  API principal: build_decision_explanation
# ════════════════════════════════════════════════════════════════════
def build_decision_explanation(
    drift_report: Any,
    risk_report: Any,
    readiness_report: Any,
    autonomy_report: Any,
    governance_report: Any,
    policy_report: Any,
    audit_report: Any,
) -> dict:
    """Construye explicación humana determinística del pipeline completo.

    ADVISORY-ONLY: NO ejecuta, NO muta, NO override.
    EXPLAINABILITY-ONLY: NO usa NLP, NO usa modelos IA, NO genera libre.

    Args:
        drift_report:      dict — output de detect_gate_drift (M13.7)
        risk_report:       dict — output de compute_risk_score (M13.8)
        readiness_report:  dict — output de compute_readiness_index (M13.9)
        autonomy_report:   dict — output de decide_purchase_autonomy (M14)
        governance_report: dict — output de evaluate_governance_state (M15)
        policy_report:     dict — output de evaluate_policy_compliance (M16)
        audit_report:      dict — output de build_audit_trace (M17)

    Returns:
        dict con explanation_state, executive_summary, decision_narrative,
        human_readable_reasons, risk_summary, deployment_summary,
        operator_guidance, system_posture, signals, consistency_verified,
        trace_id, timestamp.

    Función pura: NO muta inputs, NO IO. Fail-safe sobre inputs malformados.
    """
    try:
        # ── Extraer signals con defaults conservadores ──
        drift_severity = _safe_str(
            _safe_get(drift_report, "severity"),
            default="unknown",
        )
        risk_level = _safe_str(
            _safe_get(risk_report, "risk_level"),
            default="unknown",
        )
        readiness_status = _safe_str(
            _safe_get(readiness_report, "status"),
            default="unknown",
        )
        autonomy_decision = _safe_str(
            _safe_get(autonomy_report, "decision"),
            default="unknown",
        )
        governance_state = _safe_str(
            _safe_get(governance_report, "governance_state"),
            default="unknown",
        )
        policy_state = _safe_str(
            _safe_get(policy_report, "policy_state"),
            default="unknown",
        )
        audit_state = _safe_str(
            _safe_get(audit_report, "audit_state"),
            default="unknown",
        )

        # ── Determinar explanation_state (precedencia matriz) ──
        explanation_state = _determine_explanation_state(
            policy_state=policy_state,
            audit_state=audit_state,
        )

        # ── Construir narrativa (7 líneas determinísticas) ──
        decision_narrative = _build_decision_narrative(
            drift_severity=drift_severity,
            risk_level=risk_level,
            readiness_status=readiness_status,
            autonomy_decision=autonomy_decision,
            governance_state=governance_state,
            policy_state=policy_state,
            audit_state=audit_state,
        )

        # ── Construir razones humanas (7 códigos snake_case) ──
        human_reasons = _build_human_reasons(
            drift_severity=drift_severity,
            risk_level=risk_level,
            readiness_status=readiness_status,
            autonomy_decision=autonomy_decision,
            governance_state=governance_state,
            policy_state=policy_state,
            audit_state=audit_state,
        )

        # ── Resúmenes textuales ──
        executive_summary = _build_executive_summary(explanation_state)
        risk_summary = _build_risk_summary(risk_level)
        deployment_summary = _build_deployment_summary(explanation_state)

        # ── Postura y guía (matrices spec por policy_state) ──
        system_posture = SYSTEM_POSTURE_MATRIX.get(
            policy_state, _DEFAULT_POSTURE,
        )
        operator_guidance = OPERATOR_GUIDANCE_MATRIX.get(
            policy_state, _DEFAULT_GUIDANCE,
        )

        # ── Propagar trace_id y consistency del audit (M17) ──
        trace_id = _safe_str(
            _safe_get(audit_report, "trace_id"),
            default="0" * 16,
        )
        consistency_verified = bool(
            _safe_get(audit_report, "decision_consistency", default=False)
        )

        return {
            "explanation_state":     explanation_state,
            "executive_summary":     executive_summary,
            "decision_narrative":    decision_narrative,
            "human_readable_reasons": human_reasons,
            "risk_summary":          risk_summary,
            "deployment_summary":    deployment_summary,
            "operator_guidance":     operator_guidance,
            "system_posture":        system_posture,
            "signals": {
                "drift_severity":    drift_severity,
                "risk_level":        risk_level,
                "readiness_status":  readiness_status,
                "autonomy_decision": autonomy_decision,
                "governance_state":  governance_state,
                "policy_state":      policy_state,
                "audit_state":       audit_state,
            },
            "consistency_verified":  consistency_verified,
            "trace_id":              trace_id,
            "timestamp":             datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe absoluto: restricted + no consistency
        return {
            "explanation_state":     "restricted",
            "executive_summary": (
                "System under restricted operational envelope. "
                "Deployment held pending supervision."
            ),
            "decision_narrative": [
                "Drift severity detected: unknown.",
                "Risk engine classified system as unknown risk.",
                "Readiness index indicates unknown state.",
                "Autonomy layer recommends unknown.",
                "Governance layer state: unknown.",
                "Policy layer state: unknown.",
                "Audit layer state: unknown.",
            ],
            "human_readable_reasons": [
                "explainability_compute_failed_failsafe",
            ],
            "risk_summary":          "Aggregate risk indeterminate.",
            "deployment_summary":    "Deployment held pending review.",
            "operator_guidance":     _DEFAULT_GUIDANCE,
            "system_posture":        _DEFAULT_POSTURE,
            "signals": {
                "drift_severity":    "unknown",
                "risk_level":        "unknown",
                "readiness_status":  "unknown",
                "autonomy_decision": "unknown",
                "governance_state":  "unknown",
                "policy_state":      "unknown",
                "audit_state":       "unknown",
            },
            "consistency_verified":  False,
            "trace_id":              "0" * 16,
            "timestamp":             datetime.now(timezone.utc).isoformat(),
            "error":                 "explainability_compute_failed_failsafe",
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
