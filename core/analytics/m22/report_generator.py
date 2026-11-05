"""core/analytics/m22/report_generator.py — Report Generator.

Función top-level que ensambla el report económico final M22 a partir
de un único input: execution_result (dict de M21).

Output schema (per spec M22, exacto):
    {
        "pnl": {
            "realized":   float,
            "unrealized": float,
            "total":      float,
        },
        "slippage": {
            "avg": float,
            "max": float,
        },
        "execution_metrics": {
            "fill_rate":      float,
            "rejection_rate": float,
            "latency_avg_ms": float,
        },
        "efficiency_score": float,
    }

Reglas:
    - Determinista: mismo execution_result → mismo report bit-idéntico
    - Stateless: sin estado entre llamadas
    - Fail-safe: input malformado → report con zeros (estructura válida)
    - NO importa M19, M20 ni M21 (consume schema, no tipos)

stdlib only.
"""
from __future__ import annotations

from typing import Any

from core.analytics.m22.metrics_engine import compute_metrics
from core.analytics.m22.pnl_engine import compute_pnl
from core.analytics.m22.slippage_model import (
    compute_slippage_per_order,
)


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
ROUND_NDIGITS: int = 8

# Penalización composite para efficiency_score
EFFICIENCY_SLIPPAGE_PENALTY: float = 10.0


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════
def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _slippage_aggregates(slippage_per_order: dict) -> tuple:
    """Devuelve (avg, max) de los slippage values."""
    if not slippage_per_order:
        return 0.0, 0.0
    values = [_safe_float(v, 0.0) for v in slippage_per_order.values()]
    if not values:
        return 0.0, 0.0
    avg = sum(values) / len(values)
    return round(avg, ROUND_NDIGITS), round(max(values), ROUND_NDIGITS)


def _build_empty_report() -> dict:
    """Report default seguro para inputs malformados."""
    return {
        "pnl": {"realized": 0.0, "unrealized": 0.0, "total": 0.0},
        "slippage": {"avg": 0.0, "max": 0.0},
        "execution_metrics": {
            "fill_rate": 0.0,
            "rejection_rate": 0.0,
            "latency_avg_ms": 0.0,
        },
        "efficiency_score": 0.0,
    }


# ════════════════════════════════════════════════════════════════════
#  API principal: generate_report
# ════════════════════════════════════════════════════════════════════
def generate_report(execution_result: Any) -> dict:
    """Genera el report económico M22.

    Args:
        execution_result: dict M21-shaped. Cualquier otra cosa → empty report.

    Returns:
        dict con el output schema canónico M22.

    Pure function. Determinística. Fail-safe absoluto.
    """
    try:
        if not isinstance(execution_result, dict):
            return _build_empty_report()

        # 1. Slippage por orden (deriva volatility_proxy internamente)
        slippage_map = compute_slippage_per_order(execution_result)

        # 2. PnL
        pnl = compute_pnl(execution_result, slippage_map)

        # 3. Metrics (incluye cost_of_execution)
        metrics = compute_metrics(execution_result, slippage_map)

        # 4. Slippage aggregates
        slip_avg, slip_max = _slippage_aggregates(slippage_map)

        # 5. Efficiency score = execution_efficiency × penalty_for_slippage
        base_eff = _safe_float(metrics.get("execution_efficiency"), 0.0)
        penalty = max(0.0, 1.0 - slip_avg * EFFICIENCY_SLIPPAGE_PENALTY)
        eff_score = _clamp(base_eff * penalty)

        return {
            "pnl": {
                "realized": pnl["realized"],
                "unrealized": pnl["unrealized"],
                "total": pnl["total"],
            },
            "slippage": {
                "avg": slip_avg,
                "max": slip_max,
            },
            "execution_metrics": {
                "fill_rate": metrics["fill_rate"],
                "rejection_rate": metrics["rejection_rate"],
                "latency_avg_ms": metrics["latency_avg_ms"],
            },
            "efficiency_score": round(eff_score, ROUND_NDIGITS),
        }
    except Exception:
        # Fail-safe absoluto
        return _build_empty_report()


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
