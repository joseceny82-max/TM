"""core/execution/m21/ — M21 Execution Layer.

Capa de ejecución pura que convierte decisiones de M13-M18 en:
    - órdenes (Order)
    - resultados de envío al broker (ExecutionResult)

PRINCIPIO ARQUITECTÓNICO:
    M21 es EXECUTION-ONLY:
        - NUNCA decide
        - NUNCA interpreta estrategia
        - NUNCA consulta market
        - NUNCA importa M19 (adapter)
        - NUNCA importa M20 (fidelity / shadow)
        - NUNCA crea feedback loops

    Garantías:
        E1: Determinismo absoluto — misma decisión → mismo output
        E2: Live mode SIEMPRE bloqueado por defecto
        E3: Stateless funcionalmente (engine = config + dispatch)
        E4: Fail-safe — entrada malformada → output válido vacío
        E5: stdlib only — sin dependencias externas

Public API:
    execute_decision(decision: dict, mode: str = "simulation") -> dict
    Order
    ExecutionResult

Output contract:
    {
        "execution_mode": "simulation" | "live",
        "orders_sent":    int,
        "results": [
            {
                "order_id":        str,
                "status":          "filled" | "rejected" | "simulated" | "blocked",
                "filled_quantity": float,
                "avg_price":       float,
                "latency_ms":      int,
                "error":           str | None,
            },
            ...
        ],
    }

stdlib only. Determinístico. Sin side-effects.
"""
from core.execution.m21.execution_engine import (
    LIVE_MODE_DISABLED_ERROR,
    ExecutionEngine,
    LiveBroker,
)
from core.execution.m21.executor import execute_decision
from core.execution.m21.order_model import (
    DEFAULT_EXECUTION_MODE,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_SIMULATION,
    ORDER_SIDE_BUY,
    ORDER_SIDE_SELL,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    STATUS_BLOCKED,
    STATUS_FILLED,
    STATUS_REJECTED,
    STATUS_SIMULATED,
    ExecutionResult,
    Order,
    VALID_EXECUTION_MODES,
    VALID_ORDER_TYPES,
    VALID_SIDES,
    VALID_STATUSES,
)
from core.execution.m21.simulator import (
    LIMIT_FILL_THRESHOLD,
    MARKET_FILL_THRESHOLD,
    SimulatedBroker,
)

__all__ = [
    # Entry point
    "execute_decision",
    # Models
    "Order",
    "ExecutionResult",
    # Engine + brokers
    "ExecutionEngine",
    "SimulatedBroker",
    "LiveBroker",
    # Constants
    "ORDER_SIDE_BUY",
    "ORDER_SIDE_SELL",
    "ORDER_TYPE_MARKET",
    "ORDER_TYPE_LIMIT",
    "STATUS_FILLED",
    "STATUS_REJECTED",
    "STATUS_SIMULATED",
    "STATUS_BLOCKED",
    "EXECUTION_MODE_SIMULATION",
    "EXECUTION_MODE_LIVE",
    "DEFAULT_EXECUTION_MODE",
    "VALID_SIDES",
    "VALID_ORDER_TYPES",
    "VALID_STATUSES",
    "VALID_EXECUTION_MODES",
    "MARKET_FILL_THRESHOLD",
    "LIMIT_FILL_THRESHOLD",
    "LIVE_MODE_DISABLED_ERROR",
]
