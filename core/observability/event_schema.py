"""core/observability/event_schema.py — Aggregated metric schemas (STEP 4).

Modelos inmutables para los resultados de agregación. Cero lógica, sólo
estructuras de datos serializables.

stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ════════════════════════════════════════════════════════════════════
#  Constantes de claves canónicas
# ════════════════════════════════════════════════════════════════════
METRIC_FAILURE_RATE_BY_CATEGORY: str = "failure_rate_by_category"
METRIC_STEP_FAILURE_DISTRIBUTION: str = "step_failure_distribution"
METRIC_CART_LIFECYCLE_HEATMAP: str = "cart_lifecycle_heatmap"
METRIC_CORRELATION_BY_CART_ID: str = "correlation_by_cart_id"
METRIC_CORRELATION_BY_IDEM_KEY: str = "correlation_by_idem_key"
METRIC_SEVERITY_BREAKDOWN: str = "severity_breakdown"

UNKNOWN_BUCKET: str = "unknown"


# ════════════════════════════════════════════════════════════════════
#  AggregatedMetric — resultado genérico de una agregación
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class AggregatedMetric:
    """Resultado canónico de una agregación.

    Frozen — serializable directamente a JSON.
    """
    name: str                          # uno de METRIC_* constantes
    total_events: int                  # cuántos eventos se procesaron
    data: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_events": int(self.total_events),
            "data": dict(self.data),
            "meta": dict(self.meta),
        }


# ════════════════════════════════════════════════════════════════════
#  EventGroup — agrupación por clave (cart_id / idem_key / etc.)
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class EventGroup:
    """Grupo de eventos comparten una clave (e.g. cart_id, idem_key)."""
    key: str
    events: tuple                      # tuple[dict, ...]
    categories: tuple = ()             # tuple[str, ...] presentes en el grupo
    codes: tuple = ()                  # tuple[str, ...] presentes

    def __len__(self) -> int:
        return len(self.events)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "count": len(self.events),
            "categories": list(self.categories),
            "codes": list(self.codes),
        }


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
