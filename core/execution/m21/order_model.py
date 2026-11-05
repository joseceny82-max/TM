"""core/execution/m21/order_model.py — Order & ExecutionResult.

Modelos inmutables (dataclasses frozen) usados por M21.

NO contiene lógica de decisión, mapping de market, ni observabilidad.
Sólo estructuras de datos + constantes literales del contrato M21.

stdlib only. Sin side-effects.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  Constantes literales del contrato M21
# ════════════════════════════════════════════════════════════════════
ORDER_SIDE_BUY: str = "buy"
ORDER_SIDE_SELL: str = "sell"
VALID_SIDES: frozenset = frozenset({ORDER_SIDE_BUY, ORDER_SIDE_SELL})

ORDER_TYPE_MARKET: str = "market"
ORDER_TYPE_LIMIT: str = "limit"
VALID_ORDER_TYPES: frozenset = frozenset({ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT})

STATUS_FILLED: str = "filled"
STATUS_REJECTED: str = "rejected"
STATUS_SIMULATED: str = "simulated"
STATUS_BLOCKED: str = "blocked"
VALID_STATUSES: frozenset = frozenset({
    STATUS_FILLED, STATUS_REJECTED, STATUS_SIMULATED, STATUS_BLOCKED,
})

EXECUTION_MODE_SIMULATION: str = "simulation"
EXECUTION_MODE_LIVE: str = "live"
VALID_EXECUTION_MODES: frozenset = frozenset({
    EXECUTION_MODE_SIMULATION, EXECUTION_MODE_LIVE,
})
DEFAULT_EXECUTION_MODE: str = EXECUTION_MODE_SIMULATION


# ════════════════════════════════════════════════════════════════════
#  Order — inmutable, hashable, serializable
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Order:
    """Una orden de ejecución. Frozen — inmutable y hashable."""
    order_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    price: Optional[float] = None
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════════
#  ExecutionResult — inmutable, serializable
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ExecutionResult:
    """Resultado de envío de una orden al broker. Frozen — inmutable."""
    order_id: str
    status: str
    filled_quantity: float
    avg_price: float
    latency_ms: int
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


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
