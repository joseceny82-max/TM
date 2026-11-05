"""bot/purchase_flow.py — Flujo principal de compra (placeholder).

FASE 2: placeholder con `NotImplementedError`. Indica claramente que la
lógica todavía vive en `_compra_parallel.py`.

FASE 5: aquí se extraerá la secuencia completa:
    home → auth → event → cart → checkout

Mientras tanto el bot real se ejecuta desde:
    python tools/oneoff/_compra_parallel.py --slot 1 --event-url ... --engine morelogin ...

Una vez extraídos los flows en FASE 5/6, este módulo orquestará:
    from bot.auth_flow import AuthFlow
    from bot.cart_flow import CartFlow
    from bot.checkout_flow import CheckoutFlow

    class PurchaseFlow:
        def run(self, event_url, qty, ...) -> int: ...
"""


def run_purchase_flow() -> int:
    """Placeholder. Llamar a este path lanza NotImplementedError
    intencionalmente para evitar dual-paths accidentales."""
    raise NotImplementedError(
        "Migration pending — bot lives in tools/oneoff/_compra_parallel.py until FASE 5. "
        "Use `python tools/oneoff/_compra_parallel.py --slot 1 --event-url ...` por ahora."
    )
