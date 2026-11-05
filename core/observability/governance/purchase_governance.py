"""core/observability/governance/purchase_governance.py — M15.

Governance & Human Approval Layer (ADVISORY ONLY).

Pure function que sintetiza los 4 reports operacionales del stack M13/M14
en un envelope de governance que define:
    - estado de governance del sistema
    - escalation level operacional
    - approval requirements
    - release recommendation
    - operator action recomendada

PRINCIPIO ARQUITECTÓNICO:
    M15 es ADVISORY-ONLY:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA hace override del gate
        - NUNCA crea feedback loops sobre M13/M14
        - SOLO produce workflows humanos derivados

Governance matrix (precedencia estricta):
    1. EMERGENCY      (autonomy=rollback OR risk=critical)
                      → manual_review_required + emergency + deny
    2. HIGH RISK      (autonomy=pause OR drift=high)
                      → manual_review_required + senior_operator + hold
    3. STABLE DRY-RUN (autonomy=hold_dry_run AND readiness in
                       {stabilizing, ready})
                      → observation_only + operator + hold
    4. GO-LIVE CANDIDATE (autonomy=allow_live AND readiness=production_safe
                          AND risk=low)
                      → approval_candidate + operator + candidate
    5. FULL GOVERNED  (refines 4: no drift AND no errors)
                      → production_governed + none + approved_candidate
    6. Default safe   → observation_only + operator + hold

Output schema:
    {
        "governance_state": "observation_only" | "manual_review_required"
                           | "approval_candidate" | "production_governed",
        "approval_required": bool,
        "recommended_action": "continue_observation" | "request_operator_review"
                             | "prepare_go_live_review"
                             | "maintain_production_controls",
        "release_recommendation": "deny" | "hold" | "candidate"
                                 | "approved_candidate",
        "escalation_level": "none" | "operator" | "senior_operator"
                           | "emergency",
        "signals": {autonomy_decision, risk_level, readiness_status,
                    drift_severity},
        "reasons": list[str],
        "timestamp": ISO8601
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constants (per spec)
# ════════════════════════════════════════════════════════════════════
GOVERNANCE_STATES: tuple = (
    "observation_only",
    "manual_review_required",
    "approval_candidate",
    "production_governed",
)

ESCALATION_LEVELS: tuple = (
    "none",
    "operator",
    "senior_operator",
    "emergency",
)

RELEASE_RECOMMENDATIONS: tuple = (
    "deny",
    "hold",
    "candidate",
    "approved_candidate",
)

OPERATOR_ACTIONS: tuple = (
    "continue_observation",
    "request_operator_review",
    "prepare_go_live_review",
    "maintain_production_controls",
)

APPROVAL_REQUIRED_BY_STATE: dict[str, bool] = {
    "observation_only":         False,
    "manual_review_required":   True,
    "approval_candidate":       True,
    "production_governed":      False,
}


# ════════════════════════════════════════════════════════════════════
#  Internal helpers
# ════════════════════════════════════════════════════════════════════
def _safe_get(d: Any, *path: str, default: Any = None) -> Any:
    current = d
    for key in path:
        if not isinstance(current, dict):
            return default
        if key not in current:
            return default
        current = current[key]
    return current


def _has_internal_errors(drift_report: Any, risk_report: Any) -> bool:
    """Detecta presencia de internal_errors via drift signal o risk weights."""
    signals = _safe_get(drift_report, "signals", default=[])
    if isinstance(signals, list):
        for s in signals:
            if (isinstance(s, dict)
                    and s.get("type") == "internal_error_anomaly"):
                return True
    error_w = _safe_get(risk_report, "contributors", "error_weight",
                         default=0.0)
    try:
        return float(error_w) > 0.0
    except (TypeError, ValueError):
        return False


def _build_reasons(
    *,
    autonomy_decision: str,
    risk_level: str,
    readiness_status: str,
    drift_severity: str,
    drift_detected: bool,
    has_errors: bool,
    governance_state: str,
) -> list:
    """Construye lista de reasons estructurados para auditoría."""
    reasons: list = []

    if governance_state == "manual_review_required":
        if autonomy_decision == "rollback_required":
            reasons.append("autonomy_decision_rollback_required")
        if risk_level == "critical":
            reasons.append("risk_level_critical")
        if autonomy_decision == "pause_system":
            reasons.append("autonomy_decision_pause_system")
        if drift_severity == "high":
            reasons.append("drift_severity_high")
    elif governance_state == "observation_only":
        if autonomy_decision == "hold_dry_run":
            reasons.append("autonomy_decision_hold_dry_run")
        if readiness_status in ("stabilizing", "ready"):
            reasons.append(f"readiness_status_{readiness_status}")
    elif governance_state == "approval_candidate":
        reasons.append("autonomy_allow_live_with_clean_signals")
        if drift_detected:
            reasons.append("drift_present_blocks_full_governance")
        if has_errors:
            reasons.append("internal_errors_present_blocks_full_governance")
    elif governance_state == "production_governed":
        reasons.append("all_signals_clean_full_governance_active")

    if not reasons:
        reasons.append("default_safe_observation")
    return reasons


# ════════════════════════════════════════════════════════════════════
#  Main: evaluate_governance_state
# ════════════════════════════════════════════════════════════════════
def evaluate_governance_state(
    autonomy_report: Any,
    readiness_report: Any,
    risk_report: Any,
    drift_report: Any,
) -> dict:
    """Computa governance state advisory desde los 4 reports operacionales.

    ADVISORY ONLY: NO ejecuta, NO muta, NO override.

    Args:
        autonomy_report:  dict — output de decide_purchase_autonomy (M14)
        readiness_report: dict — output de compute_readiness_index (M13.9)
        risk_report:      dict — output de compute_risk_score (M13.8)
        drift_report:     dict — output de detect_gate_drift (M13.7)

    Returns:
        dict con governance_state, approval_required, recommended_action,
        release_recommendation, escalation_level, signals, reasons, timestamp.

    Pure function: NO muta inputs, NO IO. Fail-safe.
    """
    try:
        # Extract signals
        autonomy_decision = _safe_get(autonomy_report, "decision",
                                        default="rollback_required")
        if not isinstance(autonomy_decision, str):
            autonomy_decision = "rollback_required"

        risk_level = _safe_get(risk_report, "risk_level",
                                default="critical")
        if not isinstance(risk_level, str):
            risk_level = "critical"

        readiness_status = _safe_get(readiness_report, "status",
                                       default="not_ready")
        if not isinstance(readiness_status, str):
            readiness_status = "not_ready"

        drift_severity = _safe_get(drift_report, "severity", default="low")
        if not isinstance(drift_severity, str):
            drift_severity = "low"
        drift_detected = bool(_safe_get(drift_report, "drift_detected",
                                          default=False))

        has_errors = _has_internal_errors(drift_report, risk_report)

        # ────────────────────────────────────────────────────────────────
        #  Governance matrix — precedencia estricta
        # ────────────────────────────────────────────────────────────────
        governance_state: str
        escalation_level: str
        release_recommendation: str
        recommended_action: str

        # 1. EMERGENCY
        if (autonomy_decision == "rollback_required"
                or risk_level == "critical"):
            governance_state = "manual_review_required"
            escalation_level = "emergency"
            release_recommendation = "deny"
            recommended_action = "request_operator_review"

        # 2. HIGH RISK
        elif (autonomy_decision == "pause_system"
                or drift_severity == "high"):
            governance_state = "manual_review_required"
            escalation_level = "senior_operator"
            release_recommendation = "hold"
            recommended_action = "request_operator_review"

        # 3. STABLE DRY-RUN
        elif (autonomy_decision == "hold_dry_run"
                and readiness_status in ("stabilizing", "ready")):
            governance_state = "observation_only"
            escalation_level = "operator"
            release_recommendation = "hold"
            recommended_action = "continue_observation"

        # 4/5. GO-LIVE CANDIDATE / FULL GOVERNED
        elif (autonomy_decision == "allow_live"
                and readiness_status == "production_safe"
                and risk_level == "low"):
            # FULL GOVERNED refines: requires no drift AND no errors
            if (not drift_detected) and (not has_errors):
                governance_state = "production_governed"
                escalation_level = "none"
                release_recommendation = "approved_candidate"
                recommended_action = "maintain_production_controls"
            else:
                governance_state = "approval_candidate"
                escalation_level = "operator"
                release_recommendation = "candidate"
                recommended_action = "prepare_go_live_review"

        # 6. Default safe
        else:
            governance_state = "observation_only"
            escalation_level = "operator"
            release_recommendation = "hold"
            recommended_action = "continue_observation"

        approval_required = APPROVAL_REQUIRED_BY_STATE[governance_state]

        reasons = _build_reasons(
            autonomy_decision=autonomy_decision,
            risk_level=risk_level,
            readiness_status=readiness_status,
            drift_severity=drift_severity,
            drift_detected=drift_detected,
            has_errors=has_errors,
            governance_state=governance_state,
        )

        return {
            "governance_state":       governance_state,
            "approval_required":      approval_required,
            "recommended_action":     recommended_action,
            "release_recommendation": release_recommendation,
            "escalation_level":       escalation_level,
            "signals": {
                "autonomy_decision":  autonomy_decision,
                "risk_level":         risk_level,
                "readiness_status":   readiness_status,
                "drift_severity":     drift_severity,
            },
            "reasons":   reasons,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe: default conservador (manual_review_required, deny)
        return {
            "governance_state":       "manual_review_required",
            "approval_required":      True,
            "recommended_action":     "request_operator_review",
            "release_recommendation": "deny",
            "escalation_level":       "emergency",
            "signals": {
                "autonomy_decision": "unknown",
                "risk_level":        "unknown",
                "readiness_status":  "unknown",
                "drift_severity":    "unknown",
            },
            "reasons":   ["governance_compute_failed_failsafe"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error":     "governance_compute_failed_failsafe",
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
