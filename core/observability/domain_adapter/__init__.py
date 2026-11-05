"""core/observability/domain_adapter/ — M19 Compatibility Shim.

Shim de re-export al M19 CANÓNICO (per core/observability/M19_CANONICAL.md).
NO contiene lógica.

Namespace canónico:  core.observability.adapter
Este namespace:      compatibilidad de imports legacy / specs externos.

Ambos imports apuntan al MISMO function object (verificable con `is`).
"""
from core.observability.adapter.domain_adapter import (
    adapt_event_to_tm_snapshot,
    adapt_market_event_to_simulation,
)

__all__ = [
    "adapt_event_to_tm_snapshot",
    "adapt_market_event_to_simulation",
]
