"""core/execution/m21/simulator.py — SimulatedBroker.

Broker simulado con comportamiento DETERMINISTA y reproducible:

    - market orders: fill rate 95% (per spec)
    - limit  orders: fill rate 70% (per spec)
    - quantity <= 0 → rejected
    - type inválido → rejected
    - sin random; decisión por hash determinístico del order_id

NO usa `random`, NO importa M19/M20, NO hace IO.

stdlib only. Pure function-ish (clase pero stateless funcionalmente).
"""
from __future__ import annotations

import hashlib

from core.execution.m21.order_model import (
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    STATUS_FILLED,
    STATUS_REJECTED,
    STATUS_SIMULATED,
    Order,
    VALID_ORDER_TYPES,
)


# ════════════════════════════════════════════════════════════════════
#  Fill thresholds (spec literal — no calibrables)
# ════════════════════════════════════════════════════════════════════
MARKET_FILL_THRESHOLD: int = 95  # hash bucket < 95 → fill
LIMIT_FILL_THRESHOLD: int = 70   # hash bucket < 70 → fill

# Latency bounds for deterministic simulation
LATENCY_MIN_MS: int = 5
LATENCY_MAX_MS: int = 50
LATENCY_RANGE: int = LATENCY_MAX_MS - LATENCY_MIN_MS + 1  # inclusive

# Mock price range for orders sin price explícito
MOCK_PRICE_BASE: float = 10.0
MOCK_PRICE_SPAN: float = 990.0  # [10.0, 1000.0]

# Error literals
ERR_INVALID_QUANTITY: str = "invalid_quantity"
ERR_INVALID_ORDER_TYPE: str = "invalid_order_type"
ERR_NO_FILL: str = "no_fill_simulated"


# ════════════════════════════════════════════════════════════════════
#  Helpers determinísticos
# ════════════════════════════════════════════════════════════════════
def _det_hash(seed: str) -> int:
    """SHA-256 → entero estable (64-bit unsigned).

    Determinístico: misma seed → mismo entero. Sin random.
    """
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _fill_decision(order: Order) -> bool:
    """Decisión de fill determinística por hash del order_id + atributos."""
    seed = f"fill:{order.order_id}:{order.symbol}:{order.side}"
    bucket = _det_hash(seed) % 100  # 0..99 uniforme
    if order.order_type == ORDER_TYPE_MARKET:
        return bucket < MARKET_FILL_THRESHOLD
    if order.order_type == ORDER_TYPE_LIMIT:
        return bucket < LIMIT_FILL_THRESHOLD
    return False


def _latency_ms(order: Order) -> int:
    """Latencia simulada determinística en [LATENCY_MIN, LATENCY_MAX]."""
    seed = f"latency:{order.order_id}"
    return LATENCY_MIN_MS + (_det_hash(seed) % LATENCY_RANGE)


def _avg_price(order: Order) -> float:
    """Precio promedio de ejecución.

    Si la orden trae `price`, se respeta literal. Si no, mock determinístico
    en [MOCK_PRICE_BASE, MOCK_PRICE_BASE + MOCK_PRICE_SPAN].
    """
    if order.price is not None:
        return float(order.price)
    seed = f"price:{order.symbol}"
    pct = (_det_hash(seed) % 10000) / 10000.0  # [0, 1)
    return float(round(MOCK_PRICE_BASE + pct * MOCK_PRICE_SPAN, 4))


# ════════════════════════════════════════════════════════════════════
#  SimulatedBroker
# ════════════════════════════════════════════════════════════════════
class SimulatedBroker:
    """Broker simulado.

    Implementa el Protocol `BrokerInterface` por duck typing.
    Sin estado mutable. Pure function-ish.
    """

    def submit_order(self, order: Order) -> dict:
        # Pre-flight: quantity
        if not isinstance(order.quantity, (int, float)) or order.quantity <= 0:
            return {
                "status": STATUS_REJECTED,
                "filled_quantity": 0.0,
                "avg_price": 0.0,
                "latency_ms": 0,
                "error": ERR_INVALID_QUANTITY,
            }
        # Pre-flight: order_type
        if order.order_type not in VALID_ORDER_TYPES:
            return {
                "status": STATUS_REJECTED,
                "filled_quantity": 0.0,
                "avg_price": 0.0,
                "latency_ms": 0,
                "error": f"{ERR_INVALID_ORDER_TYPE}:{order.order_type}",
            }
        # Fill simulation
        latency = _latency_ms(order)
        if _fill_decision(order):
            return {
                "status": STATUS_FILLED,
                "filled_quantity": float(order.quantity),
                "avg_price": _avg_price(order),
                "latency_ms": latency,
                "error": None,
            }
        # No-fill outcome (deterministic per hash)
        return {
            "status": STATUS_SIMULATED,
            "filled_quantity": 0.0,
            "avg_price": 0.0,
            "latency_ms": latency,
            "error": ERR_NO_FILL,
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
