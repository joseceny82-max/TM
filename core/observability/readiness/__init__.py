"""core/observability/readiness/ — M13.9 Production Readiness Index.

Pure function layer que agrega risk_report (M13.8) + history_summary
(derivable de M13.6) en un readiness index 0-100 con status operacional
y un boolean go_live_eligibility.

Principio: capa de decisión analítica. NO ejecuta. NO controla.
"""
from core.observability.readiness.purchase_gate_readiness import (
    READINESS_STATUS_THRESHOLDS,
    compute_readiness_index,
)

# Spec alias — nombre canónico del task spec M13.9
compute_purchase_readiness_index = compute_readiness_index

__all__ = [
    "compute_readiness_index",
    "compute_purchase_readiness_index",
    "READINESS_STATUS_THRESHOLDS",
]
