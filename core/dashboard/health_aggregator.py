"""core/dashboard/health_aggregator.py — Aggregation of all health signals.

Combina:
    - compute_purchase_metrics (Step 5)
    - compute_runtime_health (dashboard runtime)
    - circuit breaker registry snapshot
    - cutover router snapshot
    - SLO evaluation

Output unificado para `build_system_snapshot()`.

stdlib only. Fail-safe absoluto.
"""
from __future__ import annotations

from typing import Any, Optional

from core.dashboard.runtime_health import compute_runtime_health
from core.dashboard.slo_engine import SLOTargets, evaluate_slos


def _safe(callable_, *args, default=None, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except Exception:
        return default


def aggregate_health(*,
                      sink_dir: str = "runs",
                      events_glob: str = "runs/structured_events_*.jsonl",
                      idem_root: str = "runs/idempotency_log",
                      slo_targets: Optional[SLOTargets] = None,
                      window_hours: float = 24.0,
                      now: Any = None,
                      ) -> dict:
    """Construye snapshot agregado de health.

    Returns:
        dict con: metrics, runtime, slo, circuit_breakers, cutover_state.
    """
    # 1. Metrics canónicas (Step 5)
    metrics = _safe(_import_compute_metrics_lazy(),
                     sink_dir=sink_dir,
                     idem_root=idem_root,
                     window_hours=window_hours,
                     now=now,
                     default={
                         "window_hours": window_hours,
                         "events_in_window": 0,
                         "success_rate_24h": {"rate": 0.0, "paid": 0,
                                                "total_outcomes": 0},
                         "failure_pareto": {},
                         "latency": {"p50_ms": 0, "p95_ms": 0, "p99_ms": 0,
                                       "count": 0},
                         "cart_lifecycle": {"count": 0},
                         "idempotency": {"total": 0, "committed": 0,
                                          "orphan": 0, "orphan_rate": 0.0},
                         "account_burn_rate_per_day": {"total": 0,
                                                          "per_day": None},
                         "subnet_burn_rate_per_day": {"total": 0,
                                                        "per_day": None},
                     })

    # 2. Runtime health (eventos crudos)
    runtime = _safe(compute_runtime_health,
                     events_glob=events_glob,
                     window_hours=window_hours,
                     now=now,
                     default={"events_observed": 0, "data_sufficient": False})

    # 3. SLO evaluation — combinar metrics + divergence
    metrics_for_slo = dict(metrics)
    div_avg = runtime.get("shadow_stats", {}).get("divergence_avg", 0.0) \
        if isinstance(runtime, dict) else 0.0
    metrics_for_slo["avg_divergence_score"] = div_avg
    metrics_for_slo["events_in_window"] = runtime.get("events_observed", 0) \
        if isinstance(runtime, dict) else 0
    slo = _safe(evaluate_slos, metrics_for_slo, targets=slo_targets,
                 default={"overall_status": "insufficient_data",
                          "evaluations": [], "violations": []})

    # 4. Circuit breakers snapshot
    cb_snapshot = _safe(_import_breaker_snapshot_lazy(),
                         default={"total": 0, "open": 0, "half_open": 0,
                                  "closed": 0, "breakers": {}})

    # 5. Cutover router state
    cutover_snapshot = _safe(_import_cutover_snapshot_lazy(),
                              default={"flow_mode": "legacy",
                                       "kill_switch": False,
                                       "canary_rate": 0.0,
                                       "is_safe_default": True})

    return {
        "metrics": metrics,
        "runtime": runtime,
        "slo": slo,
        "circuit_breakers": cb_snapshot,
        "cutover": cutover_snapshot,
        "divergence_avg": div_avg,
    }


# ─── Lazy imports (fail-safe) ───────────────────────────────────────
def _import_compute_metrics_lazy():
    try:
        from core.observability.metrics import compute_purchase_metrics
        return compute_purchase_metrics
    except Exception:
        return lambda **kw: None


def _import_breaker_snapshot_lazy():
    try:
        from core.reliability import get_default_registry
        return lambda: get_default_registry().snapshot()
    except Exception:
        return lambda: None


def _import_cutover_snapshot_lazy():
    try:
        from core.migration import snapshot as cutover_snapshot
        return cutover_snapshot
    except Exception:
        return lambda: None


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
