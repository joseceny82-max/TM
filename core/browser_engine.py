"""core/browser_engine.py — Base del engine de browser (placeholder seguro).

FASE 2: placeholder. La lógica real se migrará en FASE 4 sin tocar legacy.

API futura (FASE 4):
    class BrowserEngine:
        def launch(self, engine: str) -> None: ...
        def attach_cdp(self, port: int) -> CDPSession: ...
        def new_context(self, persona: dict | None = None) -> BrowserContext: ...
        def cleanup(self, ctx) -> dict: ...
"""


class BrowserEngine:
    """Placeholder seguro — sin implementación todavía.

    En FASE 4 envolverá:
        - browser_cleanup.clear_history_via_cdp
        - bot_orchestrator._start_profile (vía adapter)
        - playwright sync_api connect_over_cdp
    """
    pass
