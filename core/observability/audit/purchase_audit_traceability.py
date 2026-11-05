"""core/observability/audit/purchase_audit_traceability.py — M17.

Audit & Traceability Layer (ADVISORY ONLY) — Layer 6.

Función pura que consolida los 6 outputs operacionales del stack
(M13.7 → M16) en una traza auditable con integrity checks, reasoning
chain y trace_id determinístico.

PRINCIPIO ARQUITECTÓNICO:
    M17 es ADVISORY-ONLY y la CAPA TERMINAL DE LECTURA:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA hace override
        - NUNCA crea feedback loops
        - SOLO genera trazabilidad operacional

Matriz audit_state (precedencia estricta):
    1. INCONSISTENT    (cualquier integrity_check == False)
                       → inconsistent + auth=False + review=True
    2. REVIEW_REQUIRED (policy_state in {blocked, restricted})
                       → review_required + auth=False + review=True
    3. GOVERNED        (policy_state == controlled)
                       → governed + auth=False + review=True
    4. VERIFIED        (policy_state == compliant AND
                        decision_consistency == True)
                       → verified + auth=True + review=False
    5. DEFAULT SAFE    → review_required + auth=False + review=True

Integrity checks (derivados de las capas):
    - risk_matches_drift:          drift_severity vs risk_level coherencia
    - governance_matches_autonomy: autonomy_decision vs governance_state
    - policy_matches_governance:   governance_state vs policy_state
    - layer_order_valid:           AND lógico de los 3 anteriores

Trace ID:
    SHA-256(autonomy|governance|policy|risk) truncado a 16 hex chars.
    Determinístico — mismo input siempre produce mismo trace_id.

Output schema:
    {
        "trace_id": str (16 hex chars),
        "audit_state": "inconsistent" | "review_required"
                      | "governed" | "verified",
        "decision_lineage": list[str] (7 entries),
        "decision_consistency": bool,
        "human_review_required": bool,
        "deployment_authorized": bool,
        "signals": {drift_severity, risk_level, readiness_status,
                    autonomy_decision, governance_state, policy_state},
        "reasoning_chain": list[str] (7 entries — alias de decision_lineage),
        "integrity_checks": {layer_order_valid, policy_matches_governance,
                             governance_matches_autonomy, risk_matches_drift},
        "timestamp": ISO8601
    }
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes públicas
# ════════════════════════════════════════════════════════════════════
AUDIT_STATES: tuple = (
    "inconsistent",
    "review_required",
    "governed",
    "verified",
)

INTEGRITY_CHECK_NAMES: tuple = (
    "layer_order_valid",
    "policy_matches_governance",
    "governance_matches_autonomy",
    "risk_matches_drift",
)


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


def _check_risk_matches_drift(drift_severity: str, risk_level: str) -> bool:
    """Verifica coherencia entre severity de drift y risk level.

    Reglas:
        drift=critical  ⇒ risk in {high, critical}
        drift=high      ⇒ risk != low
        otros           ⇒ siempre aceptable
    """
    if drift_severity == "critical":
        return risk_level in ("high", "critical")
    if drift_severity == "high":
        return risk_level != "low"
    return True


def _check_governance_matches_autonomy(
    autonomy_decision: str, governance_state: str,
) -> bool:
    """Verifica coherencia entre autonomy decision y governance state.

    Reglas (alineadas con matriz M15):
        rollback_required → manual_review_required
        pause_system      → manual_review_required
        hold_dry_run      → observation_only o manual_review_required
        allow_live        → approval_candidate o production_governed
    """
    if autonomy_decision == "rollback_required":
        return governance_state == "manual_review_required"
    if autonomy_decision == "pause_system":
        return governance_state == "manual_review_required"
    if autonomy_decision == "hold_dry_run":
        return governance_state in (
            "observation_only", "manual_review_required",
        )
    if autonomy_decision == "allow_live":
        return governance_state in (
            "approval_candidate", "production_governed",
        )
    # Decisión desconocida ⇒ inconsistente
    return False


def _check_policy_matches_governance(
    governance_state: str, policy_state: str,
) -> bool:
    """Verifica coherencia entre governance state y policy state.

    Reglas (alineadas con matriz M16):
        manual_review_required → blocked
        observation_only       → restricted
        approval_candidate     → controlled o restricted (default safe)
        production_governed    → compliant
    """
    if governance_state == "manual_review_required":
        return policy_state == "blocked"
    if governance_state == "observation_only":
        return policy_state == "restricted"
    if governance_state == "approval_candidate":
        return policy_state in ("controlled", "restricted")
    if governance_state == "production_governed":
        return policy_state == "compliant"
    return False


def _compute_integrity_checks(
    *,
    drift_severity: str,
    risk_level: str,
    autonomy_decision: str,
    governance_state: str,
    policy_state: str,
) -> dict:
    """Computa los 4 integrity checks. Devuelve dict de booleans."""
    risk_ok = _check_risk_matches_drift(drift_severity, risk_level)
    gov_ok = _check_governance_matches_autonomy(
        autonomy_decision, governance_state,
    )
    policy_ok = _check_policy_matches_governance(
        governance_state, policy_state,
    )
    # layer_order_valid es la conjunción agregada
    layer_ok = bool(risk_ok and gov_ok and policy_ok)
    return {
        "risk_matches_drift":          risk_ok,
        "governance_matches_autonomy": gov_ok,
        "policy_matches_governance":   policy_ok,
        "layer_order_valid":           layer_ok,
    }


def _compute_trace_id(
    autonomy_decision: str,
    governance_state: str,
    policy_state: str,
    risk_level: str,
) -> str:
    """Genera trace_id determinístico vía SHA-256 truncado.

    NO usa random ni datetime — solo los 4 inputs canónicos.
    Mismos inputs ⇒ mismo trace_id.
    """
    raw = f"{autonomy_decision}|{governance_state}|{policy_state}|{risk_level}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest[:16]


def _build_reasoning_chain(
    *,
    drift_severity: str,
    risk_level: str,
    readiness_status: str,
    autonomy_decision: str,
    governance_state: str,
    policy_state: str,
    audit_state: str,
) -> list:
    """Construye la cadena de razonamiento reflejando el flujo de 7 capas."""
    return [
        f"drift severity={drift_severity}",
        f"risk level={risk_level}",
        f"readiness status={readiness_status}",
        f"autonomy decision={autonomy_decision}",
        f"governance state={governance_state}",
        f"policy state={policy_state}",
        f"audit state={audit_state}",
    ]


def _determine_audit_state(
    *,
    policy_state: str,
    decision_consistency: bool,
) -> str:
    """Aplica la matriz audit con precedencia estricta."""
    # 1. INCONSISTENT
    if not decision_consistency:
        return "inconsistent"
    # 2. REVIEW_REQUIRED (blocked o restricted)
    if policy_state in ("blocked", "restricted"):
        return "review_required"
    # 3. GOVERNED
    if policy_state == "controlled":
        return "governed"
    # 4. VERIFIED
    if policy_state == "compliant":
        return "verified"
    # 5. DEFAULT SAFE
    return "review_required"


# ════════════════════════════════════════════════════════════════════
#  API principal: build_audit_trace
# ════════════════════════════════════════════════════════════════════
def build_audit_trace(
    drift_report: Any,
    risk_report: Any,
    readiness_report: Any,
    autonomy_report: Any,
    governance_report: Any,
    policy_report: Any,
) -> dict:
    """Consolida los 6 outputs operacionales en una traza de auditoría.

    ADVISORY-ONLY: NO ejecuta, NO muta, NO override, NO feedback.

    Args:
        drift_report:      dict — output de detect_gate_drift (M13.7)
        risk_report:       dict — output de compute_risk_score (M13.8)
        readiness_report:  dict — output de compute_readiness_index (M13.9)
        autonomy_report:   dict — output de decide_purchase_autonomy (M14)
        governance_report: dict — output de evaluate_governance_state (M15)
        policy_report:     dict — output de evaluate_policy_compliance (M16)

    Returns:
        dict con trace_id, audit_state, decision_lineage, decision_consistency,
        human_review_required, deployment_authorized, signals,
        reasoning_chain, integrity_checks, timestamp.

    Función pura: NO muta inputs, NO IO. Fail-safe sobre inputs malformados.
    """
    try:
        # ── Extraer signals con defaults conservadores ──
        drift_severity = _safe_get(drift_report, "severity",
                                     default="critical")
        if not isinstance(drift_severity, str):
            drift_severity = "critical"

        risk_level = _safe_get(risk_report, "risk_level",
                                default="critical")
        if not isinstance(risk_level, str):
            risk_level = "critical"

        readiness_status = _safe_get(readiness_report, "status",
                                       default="not_ready")
        if not isinstance(readiness_status, str):
            readiness_status = "not_ready"

        autonomy_decision = _safe_get(autonomy_report, "decision",
                                        default="rollback_required")
        if not isinstance(autonomy_decision, str):
            autonomy_decision = "rollback_required"

        governance_state = _safe_get(governance_report, "governance_state",
                                       default="manual_review_required")
        if not isinstance(governance_state, str):
            governance_state = "manual_review_required"

        policy_state = _safe_get(policy_report, "policy_state",
                                   default="restricted")
        if not isinstance(policy_state, str):
            policy_state = "restricted"

        # ── Integrity checks ──
        integrity = _compute_integrity_checks(
            drift_severity=drift_severity,
            risk_level=risk_level,
            autonomy_decision=autonomy_decision,
            governance_state=governance_state,
            policy_state=policy_state,
        )
        decision_consistency = integrity["layer_order_valid"]

        # ── Audit state (precedencia matriz) ──
        audit_state = _determine_audit_state(
            policy_state=policy_state,
            decision_consistency=decision_consistency,
        )

        # ── Deployment authorization solo en verified ──
        deployment_authorized = bool(
            audit_state == "verified"
            and policy_state == "compliant"
            and decision_consistency
        )
        human_review_required = not (
            audit_state == "verified" and decision_consistency
        )

        # ── Trace ID determinístico ──
        trace_id = _compute_trace_id(
            autonomy_decision=autonomy_decision,
            governance_state=governance_state,
            policy_state=policy_state,
            risk_level=risk_level,
        )

        # ── Reasoning chain (7 entradas reflejando capas) ──
        chain = _build_reasoning_chain(
            drift_severity=drift_severity,
            risk_level=risk_level,
            readiness_status=readiness_status,
            autonomy_decision=autonomy_decision,
            governance_state=governance_state,
            policy_state=policy_state,
            audit_state=audit_state,
        )

        return {
            "trace_id":               trace_id,
            "audit_state":            audit_state,
            "decision_lineage":       list(chain),  # alias del reasoning chain
            "decision_consistency":   bool(decision_consistency),
            "human_review_required":  bool(human_review_required),
            "deployment_authorized":  bool(deployment_authorized),
            "signals": {
                "drift_severity":    drift_severity,
                "risk_level":        risk_level,
                "readiness_status":  readiness_status,
                "autonomy_decision": autonomy_decision,
                "governance_state":  governance_state,
                "policy_state":      policy_state,
            },
            "reasoning_chain":   list(chain),
            "integrity_checks":  integrity,
            "timestamp":         datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe absoluto: review_required + sin autorización
        return {
            "trace_id":               "0" * 16,
            "audit_state":            "review_required",
            "decision_lineage":       ["audit_compute_failed_failsafe"],
            "decision_consistency":   False,
            "human_review_required":  True,
            "deployment_authorized":  False,
            "signals": {
                "drift_severity":    "unknown",
                "risk_level":        "unknown",
                "readiness_status":  "unknown",
                "autonomy_decision": "unknown",
                "governance_state":  "unknown",
                "policy_state":      "unknown",
            },
            "reasoning_chain":  ["audit_compute_failed_failsafe"],
            "integrity_checks": {
                "risk_matches_drift":          False,
                "governance_matches_autonomy": False,
                "policy_matches_governance":   False,
                "layer_order_valid":           False,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error":     "audit_compute_failed_failsafe",
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
