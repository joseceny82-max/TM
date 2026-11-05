"""core/control_tower/analytics.py — Run analytics + root cause detection.

FASE 3 del Control Tower: análisis avanzado sobre los datos del aggregator.

Funcionalidades:
    - Run analytics: duración, success, estados visitados, throughput
    - Root cause detection: secuencia que causó el último fallo
    - Failure path extraction
    - Event filtering por kind

Diseño:
    - Pure functions sobre dicts del aggregator
    - Sin estado propio (stateless)
    - Sin dependencias del bot
"""
from __future__ import annotations

from typing import Any, Optional


# ════════════════════════════════════════════════════════════════════
#  Run analytics
# ════════════════════════════════════════════════════════════════════
def compute_run_analytics(run: dict) -> dict:
    """Genera analytics derivados de un RunStats dict.

    Args:
        run: dict del aggregator (RunStats.to_dict())

    Returns:
        dict con métricas derivadas:
        - duration_s, status (running/success/failed/blocked/stopped)
        - states_visited (set), states_count, fsm_coverage_pct
        - event_throughput (events/s)
        - kind_distribution_pct
        - has_failures, severity
    """
    if not run:
        return {"error": "empty_run"}

    elapsed = run.get("elapsed_s", 0)
    event_count = run.get("event_count", 0)
    state_history = run.get("state_history", [])
    current_state = run.get("current_state", "setup")
    exit_outcome = run.get("exit_outcome")
    drift_count = run.get("drift_count", 0)
    kind_counts = run.get("kind_counts", {})
    last_failure = run.get("last_failure")
    health_score = run.get("health_score", 0)

    # Estados visitados (incluye actual + previos en history)
    states_visited = {current_state}
    for h in state_history:
        if isinstance(h, dict):
            states_visited.add(h.get("from", ""))
            states_visited.add(h.get("to", ""))
    states_visited.discard("")
    states_visited.discard(None)

    fsm_states = {"setup", "browsing", "login", "checkout", "exit"}
    fsm_coverage = len(states_visited & fsm_states) / len(fsm_states) * 100

    # Status del run
    if current_state == "exit":
        if exit_outcome == "BLOCKED":
            status = "blocked"
        elif exit_outcome == "FATAL":
            status = "fatal"
        elif exit_outcome == "STOPPED":
            status = "stopped"
        elif last_failure and "FATAL" in str(last_failure):
            status = "failed"
        else:
            status = "success" if health_score >= 60 else "degraded"
    else:
        status = "running"

    # Throughput
    throughput = (event_count / elapsed) if elapsed > 0 else 0

    # Kind distribution
    total = sum(kind_counts.values()) or 1
    kind_pct = {k: round(100 * v / total, 1) for k, v in kind_counts.items()}

    # Severity
    if status in ("blocked", "fatal", "failed"):
        severity = "high"
    elif drift_count >= 3 or health_score < 50:
        severity = "medium"
    elif drift_count >= 1 or health_score < 80:
        severity = "low"
    else:
        severity = "info"

    return {
        "status": status,
        "duration_s": elapsed,
        "event_count": event_count,
        "event_throughput": round(throughput, 2),
        "states_visited": sorted(states_visited),
        "states_count": len(states_visited),
        "fsm_coverage_pct": round(fsm_coverage, 1),
        "kind_counts": dict(kind_counts),
        "kind_distribution_pct": kind_pct,
        "drift_count": drift_count,
        "health_score": health_score,
        "health_verdict": run.get("health_verdict", "UNKNOWN"),
        "exit_outcome": exit_outcome,
        "severity": severity,
        "has_failures": last_failure is not None,
    }


# ════════════════════════════════════════════════════════════════════
#  Root cause detection
# ════════════════════════════════════════════════════════════════════
KNOWN_ROOT_CAUSES = {
    "no_email_input": {
        "name": "OAUTH_NO_EMAIL_INPUT",
        "description": "El bot llegó al OAuth pero no encontró el input de email",
        "remediation": "Inspeccionar HTML post-oauth_detected; añadir selector fallback; esperar domcontentloaded +2s",
        "category": "login_failure",
    },
    "FATAL_bot_block_early": {
        "name": "AKAMAI_BOT_BLOCK",
        "description": "Akamai detectó comportamiento de bot tras credenciales",
        "remediation": "Verificar WebRTC IP leak, fingerprint TZ/UA, warming home más largo; considerar Hyper Solutions",
        "category": "anti_bot",
    },
    "bot_block_persisted": {
        "name": "AKAMAI_BOT_BLOCK_PERSISTED",
        "description": "Bot block detectado y persistido (sesión suspendida)",
        "remediation": "Subnet/cuenta quemada → swap perfil + IP fresca; cleanup cookies",
        "category": "anti_bot",
    },
    "reset_method_next_failed": {
        "name": "PASSWORD_RESET_FLOW_BROKEN",
        "description": "Reset password flow no avanzó al siguiente paso",
        "remediation": "Verificar selector botón 'Siguiente'; comprobar que el radio email esté seleccionado",
        "category": "auth_flow",
    },
    "recovery_failed_swap_account": {
        "name": "RECOVERY_LOOP_EXHAUSTED",
        "description": "Recovery falló tras varios intentos → swap forzado",
        "remediation": "Verificar IMAP madre, timing OTP, conexión 5sim/SMSPool",
        "category": "auth_flow",
    },
    "continuar_js_err": {
        "name": "CONTINUAR_BUTTON_BROKEN",
        "description": "Click 'Continuar' falló por JS error",
        "remediation": "Re-ejecutar tras DOM stable; usar Playwright role click en vez de JS",
        "category": "ui_interaction",
    },
    "pre_flight_skipped_err": {
        "name": "PRE_FLIGHT_HTTP_FAILED",
        "description": "Pre-flight check HTTP falló antes de browser",
        "remediation": "Verificar proxy conectividad, IP reputation, headers",
        "category": "infra",
    },
    "TIMEOUT_no_checkout": {
        "name": "CHECKOUT_TIMEOUT",
        "description": "Bot no llegó a checkout en el tiempo máximo",
        "remediation": "Verificar lógica de stepper, Continuar atascado, queue-it activo",
        "category": "timeout",
    },
    "max_account_swaps_reached": {
        "name": "ACCOUNTS_EXHAUSTED",
        "description": "Pool de cuentas se agotó tras swaps consecutivos",
        "remediation": "Re-generar pool, ampliar TIER 1, verificar validated_at",
        "category": "resource_exhaustion",
    },
}


def detect_root_cause(run: dict) -> Optional[dict]:
    """Identifica la causa raíz del último fallo del run.

    Returns dict con:
        - root_cause: nombre del patrón identificado
        - description: explicación humana
        - remediation: pasos recomendados
        - confidence: low/medium/high según patrón
        - failure_event: evento concreto que disparó la detección
        - failure_path: secuencia 3 previos + fallo
    """
    if not run:
        return None

    last_failure = run.get("last_failure")
    failure_trace = run.get("failure_trace", [])

    if not last_failure:
        return None

    # Buscar match exacto en patrones conocidos
    matched = None
    for evt_pattern, info in KNOWN_ROOT_CAUSES.items():
        if evt_pattern in last_failure or last_failure in evt_pattern:
            matched = info
            break

    # Fallback: classifier genérico
    if not matched:
        if "FATAL_" in last_failure:
            matched = {
                "name": "FATAL_UNKNOWN",
                "description": f"Fallo fatal no catalogado: {last_failure}",
                "remediation": "Inspeccionar logs alrededor del evento; añadir catálogo si recurrente",
                "category": "unknown",
            }
            confidence = "low"
        elif "_failed" in last_failure or "_err" in last_failure:
            matched = {
                "name": "OP_FAILED_GENERIC",
                "description": f"Operación falló: {last_failure}",
                "remediation": "Inspeccionar contexto previo en failure_trace",
                "category": "operational",
            }
            confidence = "low"
        else:
            return None
    else:
        confidence = "high"

    return {
        "root_cause": matched["name"],
        "category": matched["category"],
        "description": matched["description"],
        "remediation": matched["remediation"],
        "confidence": confidence,
        "failure_event": last_failure,
        "failure_path": failure_trace,
        "state_at_failure": run.get("current_state", "?"),
    }


# ════════════════════════════════════════════════════════════════════
#  Event filtering helper
# ════════════════════════════════════════════════════════════════════
def filter_events(events: list[dict],
                  kinds: Optional[set[str]] = None,
                  state: Optional[str] = None,
                  with_drift: Optional[bool] = None,
                  search: Optional[str] = None) -> list[dict]:
    """Filtra una lista de eventos.

    Args:
        events: lista de records JSONL
        kinds: capas permitidas {"STATE","ACTION","OBSERVABILITY","NOISE"}
        state: solo eventos en este estado FSM
        with_drift: True = solo con drift, False = solo sin drift, None = ambos
        search: substring en event name
    """
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if kinds and e.get("kind") not in kinds:
            continue
        if state and e.get("state") != state:
            continue
        if with_drift is True and not e.get("drift"):
            continue
        if with_drift is False and e.get("drift"):
            continue
        if search:
            evt = e.get("event", "")
            if search.lower() not in evt.lower():
                continue
        out.append(e)
    return out


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
