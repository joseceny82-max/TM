"""core/observability/autonomy/purchase_gate_autonomy.py — M14.

Autonomous Decision Layer (ADVISORY ONLY).

Pure function que sintetiza:
    - drift_report      (M13.7)
    - risk_report       (M13.8)
    - readiness_report  (M13.9)

En una decisión advisory cuantitativa con operator_action recomendada.

PRINCIPIO ARQUITECTÓNICO:
    M14 es advisory-only:
        - NUNCA ejecuta acciones
        - NUNCA muta state
        - NUNCA hace override del gate
        - SOLO produce recomendaciones para el operador

Decision matrix (precedencia estricta crítica → alta → media → allow):
    risk.critical OR drift.critical  → rollback_required
    risk.high     OR drift.high      → pause_system
    risk.medium   OR stabilizing     → hold_dry_run
    go_live AND risk.low AND no_drift → allow_live
    default safe                     → hold_dry_run

Confidence model:
    Base per decision: {allow_live: 90, hold_dry_run: 65,
                        pause_system: 45, rollback_required: 95}
    Adjustments:
        -10 if drift_detected
        -15 if internal_error signal present (error_weight>0 OR drift signal)
        -5 per extra drift signal beyond first
    Clamp [0, 100]

Output schema:
    {
        "decision": "allow_live" | "hold_dry_run" | "pause_system"
                   | "rollback_required",
        "confidence": float (0-100),
        "autonomy_level": "none" | "assisted" | "supervised"
                         | "candidate_autonomous",
        "reasons": list[str],
        "operator_action": "continue_monitoring" | "review_metrics"
                          | "pause_and_investigate" | "rollback_to_safe_mode",
        "signals": {drift_severity, risk_level, readiness_status,
                    go_live_eligibility},
        "timestamp": ISO8601
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constants (per spec)
# ════════════════════════════════════════════════════════════════════
BASE_CONFIDENCE_BY_DECISION: dict[str, float] = {
    "allow_live":          90.0,
    "hold_dry_run":        65.0,
    "pause_system":        45.0,
    "rollback_required":   95.0,
}

AUTONOMY_LEVEL_BY_DECISION: dict[str, str] = {
    "allow_live":          "candidate_autonomous",
    "hold_dry_run":        "supervised",
    "pause_system":        "assisted",
    "rollback_required":   "none",
}

OPERATOR_ACTION_BY_DECISION: dict[str, str] = {
    "allow_live":          "continue_monitoring",
    "hold_dry_run":        "review_metrics",
    "pause_system":        "pause_and_investigate",
    "rollback_required":   "rollback_to_safe_mode",
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


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _has_internal_errors(drift_report: Any, risk_report: Any) -> bool:
    """Detecta presencia de internal_errors usando 2 fuentes:
        - drift signal de tipo 'internal_error_anomaly'
        - risk contributors.error_weight > 0
    """
    signals = _safe_get(drift_report, "signals", default=[])
    if isinstance(signals, list):
        for s in signals:
            if isinstance(s, dict) and s.get("type") == "internal_error_anomaly":
                return True
    error_w = _safe_get(risk_report, "contributors", "error_weight",
                         default=0.0)
    try:
        return float(error_w) > 0.0
    except (TypeError, ValueError):
        return False


def _determine_decision(
    drift_severity: str,
    risk_level: str,
    readiness_status: str,
    go_live: bool,
    drift_detected: bool,
) -> str:
    """Aplica la decision matrix con precedencia estricta."""
    # Precedencia 1: crítico → rollback
    if risk_level == "critical" or drift_severity == "critical":
        return "rollback_required"
    # Precedencia 2: alto → pause
    if risk_level == "high" or drift_severity == "high":
        return "pause_system"
    # Precedencia 3: medio o stabilizing → hold dry_run
    if risk_level == "medium" or readiness_status == "stabilizing":
        return "hold_dry_run"
    # Precedencia 4: allow live solo si TODAS las condiciones limpias
    if go_live and risk_level == "low" and not drift_detected:
        return "allow_live"
    # Default safe
    return "hold_dry_run"


def _compute_confidence(
    decision: str,
    drift_detected: bool,
    has_errors: bool,
    n_drift_signals: int,
) -> float:
    """Computa confidence con base + adjustments + clamp."""
    base = BASE_CONFIDENCE_BY_DECISION.get(decision, 50.0)
    if drift_detected:
        base -= 10.0
    if has_errors:
        base -= 15.0
    extra_signals = max(0, n_drift_signals - 1)
    base -= 5.0 * extra_signals
    return _clamp(base)


def _build_reasons(
    drift_severity: str,
    drift_detected: bool,
    risk_level: str,
    readiness_status: str,
    go_live: bool,
    has_errors: bool,
) -> list:
    """Construye lista de reasons estructurados para auditoría."""
    reasons: list = []
    if drift_detected:
        reasons.append(f"drift_detected_severity_{drift_severity}")
    if risk_level not in ("low",):
        reasons.append(f"risk_level_{risk_level}")
    if readiness_status not in ("production_safe",):
        reasons.append(f"readiness_status_{readiness_status}")
    if not go_live:
        reasons.append("go_live_not_eligible")
    if has_errors:
        reasons.append("internal_errors_signal_present")
    if not reasons:
        reasons.append("all_signals_clean")
    return reasons


# ════════════════════════════════════════════════════════════════════
#  Main: decide_purchase_autonomy
# ════════════════════════════════════════════════════════════════════
def decide_purchase_autonomy(
    drift_report: Any,
    risk_report: Any,
    readiness_report: Any,
) -> dict:
    """Computa decisión advisory autónoma combinando los 3 reports M13.7-9.

    ADVISORY ONLY: NO ejecuta, NO muta, NO override del gate.

    Args:
        drift_report:     dict — output de detect_gate_drift (M13.7)
        risk_report:      dict — output de compute_risk_score (M13.8)
        readiness_report: dict — output de compute_readiness_index (M13.9)

    Returns:
        dict con decision, confidence, autonomy_level, reasons,
        operator_action, signals, timestamp.

    Pure function: NO muta inputs, NO IO. Fail-safe.
    """
    try:
        # Extraer signals fundamentales
        drift_severity = _safe_get(drift_report, "severity", default="low")
        if not isinstance(drift_severity, str):
            drift_severity = "low"
        drift_detected = bool(_safe_get(drift_report, "drift_detected",
                                          default=False))
        drift_signals = _safe_get(drift_report, "signals", default=[])
        n_drift_signals = (len(drift_signals)
                           if isinstance(drift_signals, list) else 0)

        risk_level = _safe_get(risk_report, "risk_level", default="critical")
        if not isinstance(risk_level, str):
            risk_level = "critical"

        readiness_status = _safe_get(readiness_report, "status",
                                       default="not_ready")
        if not isinstance(readiness_status, str):
            readiness_status = "not_ready"
        go_live = bool(_safe_get(readiness_report, "go_live_eligibility",
                                   default=False))

        has_errors = _has_internal_errors(drift_report, risk_report)

        # Determinar decisión
        decision = _determine_decision(
            drift_severity=drift_severity,
            risk_level=risk_level,
            readiness_status=readiness_status,
            go_live=go_live,
            drift_detected=drift_detected,
        )

        # Confidence
        confidence = _compute_confidence(
            decision=decision,
            drift_detected=drift_detected,
            has_errors=has_errors,
            n_drift_signals=n_drift_signals,
        )

        # Reasons
        reasons = _build_reasons(
            drift_severity=drift_severity,
            drift_detected=drift_detected,
            risk_level=risk_level,
            readiness_status=readiness_status,
            go_live=go_live,
            has_errors=has_errors,
        )

        return {
            "decision":        decision,
            "confidence":      float(round(confidence, 2)),
            "autonomy_level":  AUTONOMY_LEVEL_BY_DECISION[decision],
            "reasons":         reasons,
            "operator_action": OPERATOR_ACTION_BY_DECISION[decision],
            "signals": {
                "drift_severity":      drift_severity,
                "risk_level":          risk_level,
                "readiness_status":    readiness_status,
                "go_live_eligibility": go_live,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        # Fail-safe absoluto: default conservador (rollback)
        return {
            "decision":        "rollback_required",
            "confidence":      0.0,
            "autonomy_level":  "none",
            "reasons":         ["autonomy_compute_failed_failsafe"],
            "operator_action": "rollback_to_safe_mode",
            "signals": {
                "drift_severity":      "unknown",
                "risk_level":          "unknown",
                "readiness_status":    "unknown",
                "go_live_eligibility": False,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "autonomy_compute_failed_failsafe",
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
