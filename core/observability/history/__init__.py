"""core/observability/history/ — Lightweight time-series aggregation layer.

M13.6: agregación read-only de eventos del PurchaseGate desde JSONL streams
existentes (runtime_diagnostics). Sin nueva DB, sin storage engine nuevo.
"""
from core.observability.history.purchase_gate_aggregator import (
    PURCHASE_EVENT_NAMES,
    WINDOW_SECONDS,
    aggregate_purchase_history,
)

# M13.6 spec alias — nombre canónico del task spec
aggregate_purchase_gate_history = aggregate_purchase_history

__all__ = [
    "aggregate_purchase_history",
    "aggregate_purchase_gate_history",
    "PURCHASE_EVENT_NAMES",
    "WINDOW_SECONDS",
]
