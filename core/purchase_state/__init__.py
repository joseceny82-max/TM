"""core/purchase_state/ — Explicit cart state model (STEP 1 del refactor).

Modelo formal e inmutable del ciclo de vida del carrito de compra,
diseñado para reemplazar el "estado implícito en el orden de
ejecución" del flow lineal actual.

Esta capa es PURELY ADDITIVE — el flow productivo no la consume
todavía. La integración con `_compra_parallel.py` viene en pasos
posteriores del roadmap (junto con idempotency_log).

Public API:
    CartState                  - dataclass frozen del estado del carrito
    CartPhase                  - enum del ciclo de vida
    SeatLock                   - butaca reservada (frozen)
    IllegalCartTransition      - excepción de dominio
    new_cart_state(...)        - factory para INIT phase
    LEGAL_TRANSITIONS          - mapa declarativo FROM → set(TO)
    TERMINAL_PHASES            - frozenset de fases terminales

stdlib only. Pure data + pure validations.
"""
from core.purchase_state.cart_state import (
    LEGAL_TRANSITIONS,
    TERMINAL_PHASES,
    CartPhase,
    CartState,
    IllegalCartTransition,
    SeatLock,
    new_cart_state,
)

__all__ = [
    "CartState",
    "CartPhase",
    "SeatLock",
    "IllegalCartTransition",
    "new_cart_state",
    "LEGAL_TRANSITIONS",
    "TERMINAL_PHASES",
]
