"""tests/test_bridge.py — Test suite for STEP 3B Integration Bridge.

Cubre el módulo bridge que combina Steps 1+2+3 en helpers de alto nivel.

Categorías:
    1. start_flow                  (5 tests)
    2. Idempotency reads            (4 tests)
    3. Idempotency writes           (5 tests)
    4. transition_phase             (7 tests)
    5. record_exception             (5 tests)
    6. with_instrumentation (ctx mgr) (4 tests)
    7. Fail-safe / never propagates (5 tests)
    8. End-to-end con MockBackend   (3 tests)
    9. Public API                   (3 tests)

stdlib + pytest only. Aislado en tmp_path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.idempotency import (
    is_committed,
    is_orphan,
    load_record,
)
from core.observability.bridge import (
    FlowContext,
    check_already_committed,
    check_orphan_intent,
    record_exception,
    record_intent,
    record_outcome,
    snapshot,
    start_flow,
    transition_phase,
    with_instrumentation,
)
from core.observability.error_classification import (
    ErrorCategory,
    ErrorEvent,
)
from core.observability.event_emitter import (
    clear_sinks,
    read_events,
    set_sink_path,
)
from core.purchase_state import CartPhase, IllegalCartTransition


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated_sinks(tmp_path):
    """Cada test usa sus propios sinks + idem_root."""
    sink = tmp_path / "events.jsonl"
    set_sink_path(sink)
    clear_sinks()
    yield {
        "sink": sink,
        "idem_root": str(tmp_path / "idem"),
    }
    clear_sinks()
    set_sink_path(None)


def _ctx(_isolated_sinks, *, slot: int = 1, event_id: str = "E1",
         account: str = "A1", seats=("S1", "S2")) -> FlowContext:
    return start_flow(
        slot=slot, event_id=event_id, account_id=account,
        seats=list(seats), idem_root=_isolated_sinks["idem_root"],
    )


# ════════════════════════════════════════════════════════════════════
#  1. start_flow
# ════════════════════════════════════════════════════════════════════
class TestStartFlow:

    def test_returns_flow_context(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        assert isinstance(ctx, FlowContext)
        assert ctx.cart_state.phase == CartPhase.INIT

    def test_idempotency_key_deterministic(self, _isolated_sinks):
        c1 = _ctx(_isolated_sinks)
        c2 = _ctx(_isolated_sinks)
        # Misma intención + mismo día = misma key
        assert c1.idempotency_key == c2.idempotency_key

    def test_different_seats_different_key(self, _isolated_sinks):
        c1 = _ctx(_isolated_sinks, seats=("S1",))
        c2 = _ctx(_isolated_sinks, seats=("S2",))
        assert c1.idempotency_key != c2.idempotency_key

    def test_emits_flow_started_event(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        events = read_events()
        codes = [e["code"] for e in events]
        assert "flow_started" in codes

    def test_invalid_seats_None_safe(self, _isolated_sinks):
        ctx = start_flow(slot=1, event_id="E", account_id="A",
                          seats=None,
                          idem_root=_isolated_sinks["idem_root"])
        assert ctx.seats == ()


# ════════════════════════════════════════════════════════════════════
#  2. IDEMPOTENCY READS
# ════════════════════════════════════════════════════════════════════
class TestIdempotencyReads:

    def test_check_committed_returns_none_when_fresh(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        assert check_already_committed(ctx) is None

    def test_check_orphan_false_when_fresh(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        assert check_orphan_intent(ctx) is False

    def test_check_committed_returns_payload_after_outcome(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="x")
        record_outcome(ctx, status="paid", order_id="ORD-1")
        payload = check_already_committed(ctx)
        assert payload is not None
        assert payload["status"] == "paid"
        assert payload["order_id"] == "ORD-1"

    def test_check_orphan_true_after_intent_only(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="attach_cart")
        assert check_orphan_intent(ctx) is True


# ════════════════════════════════════════════════════════════════════
#  3. IDEMPOTENCY WRITES
# ════════════════════════════════════════════════════════════════════
class TestIdempotencyWrites:

    def test_record_intent_creates_file(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        ok = record_intent(ctx, step="attach")
        assert ok is True
        assert is_orphan(ctx.idempotency_key, root=ctx.idem_root)

    def test_record_outcome_marks_committed(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="attach")
        record_outcome(ctx, status="paid")
        assert is_committed(ctx.idempotency_key, root=ctx.idem_root)

    def test_record_intent_emits_event(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="attach")
        events = read_events()
        codes = [e["code"] for e in events]
        assert "intent_recorded" in codes

    def test_record_outcome_emits_event(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_outcome(ctx, status="success")
        events = read_events()
        codes = [e["code"] for e in events]
        assert "outcome_recorded" in codes

    def test_outcome_payload_contains_status_and_phase(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        ctx = transition_phase(ctx, CartPhase.ATTACHED)
        record_outcome(ctx, status="precheckout_failed", error="x")
        rec = load_record(ctx.idempotency_key, root=ctx.idem_root)
        p = rec.outcome_payload["payload"]
        assert p["status"] == "precheckout_failed"
        assert p["cart_phase"] == "attached"
        assert p["error"] == "x"


# ════════════════════════════════════════════════════════════════════
#  4. transition_phase
# ════════════════════════════════════════════════════════════════════
class TestTransitionPhase:

    def test_init_to_attached_updates_phase(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, CartPhase.ATTACHED)
        assert new_ctx.cart_state.phase == CartPhase.ATTACHED

    def test_returns_new_instance(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, CartPhase.ATTACHED)
        assert new_ctx is not ctx
        assert ctx.cart_state.phase == CartPhase.INIT  # original intact

    def test_emits_phase_transition_event(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        transition_phase(ctx, CartPhase.ATTACHED)
        events = read_events()
        codes = [e["code"] for e in events]
        assert "phase_transition" in codes

    def test_illegal_transition_returns_same_ctx(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        # INIT → PAID es ilegal
        new_ctx = transition_phase(ctx, CartPhase.PAID)
        assert new_ctx.cart_state.phase == CartPhase.INIT
        # Emite evento transition_invalid
        events = read_events()
        codes = [e["code"] for e in events]
        assert "transition_invalid" in codes

    def test_assigns_cart_id_when_first_provided(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, CartPhase.ATTACHED, cart_id="CART-X")
        assert new_ctx.cart_state.cart_id == "CART-X"

    def test_updates_seats(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, CartPhase.ATTACHED,
                                    seats=("S1", "S2", "S3"))
        assert len(new_ctx.cart_state.seats) == 3

    def test_updates_expiration(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, CartPhase.ATTACHED,
                                    expires_at_iso="2026-05-14T11:00:00+00:00")
        assert new_ctx.cart_state.expires_at_iso == "2026-05-14T11:00:00+00:00"


# ════════════════════════════════════════════════════════════════════
#  5. record_exception
# ════════════════════════════════════════════════════════════════════
class TestRecordException:

    def test_classifies_connection_error_as_network(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        try:
            raise ConnectionError("proxy refused")
        except ConnectionError as e:
            ev = record_exception(ctx, e, context="attach_cart")
        assert ev.category == ErrorCategory.NETWORK

    def test_emits_event_to_sink(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        try:
            raise TimeoutError("timeout")
        except TimeoutError as e:
            record_exception(ctx, e, context="submit_payment")
        events = read_events()
        timeouts = [e for e in events if e.get("code") == "timeout"]
        assert len(timeouts) == 1

    def test_event_carries_idempotency_key(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_exception(ctx, ConnectionError("x"), context="step")
        events = read_events()
        for e in events:
            if e.get("code") in {"proxy_dead", "connection_reset"}:
                assert e["idempotency_key"] == ctx.idempotency_key
                return
        pytest.fail("no network event found")

    def test_works_without_ctx(self, _isolated_sinks):
        # ctx=None debe funcionar (sites top-level)
        ev = record_exception(None, ConnectionError("x"), context="boot")
        assert ev is not None
        assert ev.category == ErrorCategory.NETWORK

    def test_never_propagates(self, _isolated_sinks):
        # No debe lanzar nada
        ctx = _ctx(_isolated_sinks)
        record_exception(ctx, Exception("any"), context="any")
        record_exception(None, RuntimeError("x"), context="any")


# ════════════════════════════════════════════════════════════════════
#  6. with_instrumentation (context manager)
# ════════════════════════════════════════════════════════════════════
class TestWithInstrumentation:

    def test_yields_ctx(self, _isolated_sinks):
        with with_instrumentation(
            slot=1, event_id="E", account_id="A", seats=["S"],
            idem_root=_isolated_sinks["idem_root"],
        ) as ctx:
            assert isinstance(ctx, FlowContext)
            assert ctx.slot == 1

    def test_captures_exception_and_reraises(self, _isolated_sinks):
        with pytest.raises(ConnectionError):
            with with_instrumentation(
                slot=1, event_id="E", account_id="A", seats=["S"],
                idem_root=_isolated_sinks["idem_root"],
            ):
                raise ConnectionError("proxy down")
        # El evento de excepción debe estar emitido
        events = read_events()
        codes = [e.get("code") for e in events]
        assert any(c in codes for c in {"proxy_dead", "connection_reset"})

    def test_exception_disabled_does_not_emit(self, _isolated_sinks):
        with pytest.raises(ValueError):
            with with_instrumentation(
                slot=1, event_id="E", account_id="A", seats=["S"],
                idem_root=_isolated_sinks["idem_root"],
                auto_record_exception=False,
            ):
                raise ValueError("not classified")
        events = read_events()
        # Sólo el flow_started, sin captura de excepción
        codes = [e.get("code") for e in events]
        assert "step_failed" not in codes  # no se clasificó la ValueError

    def test_normal_exit_does_not_emit_error(self, _isolated_sinks):
        with with_instrumentation(
            slot=1, event_id="E", account_id="A", seats=["S"],
            idem_root=_isolated_sinks["idem_root"],
        ) as ctx:
            pass
        events = read_events()
        # Solo flow_started (1 evento)
        assert len(events) >= 1
        # No hay eventos de exception (no proxy_dead/timeout/etc.)
        bad_codes = {"proxy_dead", "timeout", "card_declined",
                      "bot_block_detected"}
        for e in events:
            assert e.get("code") not in bad_codes


# ════════════════════════════════════════════════════════════════════
#  7. FAIL-SAFE
# ════════════════════════════════════════════════════════════════════
class TestFailSafe:

    def test_start_flow_with_garbage_inputs(self, _isolated_sinks):
        # Cualquier input garbage → ctx mínimo válido (no raise)
        ctx = start_flow(slot="not-int", event_id=None, account_id=123,
                          seats="weird", idem_root=_isolated_sinks["idem_root"])
        assert isinstance(ctx, FlowContext)

    def test_record_intent_with_nonserializable_payload_no_raise(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        class Unserializable:
            pass
        # No debe propagar
        record_intent(ctx, step="x", weird=Unserializable())
        # ok puede ser False pero no raise

    def test_transition_phase_with_non_cartphase_no_raise(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        new_ctx = transition_phase(ctx, "not a phase")
        # Devuelve ctx sin cambios
        assert new_ctx.cart_state.phase == ctx.cart_state.phase

    def test_record_exception_with_bad_exception_class(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        class BadStr(Exception):
            def __str__(self):
                raise RuntimeError("nope")
        # No debe propagar
        ev = record_exception(ctx, BadStr(), context="x")
        # Puede ser None o ErrorEvent — lo importante: no raise

    def test_check_already_committed_with_corrupted_log(self, tmp_path,
                                                          _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="x")
        # Corromper outcome.json manualmente (no existe aún, simulamos basura)
        kd = Path(ctx.idem_root) / ctx.idempotency_key
        kd.mkdir(parents=True, exist_ok=True)
        (kd / "outcome.json").write_text("{ corrupt", encoding="utf-8")
        # No debe propagar
        result = check_already_committed(ctx)
        # Corrupt outcome → tratado como no-committed
        assert result is None


# ════════════════════════════════════════════════════════════════════
#  8. END-TO-END con MockBackend
# ════════════════════════════════════════════════════════════════════
class TestEndToEnd:
    """Demuestra que el bridge basta para instrumentar un flow completo
    usando el harness del Step 6."""

    def test_full_flow_using_bridge(self, _isolated_sinks):
        from tests.failure_modes.harness import MockBackend
        b = MockBackend()

        ctx = _ctx(_isolated_sinks)
        # Pre-check
        assert check_already_committed(ctx) is None

        # 1. Attach
        record_intent(ctx, step="attach_cart")
        r = b.attach_cart(event_id=ctx.event_id, seats=list(ctx.seats),
                           account_id=ctx.account_id)
        assert r["ok"]
        ctx = transition_phase(ctx, CartPhase.ATTACHED,
                                cart_id=r["cart_id"],
                                seats=list(ctx.seats),
                                expires_at_iso=r["expires_at"])

        # 2. Checkout transition
        ctx = transition_phase(ctx, CartPhase.CHECKING_OUT)

        # 3. Payment
        pay = b.submit_payment(cart_id=ctx.cart_state.cart_id,
                                card_token="TOK")
        assert pay["ok"]
        ctx = transition_phase(ctx, CartPhase.PAID)
        record_outcome(ctx, status="paid", order_id=pay["order_id"])

        # Snapshot verifica el estado final
        snap = snapshot(ctx)
        assert snap["idempotency_state"] == "committed"
        assert snap["cart_state"]["phase"] == "paid"

    def test_full_flow_with_failure(self, _isolated_sinks):
        from tests.failure_modes.harness import MockBackend
        b = MockBackend()
        b.inject("submit_payment", declined=True)

        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="attach")
        r = b.attach_cart(event_id=ctx.event_id, seats=list(ctx.seats),
                           account_id=ctx.account_id)
        ctx = transition_phase(ctx, CartPhase.ATTACHED, cart_id=r["cart_id"])
        ctx = transition_phase(ctx, CartPhase.CHECKING_OUT)

        pay = b.submit_payment(cart_id=ctx.cart_state.cart_id,
                                card_token="TOK")
        assert not pay["ok"]
        # Bridge ayuda a clasificar el error sin tener que pensar
        ev = record_exception(
            ctx,
            Exception(pay["error"]),  # simular cómo el bot capturaría
            context="submit_payment",
        )
        ctx = transition_phase(ctx, CartPhase.FAILED)
        record_outcome(ctx, status="payment_failed", error=pay["error"])

        # Verifica que el outcome es accesible y la clasificación es correcta
        assert check_already_committed(ctx)["status"] == "payment_failed"
        assert ev.category == ErrorCategory.PAYMENT

    def test_replay_safe_after_commit(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        record_intent(ctx, step="x")
        record_outcome(ctx, status="paid", order_id="ORD-1")

        # Reintento con misma intención (nueva ctx, misma key)
        ctx2 = _ctx(_isolated_sinks)
        # ¡Debe detectar committed antes de tocar nada!
        prior = check_already_committed(ctx2)
        assert prior is not None
        assert prior["order_id"] == "ORD-1"


# ════════════════════════════════════════════════════════════════════
#  9. PUBLIC API
# ════════════════════════════════════════════════════════════════════
class TestPublicAPI:

    def test_all_exports_present(self):
        import core.observability.bridge as b
        for name in (
            "FlowContext", "start_flow", "check_already_committed",
            "check_orphan_intent", "record_intent", "record_outcome",
            "transition_phase", "record_exception",
            "with_instrumentation", "snapshot",
        ):
            assert hasattr(b, name), f"Missing export: {name}"

    def test_flow_context_is_frozen(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        with pytest.raises((AttributeError, Exception)):
            ctx.slot = 999

    def test_snapshot_has_all_expected_keys(self, _isolated_sinks):
        ctx = _ctx(_isolated_sinks)
        snap = snapshot(ctx)
        assert set(snap.keys()) == {
            "slot", "event_id", "account_id", "seats",
            "idempotency_key", "idempotency_state",
            "cart_state", "created_at",
        }
