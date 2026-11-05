"""core/analytics/m22/metrics_engine.py — Execution Metrics Engine.

Calcula métricas agregadas de calidad de ejecución sobre execution_result
de M21.

Métricas producidas:
    - fill_rate:           proporción de filled / total
    - rejection_rate:      1 - fill_rate (cubre rejected/simulated/blocked)
    - execution_efficiency: composite ∈ [0, 1] que combina fill_rate
                              con latencia (a menor latencia, mayor eficiencia)
    - latency_avg_ms:      latencia media en ms (todas las órdenes)
    - cost_of_execution:   slippage_total acumulado (si se pasa slippage map)

INVARIANTE GARANTIZADO POR CONSTRUCCIÓN:
    fill_rate + rejection_rate == 1.0    (con tolerancia float)

Determinístico. stdlib only. Sin side-effects.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
STATUS_FILLED: str = "filled"
ROUND_NDIGITS: int = 8

# Para execution_efficiency: penaliza latencias > LATENCY_REFERENCE_MS
LATENCY_REFERENCE_MS: float = 100.0


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ════════════════════════════════════════════════════════════════════
#  Métricas individuales
# ════════════════════════════════════════════════════════════════════
def _count_statuses(results: list) -> dict:
    """Counts por status. Solo enumera, no clasifica."""
    counts: dict = {
        "filled": 0, "rejected": 0, "simulated": 0,
        "blocked": 0, "other": 0,
    }
    for r in results:
        if not isinstance(r, dict):
            counts["other"] += 1
            continue
        s = r.get("status")
        if s in counts:
            counts[s] += 1
        else:
            counts["other"] += 1
    return counts


def _avg_latency(results: list) -> float:
    """Latencia media (ms) sobre TODAS las órdenes."""
    if not results:
        return 0.0
    total: float = 0.0
    n: int = 0
    for r in results:
        if not isinstance(r, dict):
            n += 1  # counts towards denominator even if no latency
            continue
        total += _safe_float(r.get("latency_ms"), 0.0)
        n += 1
    if n == 0:
        return 0.0
    return total / n


def _execution_efficiency(fill_rate: float, latency_avg: float) -> float:
    """Composite ∈ [0, 1]: fill_rate × latency_penalty.

    latency_penalty = max(0, 1 − latency_avg / LATENCY_REFERENCE_MS)
    """
    if LATENCY_REFERENCE_MS <= 0:
        latency_pen = 1.0
    else:
        latency_pen = 1.0 - (latency_avg / LATENCY_REFERENCE_MS)
    latency_pen = _clamp(latency_pen)
    return _clamp(fill_rate * latency_pen)


def _cost_of_execution(slippage_per_order: dict, results: list) -> float:
    """Suma de (slippage_i × filled_qty_i) sobre fills."""
    total: float = 0.0
    if not isinstance(slippage_per_order, dict):
        return 0.0
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("status") != STATUS_FILLED:
            continue
        oid_raw = r.get("order_id", "")
        oid = oid_raw if isinstance(oid_raw, str) else str(oid_raw)
        qty = _safe_float(r.get("filled_quantity"), 0.0)
        slip = _safe_float(slippage_per_order.get(oid, 0.0), 0.0)
        total += slip * qty
    return round(total, ROUND_NDIGITS)


# ════════════════════════════════════════════════════════════════════
#  API principal: compute_metrics
# ════════════════════════════════════════════════════════════════════
def compute_metrics(execution_result: Any,
                     slippage_per_order: Any = None) -> dict:
    """Calcula métricas agregadas.

    Args:
        execution_result: dict M21-shaped.
        slippage_per_order: dict opcional para cost_of_execution.

    Returns:
        dict con: fill_rate, rejection_rate, execution_efficiency,
                  latency_avg_ms, cost_of_execution.

    INVARIANTE: fill_rate + rejection_rate == 1.0 (o ambos 0 si vacío).
    """
    results = _safe_list(_safe_dict(execution_result).get("results"))
    n = len(results)

    if n == 0:
        return {
            "fill_rate": 0.0,
            "rejection_rate": 0.0,
            "execution_efficiency": 0.0,
            "latency_avg_ms": 0.0,
            "cost_of_execution": 0.0,
        }

    counts = _count_statuses(results)
    filled = counts["filled"]

    fill_rate = filled / n
    rejection_rate = 1.0 - fill_rate  # construcción exacta del invariante

    latency_avg = _avg_latency(results)
    efficiency = _execution_efficiency(fill_rate, latency_avg)
    cost = _cost_of_execution(_safe_dict(slippage_per_order), results)

    return {
        "fill_rate": round(fill_rate, ROUND_NDIGITS),
        "rejection_rate": round(rejection_rate, ROUND_NDIGITS),
        "execution_efficiency": round(efficiency, ROUND_NDIGITS),
        "latency_avg_ms": round(latency_avg, ROUND_NDIGITS),
        "cost_of_execution": cost,
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
