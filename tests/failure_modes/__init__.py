"""tests/failure_modes/ — failure-mode test harness + scenarios (STEP 6).

Suite que simula condiciones reales de producción que rompen los flows
de compra (carrito que expira, inventario que cambia, crash entre pago
y persistencia, concurrencia, replay).

Usa las primitivas de:
    - core.purchase_state (Step 1) — CartState
    - core.idempotency    (Step 2) — WAL intent/outcome

NO toca el flow productivo (`_compra_parallel.py` etc.). El test driver
es una REFERENCIA EJECUTABLE de cómo el flow refactorizado debería
componer las primitivas.
"""
