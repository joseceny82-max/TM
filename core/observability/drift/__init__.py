"""core/observability/drift/ — M13.7 Drift Detection Engine.

Pure function layer que detecta desviación operacional del PurchaseGate
comparando snapshots actuales contra baseline histórico.

Sin IO, sin mutations, sin threading. Determinístico.
"""
from core.observability.drift.purchase_gate_drift import (
    BLOCK_RATE_SPIKE_FACTOR,
    BLOCK_RATE_SPIKE_MIN_ABSOLUTE,
    DRY_RUN_DEVIATION_THRESHOLD,
    MIN_HISTORY_FOR_DEVIATION,
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    compute_baseline,
    detect_gate_drift,
)

# Spec alias — function name canónico del task spec M13.7
detect_purchase_gate_drift = detect_gate_drift

__all__ = [
    "detect_gate_drift",
    "detect_purchase_gate_drift",
    "compute_baseline",
    "DRY_RUN_DEVIATION_THRESHOLD",
    "BLOCK_RATE_SPIKE_FACTOR",
    "BLOCK_RATE_SPIKE_MIN_ABSOLUTE",
    "MIN_HISTORY_FOR_DEVIATION",
    "SEVERITY_LEVELS",
    "SEVERITY_ORDER",
]
