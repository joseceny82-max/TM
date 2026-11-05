"""core/contracts/ — Event contract layer.

Reglas verificables de clasificación de eventos en 4 capas:
    STATE         → cambia FSM
    ACTION        → intent/side-effect, NO cambia FSM
    OBSERVABILITY → telemetría/lifecycle, NO cambia FSM
    NOISE         → ruido a filtrar (shot polimórfico, pre_*, _skipped)

NO toca lógica del bot. NO toca el FSM. Solo añade tipado semántico.
"""

__all__ = ["event_classifier"]
