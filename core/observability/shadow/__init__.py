"""core/observability/shadow/ — M20 Shadow Observation Layer.

Capa observacional puramente PASIVA que clasifica el régimen dinámico
de la secuencia (history, current) producida por las capas previas
(M13.7 → M19) y reporta:

    - shadow_state         : stable | volatile | critical
    - trend_direction      : improving | degrading | oscillating
    - stability_index      : float ∈ [0, 1]
    - volatility_index     : float ∈ [0, 1]
    - divergence_events    : list[dict]
    - tick_summary         : {total, critical_ticks, verified_ticks}
    - meta                 : {simulation_mode, timestamp}

PRINCIPIO CORE:
    M20 Shadow es OBSERVATIONAL-ONLY:
        - NUNCA decide
        - NUNCA ejecuta
        - NUNCA hace override
        - NUNCA influye en M11-M19 ni en M20-Fidelity
        - NUNCA importa otras capas (independencia epistemológica)

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.shadow.shadow_observer import (
    build_shadow_observation,
)

# Alias semántico — algunos especificadores pueden invocar ambos nombres
compute_shadow_report = build_shadow_observation

__all__ = [
    "build_shadow_observation",
    "compute_shadow_report",
]
