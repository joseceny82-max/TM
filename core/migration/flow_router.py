"""core/migration/flow_router.py — Safe Routing Layer (STEP 9).

Router que decide entre legacy y core flow basado en flags.

PRINCIPIO: SAFE DEFAULT.
    Si flags.USE_CORE_FLOW == False → SIEMPRE legacy.
    El core path es OPT-IN explícito.

stdlib only.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from core.migration.flags import MigrationFlags, get_flags


def route_flow(ctx: Any,
                *,
                legacy_callable: Callable[[Any], Any],
                core_callable: Optional[Callable[[Any], Any]] = None,
                flags: Optional[MigrationFlags] = None) -> Any:
    """Rutea ejecución al flow apropiado.

    Args:
        ctx: contexto a pasar al callable
        legacy_callable: SIEMPRE disponible
        core_callable: opcional; si None y USE_CORE_FLOW=True → legacy
        flags: opcional override (default: get_flags())

    Returns:
        El resultado del callable elegido.

    Garantía: si USE_CORE_FLOW=False (default), nunca se llama core_callable.
    """
    f = flags or get_flags()
    if f.USE_CORE_FLOW and callable(core_callable):
        try:
            return core_callable(ctx)
        except Exception:
            # Failover absoluto: si core crashea → legacy
            return legacy_callable(ctx)
    return legacy_callable(ctx)
