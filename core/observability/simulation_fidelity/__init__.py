"""core/observability/simulation_fidelity/ — M20 Simulation Fidelity Layer.

Capa observacional pasiva que evalúa la fidelidad de M19 (Market→TM
adapter) respecto al comportamiento esperado definido por DOMAIN
BOUNDARY SPEC v1.0.

PRINCIPIO CORE:
    M20 es OBSERVATIONAL VALIDATION ONLY:
        - NUNCA decide
        - NUNCA ejecuta
        - NUNCA hace override
        - NUNCA influye en M11-M19
        - NUNCA importa M11-M19 internals (epistemological independence)

Mide:
    - alignment de cada signal Market vs TM proxy
    - fidelity score agregado [0, 1]
    - adapter quality (high/medium/low/critical)
    - drift entre vectores de signals
    - hashes determinísticos de inputs para trazabilidad

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.simulation_fidelity.fidelity_engine import (
    ADAPTER_QUALITY_THRESHOLDS,
    RECOMMENDATION_BY_QUALITY,
    evaluate_simulation_fidelity,
)

# Alias semántico
compute_fidelity_report = evaluate_simulation_fidelity

__all__ = [
    "evaluate_simulation_fidelity",
    "compute_fidelity_report",
    "ADAPTER_QUALITY_THRESHOLDS",
    "RECOMMENDATION_BY_QUALITY",
]
