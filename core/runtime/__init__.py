"""core/runtime/ — Runtime session manager.

Mantiene una sesión observable por slot/run combinando:
    - event_classifier (capa contractual)
    - drift_detector   (FSM vs runtime)
    - health_score     (0-100 score)

Sin dependencia del bot. Streaming. Fail-safe.
"""

__all__ = ["run_session"]
