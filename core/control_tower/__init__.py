"""core/control_tower/ — FastAPI-based observability dashboard.

4 fases incrementales sobre el mismo backend:
    FASE 1 — MVP            (/, /stream, /state, /health)
    FASE 2 — Realtime       (/state/live, /drift/current, /loops/current, /health/current)
    FASE 3 — Control Tower  (/run/<id>/analytics, /run/<id>/root-cause, /events/filter)
    FASE 4 — Industrial     (/runs/all, /drift/global, /alerts, /history)

Cada fase añade sin romper la anterior. UI separadas:
    /          → MVP HTML (eventos live)
    /realtime  → 4 paneles
    /control   → analytics + root cause
    /tower     → control tower industrial

Reutiliza módulos del sistema Flask existente:
    core.dashboard.stream       → JSONLTailer
    core.dashboard.aggregator   → SessionAggregator
    core.observability.state_machine → FSM v2 (referencia)
"""

__version__ = "1.0.0"
