"""tests/failure_modes/test_scenarios.py — 6 mandatory failure-mode tests.

Escenarios obligatorios (per STEP 6 spec):

    1. Carrito expira mid-flow         → TestCartExpiredMidFlow
    2. Inventario cambia entre pasos    → TestInventoryChangesBetweenSteps
    3. Cola reubica usuario             → TestQueueReassignDuringCheckout
    4. Crash entre pago y persistencia → TestCrashBetweenPayAndPersist
    5. Concurrencia multi-slot          → TestConcurrentSlots
    6. Replay de logs                   → TestLogReplay

Adicional:
    7. Happy path baseline              → TestHappyPathBaseline

Todos los tests:
    - usan el harness aislado (tmp_path)
    - validan que CartState + idempotency_log gobiernan correctamente
    - NO tocan producción
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[2]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.idempotency import (
    is_committed,
    is_orphan,
    load_record,
    scan_orphan_intents,
    write_outcome,
)
from core.purchase_state import CartPhase
from tests.failure_modes.harness import (
    MockBackend,
    ReferenceFlowDriver,
    recover_orphan,
)


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_root(tmp_path):
    return str(tmp_path / "idem_log")


def _driver(backend, root, *, event="E1", account="A1",
             seats=("S1", "S2")) -> ReferenceFlowDriver:
    return ReferenceFlowDriver(
        backend=backend, idem_root=root,
        event_id=event, account_id=account, seats=list(seats),
    )


# ════════════════════════════════════════════════════════════════════
#  0. Happy path baseline (sanity)
# ════════════════════════════════════════════════════════════════════
class TestHappyPathBaseline:

    def test_happy_path_succeeds(self, isolated_root):
        b = MockBackend()
        d = _driver(b, isolated_root)
        r = d.run()
        assert r.status == "success"
        assert r.cart_phase == "paid"
        assert r.idempotency_state == "committed"
        assert r.outcome["order_id"].startswith("ORD-")

    def test_happy_path_calls_attach_and_payment(self, isolated_root):
        b = MockBackend()
        d = _driver(b, isolated_root)
        d.run()
        steps = [c[0] for c in b.call_log]
        assert steps == ["attach_cart", "submit_payment"]

    def test_happy_path_writes_both_intent_and_outcome(self, isolated_root):
        b = MockBackend()
        d = _driver(b, isolated_root)
        d.run()
        assert is_committed(d.idem_key, root=isolated_root)


# ════════════════════════════════════════════════════════════════════
#  1. Carrito expira MID-FLOW (tras attach, antes/durante pago)
# ════════════════════════════════════════════════════════════════════
class TestCartExpiredMidFlow:

    def test_cart_expires_via_time_advance(self, isolated_root):
        """Simula procesamiento lento: cart ttl=10s, pago tarda 30s →
        el backend detecta expiración cuando llega submit_payment."""
        b = MockBackend()
        b.inject("attach_cart", ttl=10)
        # Pago "tarda" 30s en procesarse → backend avanza reloj antes de chequear
        b.inject("submit_payment", delay_seconds=30)
        d = _driver(b, isolated_root)
        r = d.run()
        assert r.status == "payment_failed"
        assert r.cart_phase == "expired"
        assert "expired" in r.error.lower()
        assert r.error_category == "cart_lifecycle"
        assert r.idempotency_state == "committed"  # outcome sellado

    def test_cart_expired_response_from_backend(self, isolated_root):
        b = MockBackend()
        # Forzar respuesta cart_expired en submit_payment (incluso si TTL no expiró)
        b.inject("submit_payment", cart_expired=True)
        d = _driver(b, isolated_root)
        r = d.run()
        assert r.status == "payment_failed"
        assert r.cart_phase == "expired"
        assert r.error_category == "cart_lifecycle"

    def test_expired_cart_outcome_sealed_no_retry_pays(self, isolated_root):
        b = MockBackend()
        b.inject("submit_payment", cart_expired=True)
        d = _driver(b, isolated_root)
        d.run()
        # Reintentar con MISMA idempotency key → debe ser skip
        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        assert r2.status == "skipped_already_committed"
        # Backend NO debe ver un segundo submit_payment
        payment_calls = [c for c in b.call_log if c[0] == "submit_payment"]
        assert len(payment_calls) == 1


# ════════════════════════════════════════════════════════════════════
#  2. Inventario cambia entre selección y carrito
# ════════════════════════════════════════════════════════════════════
class TestInventoryChangesBetweenSteps:

    def test_inventory_changed_returns_attach_failed(self, isolated_root):
        b = MockBackend()
        b.inject("attach_cart", inventory_changed=True,
                  now_available=["S5", "S6"])
        d = _driver(b, isolated_root, seats=["S1", "S2"])
        r = d.run()
        assert r.status == "attach_failed"
        assert r.cart_phase == "failed"
        assert r.error_category == "inventory"
        assert "inventory" in r.error.lower()

    def test_no_payment_attempted_when_inventory_changed(self, isolated_root):
        b = MockBackend()
        b.inject("attach_cart", inventory_changed=True)
        d = _driver(b, isolated_root)
        d.run()
        payment_calls = [c for c in b.call_log if c[0] == "submit_payment"]
        assert len(payment_calls) == 0

    def test_inventory_failure_outcome_committed(self, isolated_root):
        b = MockBackend()
        b.inject("attach_cart", inventory_changed=True)
        d = _driver(b, isolated_root)
        d.run()
        # outcome sellado → reintento es no-op
        assert is_committed(d.idem_key, root=isolated_root)


# ════════════════════════════════════════════════════════════════════
#  3. Cola reubica al usuario durante checkout
# ════════════════════════════════════════════════════════════════════
class TestQueueReassignDuringCheckout:

    def test_queue_reassign_aborts_payment(self, isolated_root):
        b = MockBackend()
        b.inject("submit_payment", queue_reassign=True, new_position=5000)
        d = _driver(b, isolated_root)
        r = d.run()
        assert r.status == "payment_failed"
        assert r.cart_phase == "failed"
        assert r.error_category == "queue"
        assert "queue" in r.error.lower()

    def test_queue_reassign_does_not_create_order(self, isolated_root):
        b = MockBackend()
        b.inject("submit_payment", queue_reassign=True)
        d = _driver(b, isolated_root)
        d.run()
        # Backend NO debe registrar order
        assert b.committed_orders == {}

    def test_queue_reassign_outcome_sealed(self, isolated_root):
        b = MockBackend()
        b.inject("submit_payment", queue_reassign=True)
        d = _driver(b, isolated_root)
        d.run()
        assert is_committed(d.idem_key, root=isolated_root)


# ════════════════════════════════════════════════════════════════════
#  4. CRÍTICO: crash entre pago y persistencia
# ════════════════════════════════════════════════════════════════════
class TestCrashBetweenPayAndPersist:

    def test_crash_during_payment_leaves_orphan_intent(self, isolated_root):
        """Si el side-effect crashea sin response, el intent queda orphan."""
        b = MockBackend()
        b.inject("submit_payment", crash_before_response=True,
                  exception_class=ConnectionError,
                  exception_msg="network broke")
        d = _driver(b, isolated_root)
        r = d.run()
        assert r.status == "crash_during_payment"
        assert r.idempotency_state == "orphan"
        assert "network broke" in r.error
        assert r.error_category == "network"
        # Verificar archivo: intent existe, outcome no
        assert is_orphan(d.idem_key, root=isolated_root)

    def test_orphan_detected_on_scan(self, isolated_root):
        b = MockBackend()
        b.inject("submit_payment", crash_before_response=True)
        d = _driver(b, isolated_root)
        d.run()
        orphans = scan_orphan_intents(root=isolated_root)
        assert len(orphans) == 1
        assert orphans[0]["key"] == d.idem_key

    def test_retry_with_same_key_is_safely_aborted(self, isolated_root):
        """CRÍTICO: tras crash, reintento con misma key NO repite pago."""
        b = MockBackend()
        b.inject("submit_payment", crash_before_response=True)
        d = _driver(b, isolated_root)
        d.run()  # crash → orphan
        # Reintento (nueva instancia, misma idem key)
        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        # El driver detecta orphan → aborta SIN tocar payment
        assert r2.status == "aborted_orphan_intent"
        # Backend recibió SOLO 1 intento de pago (el que crasheó)
        payment_calls = [c for c in b.call_log if c[0] == "submit_payment"]
        assert len(payment_calls) == 1

    def test_recovery_seals_orphan(self, isolated_root):
        """Recovery flow puede sellar el orphan como 'unknown' y desbloquear."""
        b = MockBackend()
        b.inject("submit_payment", crash_before_response=True)
        d = _driver(b, isolated_root)
        d.run()
        assert is_orphan(d.idem_key, root=isolated_root)
        # Recovery
        rec_r = recover_orphan(d.idem_key, idem_root=isolated_root, backend=b)
        assert rec_r["recovered"] is True
        assert rec_r["status"] == "unknown"
        # Ya no es orphan — es committed (con outcome 'unknown')
        assert not is_orphan(d.idem_key, root=isolated_root)
        rec = load_record(d.idem_key, root=isolated_root)
        assert rec.is_committed()
        assert rec.outcome_payload["payload"]["status"] == "unknown_after_orphan_recovery"


# ════════════════════════════════════════════════════════════════════
#  5. Concurrencia multi-slot (mismo evento, mismas seats)
# ════════════════════════════════════════════════════════════════════
class TestConcurrentSlots:

    def test_same_key_two_drivers_dedupe(self, isolated_root):
        """Dos drivers SECUENCIALES con misma key: segundo es skip."""
        b = MockBackend()
        d1 = _driver(b, isolated_root)
        r1 = d1.run()
        assert r1.status == "success"

        # Segundo intento — misma key, mismo backend
        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        assert r2.status == "skipped_already_committed"
        # Backend: 1 attach + 1 payment (no duplicación)
        assert len([c for c in b.call_log if c[0] == "submit_payment"]) == 1

    def test_concurrent_threads_only_one_pays(self, isolated_root):
        """Dos threads SIMULTÁNEOS con misma idem key.

        Ambos generan el MISMO cart_id determinístico (mismo event_id +
        mismo timestamp del backend, que no se avanza). El backend
        deduplica por cart_id en `committed_orders`. Resultado: máximo
        1 order distinto.
        """
        b = MockBackend()
        d1 = _driver(b, isolated_root)
        d2 = _driver(b, isolated_root)
        # Misma key garantizada (mismo event, account, seats, day-bucket)
        assert d1.idem_key == d2.idem_key

        results: dict = {}
        def _run(name, drv):
            results[name] = drv.run()

        t1 = threading.Thread(target=_run, args=("t1", d1))
        t2 = threading.Thread(target=_run, args=("t2", d2))
        t1.start(); t2.start()
        t1.join(); t2.join()

        statuses = sorted([results["t1"].status, results["t2"].status])
        # Al menos uno debe haber tenido éxito o haberse abortado limpiamente
        assert "success" in statuses or "skipped_already_committed" in statuses
        # CRÍTICO: en ningún escenario debe haber 2 orders distintos
        order_ids = set(b.committed_orders.values())
        assert len(order_ids) <= 1, (
            f"Multiple orders generated: {order_ids}. statuses={statuses}"
        )

    def test_different_keys_independent(self, isolated_root):
        """Eventos/cuentas distintas → keys distintas → flows independientes."""
        b = MockBackend()
        d1 = _driver(b, isolated_root, event="E1", account="A1")
        d2 = _driver(b, isolated_root, event="E2", account="A2")
        assert d1.idem_key != d2.idem_key
        r1 = d1.run()
        r2 = d2.run()
        assert r1.status == "success"
        assert r2.status == "success"
        assert r1.cart_id != r2.cart_id


# ════════════════════════════════════════════════════════════════════
#  6. Replay de logs (re-ejecutar con misma key)
# ════════════════════════════════════════════════════════════════════
class TestLogReplay:

    def test_replay_after_success_is_noop(self, isolated_root):
        """Tras compra exitosa, replay no toca el backend."""
        b = MockBackend()
        d = _driver(b, isolated_root)
        r1 = d.run()
        assert r1.status == "success"

        # Reset call log; replay
        b.call_log.clear()
        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        assert r2.status == "skipped_already_committed"
        # ZERO calls to backend
        assert b.call_log == []

    def test_replay_returns_original_outcome(self, isolated_root):
        b = MockBackend()
        d = _driver(b, isolated_root)
        r1 = d.run()
        original_order = r1.outcome["order_id"]

        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        assert r2.outcome["order_id"] == original_order

    def test_replay_after_failed_payment_is_noop(self, isolated_root):
        """Replay tras pago declinado tampoco repite intento."""
        b = MockBackend()
        b.inject("submit_payment", declined=True, decline_code="DO_NOT_HONOR")
        d = _driver(b, isolated_root)
        r1 = d.run()
        assert r1.status == "payment_failed"

        b.call_log.clear()
        d2 = _driver(b, isolated_root)
        r2 = d2.run()
        assert r2.status == "skipped_already_committed"
        assert b.call_log == []

    def test_replay_after_orphan_is_blocked(self, isolated_root):
        """Tras crash (orphan), replay aborta — NO reintenta el pago."""
        b = MockBackend()
        b.inject("submit_payment", crash_before_response=True)
        d = _driver(b, isolated_root)
        d.run()
        b.call_log.clear()
        d2 = _driver(b, isolated_root)
        r = d2.run()
        assert r.status == "aborted_orphan_intent"
        assert b.call_log == []  # no backend interaction


# ════════════════════════════════════════════════════════════════════
#  EXTRA: verificación de invariantes globales
# ════════════════════════════════════════════════════════════════════
class TestGlobalInvariants:

    def test_no_duplicate_orders_across_scenarios(self, isolated_root):
        """No importa qué falle, NUNCA hay 2 orders para misma idem key."""
        b = MockBackend()
        d = _driver(b, isolated_root)
        d.run()  # success
        d2 = _driver(b, isolated_root)
        d2.run()  # skip
        d3 = _driver(b, isolated_root)
        d3.run()  # skip
        assert len(b.committed_orders) == 1

    def test_cart_state_always_terminal_after_run(self, isolated_root):
        """Tras un run completo, el carrito debe estar en estado terminal
        excepto si crash interrumpió (CHECKING_OUT en orphan)."""
        scenarios = [
            (lambda b: None, "paid"),
            (lambda b: b.inject("attach_cart", inventory_changed=True), "failed"),
            (lambda b: b.inject("submit_payment", cart_expired=True), "expired"),
            (lambda b: b.inject("submit_payment", declined=True), "failed"),
        ]
        from datetime import datetime
        for setup, expected_phase in scenarios:
            # Cada escenario con su propio backend + root para evitar colisión de idem
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                b = MockBackend()
                setup(b)
                d = _driver(b, td)
                r = d.run()
                assert r.cart_phase == expected_phase, (
                    f"Expected {expected_phase}, got {r.cart_phase} "
                    f"for scenario, status={r.status}"
                )
