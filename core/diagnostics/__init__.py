"""core/diagnostics/ — Runtime diagnostics layer.

Componentes:
    drift_detector  → FSM vs runtime real-time
    health_score    → score 0-100 por run

NO toca lógica del bot. NO toca el FSM. Solo lee eventos clasificados.
"""

__all__ = ["drift_detector", "health_score"]
