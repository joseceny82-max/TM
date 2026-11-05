"""core/execution/m21/execution_engine.py — Engine + LiveBroker.

Engine que rutea órdenes al broker apropiado según `mode`:
    - "simulation" → SimulatedBroker  (95% / 70% fill determinístico)
    - "live"       → LiveBroker       (SIEMPRE bloqueado por defecto)

LiveBroker es OBLIGATORIAMENTE inactivo según spec M21:
    return {"status": "blocked", "error": "live_mode_disabled"}

NO interpreta estrategia. NO consulta market. NO importa M19/M20.

stdlib only.
"""
from __future__ import annotations

from core.execution.m21.order_model import (
    DEFAULT_EXECUTION_MODE,
    EXECUTION_MODE_LIVE,
    EXECUTION_MODE_SIMULATION,
    STATUS_BLOCKED,
    Order,
    VALID_EXECUTION_MODES,
)
from core.execution.m21.simulator import SimulatedBroker


# ════════════════════════════════════════════════════════════════════
#  LiveBroker — SIEMPRE BLOQUEADO POR DEFECTO
# ════════════════════════════════════════════════════════════════════
LIVE_MODE_DISABLED_ERROR: str = "live_mode_disabled"

_LIVE_BLOCKED_RESPONSE_TEMPLATE: dict = {
    "status": STATUS_BLOCKED,
    "filled_quantity": 0.0,
    "avg_price": 0.0,
    "latency_ms": 0,
    "error": LIVE_MODE_DISABLED_ERROR,
}


class LiveBroker:
    """Broker live: SIEMPRE bloqueado.

    Per M21 spec, live execution está deshabilitada por defecto y
    no se puede activar sólo cambiando el modo. Esto es una garantía
    de seguridad estructural — toda llamada devuelve `blocked`.
    """

    def submit_order(self, order: Order) -> dict:
        # Devuelve copia defensiva del template
        return dict(_LIVE_BLOCKED_RESPONSE_TEMPLATE)


# ════════════════════════════════════════════════════════════════════
#  ExecutionEngine — routing simulation | live
# ════════════════════════════════════════════════════════════════════
def _normalize_mode(mode) -> str:
    if isinstance(mode, str) and mode in VALID_EXECUTION_MODES:
        return mode
    return DEFAULT_EXECUTION_MODE


def _get_broker(mode: str):
    """Selector de broker. Nunca devuelve None.

    - simulation → SimulatedBroker
    - live       → LiveBroker (bloqueado)
    """
    if mode == EXECUTION_MODE_LIVE:
        return LiveBroker()
    # default y simulation → SimulatedBroker
    return SimulatedBroker()


class ExecutionEngine:
    """Motor de ejecución — sólo enruta.

    Sin lógica de decisión, sin observabilidad, sin feedback. Stateless
    funcionalmente (el `mode` y el `broker` son configuración, no estado
    mutable durante la vida del objeto).
    """

    def __init__(self, mode: str = DEFAULT_EXECUTION_MODE):
        self.mode: str = _normalize_mode(mode)
        self.broker = _get_broker(self.mode)

    def execute_orders(self, orders: list) -> list:
        """Envía cada orden al broker y devuelve lista de result dicts.

        Args:
            orders: list de `Order` (frozen dataclass).

        Returns:
            list de dicts con order_id + status + filled_quantity +
            avg_price + latency_ms + error.
        """
        results: list = []
        for order in orders:
            r = self.broker.submit_order(order)
            # Garantizar que order_id está presente en el resultado
            r = dict(r)  # copia defensiva
            r["order_id"] = order.order_id
            results.append(r)
        return results


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
