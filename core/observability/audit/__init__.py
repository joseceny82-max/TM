"""core/observability/audit/ — M17 Audit & Traceability Layer.

Capa Layer 6 puramente derivada encima de M16 policy/compliance.

Pure function layer que consolida outputs de M13.7 → M16 en una traza de
auditoría: lineage de decisión, integrity checks, trace_id determinístico
y reasoning chain reflejando el flujo completo de la pirámide.

PRINCIPIO CORE:
    M17 NUNCA ejecuta, NUNCA muta, NUNCA hace override.
    NUNCA introduce feedback loops sobre capas inferiores.
    SOLO consolida estado en una traza auditable.

stdlib only. Determinístico. Sin side-effects.
"""
from core.observability.audit.purchase_audit_traceability import (
    AUDIT_STATES,
    INTEGRITY_CHECK_NAMES,
    build_audit_trace,
)

# Alias canónico del spec M17
compute_decision_traceability_report = build_audit_trace

__all__ = [
    "build_audit_trace",
    "compute_decision_traceability_report",
    "AUDIT_STATES",
    "INTEGRITY_CHECK_NAMES",
]
