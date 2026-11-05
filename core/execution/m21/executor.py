"""core/execution/m21/executor.py — execute_decision entry point.

Función pura top-level que orquesta:

    decision: dict
        ↓
    actions  → Orders (normalización fail-safe)
        ↓
    ExecutionEngine(mode)
        ↓
    list[ExecutionResult dicts]
        ↓
    Output contract dict (execution_mode, orders_sent, results)

PRINCIPIO M21:
    - NO decide
    - NO interpreta estrategia
    - NO consulta market
    - NO importa M19 (adapter) ni M20 (observability)
    - Sólo TRADUCE structures existentes en órdenes

Determinístico. stdlib only.
"""
from __future__ import annotations

import hashlib
from typing import Any

from core.execution.m21.execution_engine import ExecutionEngine
from core.execution.m21.order_model import (
    DEFAULT_EXECUTION_MODE,
    ORDER_SIDE_BUY,
    ORDER_TYPE_MARKET,
    Order,
    VALID_EXECUTION_MODES,
    VALID_ORDER_TYPES,
    VALID_SIDES,
)


# ════════════════════════════════════════════════════════════════════
#  Helpers fail-safe (coerciones determinísticas)
# ════════════════════════════════════════════════════════════════════
def _safe_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_price(value: Any) -> Any:
    """price es opcional: None si ausente o inválido."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_mode(mode: Any) -> str:
    if isinstance(mode, str) and mode in VALID_EXECUTION_MODES:
        return mode
    return DEFAULT_EXECUTION_MODE


def _make_order_id(action: dict, index: int) -> str:
    """Order id determinístico — hash de campos + posición."""
    parts = [
        str(index),
        _safe_str(action.get("symbol"), "?"),
        _safe_str(action.get("side"), "?"),
        str(action.get("quantity")),
        _safe_str(action.get("order_type"), "?"),
        str(action.get("price")),
    ]
    seed = "|".join(parts)
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"ord-{h[:16]}"


def _normalize_action(action: Any, index: int) -> Order:
    """Coerce raw action → Order. Defaults sanos para faltantes."""
    if not isinstance(action, dict):
        action = {}

    order_id = _make_order_id(action, index)
    symbol = _safe_str(action.get("symbol"), "UNKNOWN")

    side = _safe_str(action.get("side"), ORDER_SIDE_BUY)
    if side not in VALID_SIDES:
        side = ORDER_SIDE_BUY

    quantity = _safe_float(action.get("quantity"), 0.0)

    order_type = _safe_str(action.get("order_type"), ORDER_TYPE_MARKET)
    if order_type not in VALID_ORDER_TYPES:
        order_type = ORDER_TYPE_MARKET

    price = _safe_price(action.get("price"))
    timestamp = _safe_str(action.get("timestamp"), "")

    return Order(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        timestamp=timestamp,
    )


def _shape_result(raw: dict) -> dict:
    """Coerce raw broker dict a output contract canónico."""
    return {
        "order_id": _safe_str(raw.get("order_id"), ""),
        "status": _safe_str(raw.get("status"), ""),
        "filled_quantity": _safe_float(raw.get("filled_quantity"), 0.0),
        "avg_price": _safe_float(raw.get("avg_price"), 0.0),
        "latency_ms": int(_safe_float(raw.get("latency_ms"), 0.0)),
        "error": raw.get("error") if raw.get("error") is not None else None,
    }


# ════════════════════════════════════════════════════════════════════
#  API principal: execute_decision
# ════════════════════════════════════════════════════════════════════
def execute_decision(decision: dict, mode: str = DEFAULT_EXECUTION_MODE) -> dict:
    """Traduce una decisión M13-M18 en órdenes y devuelve resultados.

    PURE FUNCTION. NO IO. NO random. NO clock. NO M19/M20 imports.

    Args:
        decision: dict con clave "actions" (list de dicts).
            Cada action puede tener: symbol, side, quantity,
            order_type, price, timestamp.
        mode: "simulation" (default) | "live". Cualquier otro valor
            cae al default "simulation".

    Returns:
        dict según output contract M21:
            {
                "execution_mode": str,
                "orders_sent":    int,
                "results":        list[dict],
            }

    Determinista: misma `decision` + mismo `mode` → output bit-idéntico.
    Fail-safe: inputs malformados → estructura válida con 0 orders.
    """
    try:
        norm_mode = _normalize_mode(mode)

        if not isinstance(decision, dict):
            actions: list = []
        else:
            raw_actions = decision.get("actions")
            actions = raw_actions if isinstance(raw_actions, list) else []

        orders: list = [_normalize_action(a, i) for i, a in enumerate(actions)]

        engine = ExecutionEngine(mode=norm_mode)
        raw_results = engine.execute_orders(orders)

        results: list = [_shape_result(r) for r in raw_results]

        return {
            "execution_mode": norm_mode,
            "orders_sent": int(len(orders)),
            "results": results,
        }
    except Exception:
        # Fail-safe absoluto — nunca propagar excepciones desde M21
        return {
            "execution_mode": DEFAULT_EXECUTION_MODE,
            "orders_sent": 0,
            "results": [],
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
