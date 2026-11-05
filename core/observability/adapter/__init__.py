"""core/observability/adapter/ — M19 Domain Adapter Layer.

Capa de traducción semántica entre dominios externos (Market /
Simulation) y el dominio TM canónico (TM_Snapshot consumible por M13.7).

PRINCIPIO ARQUITECTÓNICO:
    M19 es ADVISORY-ONLY y BOUNDARY-ONLY:
        - NUNCA ejecuta
        - NUNCA muta state
        - NUNCA influye en decisiones
        - NUNCA introduce feedback loops
        - SOLO traduce signals según mapping fijo determinístico

Domain isolation (per DOMAIN BOUNDARY SPEC v1.0):
    Market signals NUNCA fluyen directo al core (M13.7+).
    Sólo via este adapter, que produce TM_Snapshot canónico.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.adapter.domain_adapter import (
    DRIFT_HINT_THRESHOLDS,
    MAX_ERROR_PROXY,
    VOLUME_ERROR_THRESHOLDS,
    adapt_event_to_tm_snapshot,
    adapt_market_event,
    adapt_market_event_to_simulation,
)

__all__ = [
    "adapt_event_to_tm_snapshot",
    "adapt_market_event",
    "adapt_market_event_to_simulation",
    "DRIFT_HINT_THRESHOLDS",
    "VOLUME_ERROR_THRESHOLDS",
    "MAX_ERROR_PROXY",
]
