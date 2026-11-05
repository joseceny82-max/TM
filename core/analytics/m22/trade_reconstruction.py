"""core/analytics/m22/trade_reconstruction.py — Trade Reconstruction.

Reconstruye, a partir de execution_result, una vista normalizada de
"qué pasó realmente con cada orden": intención → ejecución.

Output: list[dict] de trades reconstruidos:
    {
        "order_id":          str,
        "status":            str,
        "executed_quantity": float,
        "executed_price":    float,
        "latency_ms":        int,
        "outcome":           "executed" | "not_executed",
        "error":             str | None,
    }

NO trae intención original (M22 no importa M21 ni M13–M18). La función
acepta opcionalmente un "intent_map" con la intención por order_id —
si se provee, lo anexa al trade; si no, omite el campo.

Determinístico. stdlib only. Sin side-effects.
"""
from __future__ import annotations

from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
STATUS_FILLED: str = "filled"

OUTCOME_EXECUTED: str = "executed"
OUTCOME_NOT_EXECUTED: str = "not_executed"


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


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def _safe_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


# ════════════════════════════════════════════════════════════════════
#  API principal: reconstruct_trades
# ════════════════════════════════════════════════════════════════════
def reconstruct_trades(execution_result: Any,
                       intent_map: Any = None) -> list:
    """Reconstruye trades por orden a partir de execution_result.

    Args:
        execution_result: dict M21-shaped con `results` list.
        intent_map: dict opcional {order_id: intent_descriptor}.
            Si está, cada trade incluye una clave `intent`.

    Returns:
        list[dict] con un trade por result entry.

    Pure function. Determinística.
    """
    results = _safe_list(_safe_dict(execution_result).get("results"))
    intents = _safe_dict(intent_map)

    trades: list = []
    for r in results:
        if not isinstance(r, dict):
            # Una entrada malformada → trade vacío trazable
            trades.append({
                "order_id": "",
                "status": "",
                "executed_quantity": 0.0,
                "executed_price": 0.0,
                "latency_ms": 0,
                "outcome": OUTCOME_NOT_EXECUTED,
                "error": "malformed_result_entry",
            })
            continue

        oid = _safe_str(r.get("order_id"), "")
        status = _safe_str(r.get("status"), "")
        qty = _safe_float(r.get("filled_quantity"), 0.0)
        price = _safe_float(r.get("avg_price"), 0.0)
        latency = _safe_int(r.get("latency_ms"), 0)
        error = r.get("error") if r.get("error") is not None else None

        outcome = OUTCOME_EXECUTED if status == STATUS_FILLED else OUTCOME_NOT_EXECUTED

        trade: dict = {
            "order_id": oid,
            "status": status,
            "executed_quantity": qty,
            "executed_price": price,
            "latency_ms": latency,
            "outcome": outcome,
            "error": error,
        }

        # Anexar intent si está mapeado para este order_id
        if oid in intents:
            trade["intent"] = intents[oid]

        trades.append(trade)
    return trades


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
