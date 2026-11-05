"""core/observability/alerts/ — Pure evaluation layer for alerting rules.

M13.5: Reglas evaluadas sobre snapshots del PurchaseGate (M13.3 endpoint).
Sin side effects, sin mutaciones, sin nuevas dependencias.
"""
from core.observability.alerts.purchase_gate_rules import (
    SEVERITY_LEVELS,
    SEVERITY_ORDER,
    evaluate_rules,
)

# M13.5 spec alias — nombre canónico del task spec
evaluate_purchase_gate_alert = evaluate_rules

__all__ = [
    "evaluate_rules",
    "evaluate_purchase_gate_alert",
    "SEVERITY_LEVELS",
    "SEVERITY_ORDER",
]
