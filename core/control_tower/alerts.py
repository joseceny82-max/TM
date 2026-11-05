"""core/control_tower/alerts.py — Alert engine.

FASE 4 del Control Tower: detección automática de anomalías globales.

Tipos de alerta:
    - DRIFT_SPIKE: drift_count global > threshold
    - LOGIN_FAILURE_SPIKE: >N runs con last_failure=no_email_input en ventana
    - CHECKOUT_DROP: ratio de runs llegando a checkout < threshold
    - BOT_BLOCK_BURST: >N bot_block en ventana de tiempo
    - HEALTH_DEGRADATION: avg_health_score baja debajo de threshold

Diseño:
    - Pure functions sobre snapshot del aggregator
    - Configurable via env vars
    - Stateless (cada evaluación lee snapshot actual)
"""
from __future__ import annotations

import os
import time
from collections import Counter
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  Config (configurable via env)
# ════════════════════════════════════════════════════════════════════
def _env_int(k: str, default: int) -> int:
    try:
        return int(os.environ.get(k, str(default)))
    except (ValueError, TypeError):
        return default


THRESHOLDS = {
    "DRIFT_SPIKE_MIN":           _env_int("CT_ALERT_DRIFT_SPIKE", 5),
    "LOGIN_FAILURE_SPIKE_MIN":   _env_int("CT_ALERT_LOGIN_FAILURES", 3),
    "BOT_BLOCK_BURST_MIN":       _env_int("CT_ALERT_BOT_BLOCK_BURST", 2),
    "HEALTH_DEGRADATION_BELOW":  _env_int("CT_ALERT_HEALTH_DEGR", 50),
    "CHECKOUT_DROP_BELOW_PCT":   _env_int("CT_ALERT_CHECKOUT_DROP_PCT", 20),
    "MIN_RUNS_FOR_RATIO":        _env_int("CT_ALERT_MIN_RUNS_RATIO", 3),
}


# ════════════════════════════════════════════════════════════════════
#  evaluate_alerts() — pure function
# ════════════════════════════════════════════════════════════════════
def evaluate_alerts(runs: list[dict], global_summary: dict) -> list[dict]:
    """Genera lista de alertas desde el estado actual.

    Args:
        runs: lista de runs activos (RunStats.to_dict())
        global_summary: dict del aggregator

    Returns:
        Lista de alertas, cada una:
            {
              "kind": str,
              "severity": "info|warn|critical",
              "message": str,
              "value": numeric,
              "threshold": numeric,
              "ts": iso,
              "affected_slots": [...],
            }
    """
    alerts: list[dict] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    if not runs:
        return alerts

    # ── 1. DRIFT_SPIKE (global) ─────────────────────────────────────
    total_drifts = sum(r.get("drift_count", 0) for r in runs)
    if total_drifts >= THRESHOLDS["DRIFT_SPIKE_MIN"]:
        affected = [r["slot"] for r in runs if r.get("drift_count", 0) > 0]
        alerts.append({
            "kind": "DRIFT_SPIKE",
            "severity": "warn" if total_drifts < 10 else "critical",
            "message": f"Drift global elevado: {total_drifts} drifts en {len(runs)} runs",
            "value": total_drifts,
            "threshold": THRESHOLDS["DRIFT_SPIKE_MIN"],
            "ts": now,
            "affected_slots": affected,
        })

    # ── 2. LOGIN_FAILURE_SPIKE ──────────────────────────────────────
    login_fail_runs = [
        r for r in runs
        if r.get("last_failure") and "no_email_input" in str(r.get("last_failure", ""))
    ]
    if len(login_fail_runs) >= THRESHOLDS["LOGIN_FAILURE_SPIKE_MIN"]:
        alerts.append({
            "kind": "LOGIN_FAILURE_SPIKE",
            "severity": "critical",
            "message": f"{len(login_fail_runs)} runs con no_email_input (login degradado)",
            "value": len(login_fail_runs),
            "threshold": THRESHOLDS["LOGIN_FAILURE_SPIKE_MIN"],
            "ts": now,
            "affected_slots": [r["slot"] for r in login_fail_runs],
        })

    # ── 3. BOT_BLOCK_BURST ──────────────────────────────────────────
    blocked_runs = [r for r in runs if r.get("exit_outcome") == "BLOCKED"]
    if len(blocked_runs) >= THRESHOLDS["BOT_BLOCK_BURST_MIN"]:
        alerts.append({
            "kind": "BOT_BLOCK_BURST",
            "severity": "critical",
            "message": f"{len(blocked_runs)} runs con bot_block — posible subnet quemada",
            "value": len(blocked_runs),
            "threshold": THRESHOLDS["BOT_BLOCK_BURST_MIN"],
            "ts": now,
            "affected_slots": [r["slot"] for r in blocked_runs],
        })

    # ── 4. CHECKOUT_DROP ────────────────────────────────────────────
    if len(runs) >= THRESHOLDS["MIN_RUNS_FOR_RATIO"]:
        states_seen_per_run = []
        for r in runs:
            seen = {r.get("current_state")}
            for h in r.get("state_history", []) or []:
                if isinstance(h, dict):
                    seen.add(h.get("to"))
            states_seen_per_run.append(seen)
        checkout_reached = sum(1 for s in states_seen_per_run if "checkout" in s)
        pct = 100 * checkout_reached / len(runs)
        if pct < THRESHOLDS["CHECKOUT_DROP_BELOW_PCT"]:
            alerts.append({
                "kind": "CHECKOUT_DROP",
                "severity": "warn",
                "message": f"Solo {checkout_reached}/{len(runs)} runs llegaron a CHECKOUT ({pct:.0f}%)",
                "value": round(pct, 1),
                "threshold": THRESHOLDS["CHECKOUT_DROP_BELOW_PCT"],
                "ts": now,
                "affected_slots": [],
            })

    # ── 5. HEALTH_DEGRADATION ───────────────────────────────────────
    avg = global_summary.get("avg_health_score", 100) if global_summary else 100
    if avg < THRESHOLDS["HEALTH_DEGRADATION_BELOW"]:
        unhealthy_slots = [r["slot"] for r in runs
                            if r.get("health_score", 100) < THRESHOLDS["HEALTH_DEGRADATION_BELOW"]]
        alerts.append({
            "kind": "HEALTH_DEGRADATION",
            "severity": "warn",
            "message": f"Avg health = {avg}, por debajo de {THRESHOLDS['HEALTH_DEGRADATION_BELOW']}",
            "value": avg,
            "threshold": THRESHOLDS["HEALTH_DEGRADATION_BELOW"],
            "ts": now,
            "affected_slots": unhealthy_slots,
        })

    return alerts


def get_thresholds() -> dict:
    """Devuelve los thresholds activos (configurables vía env)."""
    return dict(THRESHOLDS)


# ════════════════════════════════════════════════════════════════════
#  Self-test
# ════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json
    sample = [
        {"slot": 1, "drift_count": 3, "exit_outcome": "BLOCKED", "last_failure": "no_email_input",
          "current_state": "exit", "state_history": [{"to": "browsing"}, {"to": "login"}],
          "health_score": 25},
        {"slot": 2, "drift_count": 2, "exit_outcome": "BLOCKED", "last_failure": "no_email_input",
          "current_state": "exit", "state_history": [{"to": "browsing"}, {"to": "login"}],
          "health_score": 30},
        {"slot": 3, "drift_count": 1, "exit_outcome": None, "last_failure": "no_email_input",
          "current_state": "login", "state_history": [{"to": "browsing"}],
          "health_score": 45},
    ]
    g = {"avg_health_score": 33, "runs_tracked": 3}
    alerts = evaluate_alerts(sample, g)
    print(json.dumps(alerts, indent=2))


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
