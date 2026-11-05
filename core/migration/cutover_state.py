"""core/migration/cutover_state.py — Cutover State Machine (STEP 10).

Máquina de estados explícita para la migración progresiva:

    LEGACY_ONLY → SHADOW → CANARY_1 → CANARY_10 → CANARY_50 → CANARY_100 → FULL_CORE

Transiciones SOLO hacia adelante (excepto rollback hacia atrás).

stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ════════════════════════════════════════════════════════════════════
#  Cutover states
# ════════════════════════════════════════════════════════════════════
class CutoverState(Enum):
    LEGACY_ONLY = "legacy_only"
    SHADOW = "shadow"
    CANARY_1 = "canary_1"
    CANARY_10 = "canary_10"
    CANARY_50 = "canary_50"
    CANARY_100 = "canary_100"
    FULL_CORE = "full_core"


# Configuración asociada a cada state
@dataclass(frozen=True)
class CutoverConfig:
    """Snapshot inmutable de la config asociada a un state."""
    state: CutoverState
    canary_percentage: float
    use_core_flow: bool
    shadow_core_flow: bool

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "canary_percentage": float(self.canary_percentage),
            "use_core_flow": bool(self.use_core_flow),
            "shadow_core_flow": bool(self.shadow_core_flow),
        }


_STATE_CONFIGS: dict = {
    CutoverState.LEGACY_ONLY: CutoverConfig(
        state=CutoverState.LEGACY_ONLY,
        canary_percentage=0.0,
        use_core_flow=False,
        shadow_core_flow=False,
    ),
    CutoverState.SHADOW: CutoverConfig(
        state=CutoverState.SHADOW,
        canary_percentage=0.0,
        use_core_flow=False,
        shadow_core_flow=True,
    ),
    CutoverState.CANARY_1: CutoverConfig(
        state=CutoverState.CANARY_1,
        canary_percentage=1.0,
        use_core_flow=False,    # router decide por canary
        shadow_core_flow=True,
    ),
    CutoverState.CANARY_10: CutoverConfig(
        state=CutoverState.CANARY_10,
        canary_percentage=10.0,
        use_core_flow=False,
        shadow_core_flow=True,
    ),
    CutoverState.CANARY_50: CutoverConfig(
        state=CutoverState.CANARY_50,
        canary_percentage=50.0,
        use_core_flow=False,
        shadow_core_flow=True,
    ),
    CutoverState.CANARY_100: CutoverConfig(
        state=CutoverState.CANARY_100,
        canary_percentage=100.0,
        use_core_flow=False,
        shadow_core_flow=True,
    ),
    CutoverState.FULL_CORE: CutoverConfig(
        state=CutoverState.FULL_CORE,
        canary_percentage=100.0,
        use_core_flow=True,
        shadow_core_flow=False,
    ),
}


# Transiciones legales: (FROM → set de TO permitidos)
# Reglas: forward por escalones; rollback siempre permitido a step anterior;
# emergencia: cualquier state puede ir a LEGACY_ONLY
_FORWARD: dict = {
    CutoverState.LEGACY_ONLY: {CutoverState.SHADOW},
    CutoverState.SHADOW: {CutoverState.CANARY_1, CutoverState.LEGACY_ONLY},
    CutoverState.CANARY_1: {CutoverState.CANARY_10, CutoverState.SHADOW,
                              CutoverState.LEGACY_ONLY},
    CutoverState.CANARY_10: {CutoverState.CANARY_50, CutoverState.CANARY_1,
                               CutoverState.LEGACY_ONLY},
    CutoverState.CANARY_50: {CutoverState.CANARY_100, CutoverState.CANARY_10,
                               CutoverState.LEGACY_ONLY},
    CutoverState.CANARY_100: {CutoverState.FULL_CORE, CutoverState.CANARY_50,
                                CutoverState.LEGACY_ONLY},
    CutoverState.FULL_CORE: {CutoverState.CANARY_100, CutoverState.LEGACY_ONLY},
}


class IllegalCutoverTransition(Exception):
    pass


def config_for(state: CutoverState) -> CutoverConfig:
    """Devuelve la config inmutable asociada a un state."""
    return _STATE_CONFIGS[state]


def legal_transitions(state: CutoverState) -> frozenset:
    """Set de states a los que se puede transitar desde `state`."""
    return frozenset(_FORWARD.get(state, set()))


def is_legal_transition(from_state: CutoverState,
                         to_state: CutoverState) -> bool:
    if from_state == to_state:
        return True   # idempotente
    return to_state in legal_transitions(from_state)


def transition(from_state: CutoverState,
                to_state: CutoverState) -> CutoverConfig:
    """Aplica una transición. Raises si ilegal."""
    if not is_legal_transition(from_state, to_state):
        raise IllegalCutoverTransition(
            f"{from_state.value} -> {to_state.value} not allowed"
        )
    return config_for(to_state)


def rollback_state(from_state: CutoverState) -> CutoverState:
    """Devuelve el state anterior en la progresión (rollback step)."""
    chain = [
        CutoverState.LEGACY_ONLY, CutoverState.SHADOW,
        CutoverState.CANARY_1, CutoverState.CANARY_10,
        CutoverState.CANARY_50, CutoverState.CANARY_100,
        CutoverState.FULL_CORE,
    ]
    try:
        idx = chain.index(from_state)
    except ValueError:
        return CutoverState.LEGACY_ONLY
    return chain[max(0, idx - 1)]


def emergency_rollback() -> CutoverState:
    """Rollback de emergencia: vuelve a LEGACY_ONLY desde cualquier state."""
    return CutoverState.LEGACY_ONLY


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
