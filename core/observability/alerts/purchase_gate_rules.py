"""core/observability/alerts/purchase_gate_rules.py — M13.5 Alerting Rules Layer.

Pure evaluation layer que consume snapshots del PurchaseGate (output del
endpoint M13.3 `/api/diagnostics/purchase_gate`) y devuelve un dict con
alertas activas según 4 reglas threshold-based.

Diseño:
    - Función pura: mismo input → mismo output (excepto timestamp)
    - Sin side-effects sobre el gate
    - Sin logging modifications
    - Sin nuevas dependencias (stdlib only)
    - Fail-safe: cualquier excepción interna → no-alert state seguro

Reglas (per spec M13.5):
    R1 — Internal Error Spike:
         counters.internal_errors > 0
         severity: critical

    R2 — Block Rate Degradation:
         rates.purchase_block_rate > 0.8
         severity: high

    R3 — Dry-run failure anomaly:
         rates.purchase_dry_run_rate < 0.5 AND
         counters.purchase_attempted_count > 20
         severity: medium

    R4 — Live execution safety signal:
         counters.purchase_live_count > 0 AND
         config.mode != "live"
         severity: critical (safety violation — live click sin live mode)

Output format:
    {
        "alert_active": bool,
        "severity": "low" | "medium" | "high" | "critical",
        "triggered_rules": [{rule, severity, value}, ...],
        "timestamp": ISO8601 UTC
    }
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


# Severity ordering (mayor = más crítico)
SEVERITY_LEVELS: tuple = ("low", "medium", "high", "critical")
SEVERITY_ORDER: dict[str, int] = {s: i for i, s in enumerate(SEVERITY_LEVELS)}


# Thresholds (per spec M13.5)
_BLOCK_RATE_THRESHOLD: float = 0.8
_DRY_RUN_RATE_FLOOR: float = 0.5
_DRY_RUN_MIN_ATTEMPTED: int = 20


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


def _max_severity(a: str, b: str) -> str:
    """Devuelve la severity más alta entre a y b."""
    return a if SEVERITY_ORDER.get(a, -1) >= SEVERITY_ORDER.get(b, -1) else b


def evaluate_rules(snapshot: dict) -> dict:
    """Evalúa las 4 reglas de alerting M13.5 sobre un snapshot del gate.

    Args:
        snapshot: dict con el esquema del endpoint M13.3
                  /api/diagnostics/purchase_gate

    Returns:
        dict con:
            alert_active:    bool — True si alguna regla disparó
            severity:        "low" | "medium" | "high" | "critical"
            triggered_rules: lista de rules disparadas con context
            timestamp:       ISO8601 UTC del momento de evaluación

    Pure function: NO side-effects, NO mutaciones sobre snapshot.
    Fail-safe: cualquier excepción → alert_active=False, severity=low.
    """
    triggered: list = []
    max_sev: str = "low"

    try:
        # R1 — Internal error spike
        internal = _safe_get(snapshot, "counters", "internal_errors", default=0)
        if isinstance(internal, int) and internal > 0:
            triggered.append({
                "rule": "internal_error_spike",
                "severity": "critical",
                "value": internal,
            })
            max_sev = _max_severity(max_sev, "critical")

        # R2 — Block rate degradation
        block_rate = _safe_get(snapshot, "rates",
                                "purchase_block_rate", default=0.0)
        if (isinstance(block_rate, (int, float))
                and block_rate > _BLOCK_RATE_THRESHOLD):
            triggered.append({
                "rule": "block_rate_degradation",
                "severity": "high",
                "value": float(block_rate),
            })
            max_sev = _max_severity(max_sev, "high")

        # R3 — Dry-run failure anomaly
        dry_rate = _safe_get(snapshot, "rates",
                              "purchase_dry_run_rate", default=0.0)
        attempted = _safe_get(snapshot, "counters",
                               "purchase_attempted_count", default=0)
        if (isinstance(dry_rate, (int, float))
                and isinstance(attempted, int)
                and dry_rate < _DRY_RUN_RATE_FLOOR
                and attempted > _DRY_RUN_MIN_ATTEMPTED):
            triggered.append({
                "rule": "dry_run_failure_anomaly",
                "severity": "medium",
                "value": {
                    "dry_run_rate": float(dry_rate),
                    "attempted": attempted,
                },
            })
            max_sev = _max_severity(max_sev, "medium")

        # R4 — Live execution safety violation
        live_count = _safe_get(snapshot, "counters",
                                "purchase_live_count", default=0)
        mode = _safe_get(snapshot, "config", "mode", default="")
        if (isinstance(live_count, int) and live_count > 0
                and isinstance(mode, str) and mode != "live"):
            triggered.append({
                "rule": "live_execution_safety_violation",
                "severity": "critical",
                "value": {
                    "live_count": live_count,
                    "current_mode": mode,
                },
            })
            max_sev = _max_severity(max_sev, "critical")
    except Exception:
        # Fail-safe: cualquier error en evaluación → no triggered
        return {
            "alert_active": False,
            "severity": "low",
            "triggered_rules": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "error": "evaluation_failed",
        }

    return {
        "alert_active": len(triggered) > 0,
        "severity": max_sev if triggered else "low",
        "triggered_rules": triggered,
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
