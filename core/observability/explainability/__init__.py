"""core/observability/explainability/ — M18 Explainability Layer.

Capa Layer 7 puramente derivada encima de M17 audit & traceability.

Pure function layer que traduce el pipeline completo (M13.7 → M17) en
narrativa humana determinística con resumen ejecutivo, postura del
sistema, guía operacional y razones reproducibles.

PRINCIPIO CORE:
    M18 NUNCA ejecuta, NUNCA muta, NUNCA hace override.
    NUNCA usa NLP, modelos IA ni generación libre.
    SOLO aplica plantillas estáticas sobre signals existentes.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.explainability.purchase_decision_explainability import (
    EXPLANATION_STATES,
    OPERATOR_GUIDANCE_MATRIX,
    SYSTEM_POSTURE_MATRIX,
    build_decision_explanation,
)

# Alias canónico del spec M18
compute_explainability_report = build_decision_explanation

__all__ = [
    "build_decision_explanation",
    "compute_explainability_report",
    "EXPLANATION_STATES",
    "SYSTEM_POSTURE_MATRIX",
    "OPERATOR_GUIDANCE_MATRIX",
]
