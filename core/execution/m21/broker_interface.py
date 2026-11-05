"""core/execution/m21/broker_interface.py — Broker Protocol.

Contrato estructural (Protocol PEP 544) que toda implementación de
broker dentro de M21 debe satisfacer:

    submit_order(order: Order) -> dict

El dict de retorno debe contener las claves:
    - status          (str): "filled" | "rejected" | "simulated" | "blocked"
    - filled_quantity (float): cantidad efectivamente ejecutada
    - avg_price       (float): precio medio de ejecución
    - latency_ms      (int):   latencia simulada o real
    - error           (str | None): mensaje de error si aplica

stdlib only. Sin lógica. Sólo contrato estructural.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.execution.m21.order_model import Order


@runtime_checkable
class BrokerInterface(Protocol):
    """Protocolo estructural para brokers M21.

    Cualquier clase con un método `submit_order(order: Order) -> dict`
    satisface este Protocol — sin necesidad de herencia explícita.
    """

    def submit_order(self, order: Order) -> dict:
        """Envía la orden al broker (real o simulado) y devuelve el resultado.

        Args:
            order: Order inmutable construida por el executor.

        Returns:
            dict con claves: status, filled_quantity, avg_price,
                             latency_ms, error.
        """
        ...
