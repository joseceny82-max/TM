"""core/analytics/m22/pnl_engine.py — PnL Engine.

Calcula PnL (Profit and Loss) observacional a partir de execution_result
de M21. Sin acceso a mercado externo, sin estado persistente.

Modelo (semántica de implementation shortfall):
    El PnL realizado de cada orden filled es el negativo del COSTO DE
    EJECUCIÓN (slippage × cantidad ejecutada). Cuanto mayor el slippage,
    más negativo el PnL.

    realized_pnl    = - Σ (slippage_i × filled_quantity_i)   ∀ filled
    unrealized_pnl  = 0    (stateless — sin inventario persistente)
    total_pnl       = realized_pnl + unrealized_pnl

    per_order_pnl[oid] = - slippage[oid] × filled_quantity[oid]
                          si status == "filled"
                       = 0 en cualquier otro estado

Determinístico. stdlib only. Sin random, sin clock, sin IO.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
ROUND_NDIGITS: int = 8


# ════════════════════════════════════════════════════════════════════
#  Helpers fail-safe
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


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ════════════════════════════════════════════════════════════════════
#  API principal: compute_pnl
# ════════════════════════════════════════════════════════════════════
def compute_pnl(execution_result: Any,
                slippage_per_order: Any = None) -> dict:
    """Calcula PnL observacional a partir de execution_result + slippage.

    Args:
        execution_result: dict M21-shaped con `results` list.
        slippage_per_order: dict opcional {order_id: slippage_per_share}.
            Si es None o no provee un order_id, slippage = 0 para ese.

    Returns:
        dict:
            {
                "realized":   float,
                "unrealized": float,
                "total":      float,
                "per_order":  {order_id: pnl},
            }

    Pure function. Determinística.
    """
    slippage_map = _safe_dict(slippage_per_order)
    results = _safe_list(_safe_dict(execution_result).get("results"))

    per_order: dict = {}
    realized: float = 0.0

    for r in results:
        if not isinstance(r, dict):
            continue
        oid_raw = r.get("order_id", "")
        oid = oid_raw if isinstance(oid_raw, str) else str(oid_raw)

        if r.get("status") != "filled":
            per_order[oid] = 0.0
            continue

        qty = _safe_float(r.get("filled_quantity"), 0.0)
        slip = _safe_float(slippage_map.get(oid, 0.0), 0.0)
        order_pnl = -(slip * qty)
        per_order[oid] = round(order_pnl, ROUND_NDIGITS)
        realized += order_pnl

    realized_r = round(realized, ROUND_NDIGITS)
    unrealized_r = 0.0  # stateless — sin tracking de posiciones
    total_r = round(realized_r + unrealized_r, ROUND_NDIGITS)

    return {
        "realized": realized_r,
        "unrealized": unrealized_r,
        "total": total_r,
        "per_order": per_order,
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
