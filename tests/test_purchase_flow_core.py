"""tests/test_purchase_flow_core.py — Test suite for STEP 7.

Cubre el orchestrator canónico que consolida la secuencia de pasos
compartida entre `_compra_parallel.py` y `_compra_camoufox.py`.

Categorías:
    1. Step result construction      (3 tests)
    2. Happy path                    (3 tests)
    3. Step failures                 (5 tests)
    4. Idempotency integration       (4 tests)
    5. Phase transitions             (4 tests)
    6. Exception capture             (3 tests)
    7. FlowOutcome serialization     (3 tests)

stdlib + pytest only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ_ROOT = Path(__file__).resolve().parents[1]
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


from core.observability.bridge import (
    FlowContext,
    start_flow,
)
from core.observability.event_emitter import (
    clear_sinks,
    set_sink_path,
)
from core.purchase_flow_core import (
    FlowOutcome,
    PurchaseFlow,
    PurchaseStep,
    StepResult,
)
from core.purchase_state import CartPhase


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    set_sink_path(tmp_path / "events.jsonl")
    clear_sinks()
    yield {
        "idem": str(tmp_path / "idem"),
    }
    clear_sinks()
    set_sink_path(None)


def _ctx(_isolated, **overrides) -> FlowContext:
    defaults = dict(
        slot=1, event_id="E1", account_id="A1", seats=["S1", "S2"],
        idem_root=_isolated["idem"],
    )
    defaults.update(overrides)
    return start_flow(**defaults)


def _ok_attach(ctx, extras):
    return {
        "ok": True, "cart_id": "CART-X",
        "expires_at": "2026-05-14T11:00:00+00:00",
        "seats": ["S1", "S2"],
    }


def _ok_precheck(ctx, extras):
    return {"ok": True}


def _ok_pay(ctx, extras):
    return {"ok": True, "order_id": "ORD-1", "amount": 100.0}


# ════════════════════════════════════════════════════════════════════
#  1. STEP RESULT CONSTRUCTION
# ════════════════════════════════════════════════════════════════════
class TestStepResultConstruction:

    def test_step_result_is_frozen(self):
        sr = StepResult(step=PurchaseStep.ATTACH_CART, ok=True)
        with pytest.raises((AttributeError, Exception)):
            sr.ok = False

    def test_step_result_to_dict(self):
        sr = StepResult(step=PurchaseStep.ATTACH_CART, ok=True,
                         elapsed_ms=12.5,
                         payload={"cart_id": "CART-1"})
        d = sr.to_dict()
        assert d["step"] == "attach_cart"
        assert d["ok"] is True
        assert d["payload"] == {"cart_id": "CART-1"}

    def test_step_enum_canonical(self):
        steps = [s.value for s in PurchaseStep]
        assert steps == ["preflight", "attach_cart", "precheckout",
                          "submit_payment", "finalize"]


# ════════════════════════════════════════════════════════════════════
#  2. HAPPY PATH
# ════════════════════════════════════════════════════════════════════
class TestHappyPath:

    def test_full_flow_succeeds(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.success is True
        assert outcome.final_phase == "paid"
        assert outcome.order_id == "ORD-1"

    def test_all_five_steps_executed(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        steps = [s.step.value for s in outcome.steps]
        assert steps == ["preflight", "attach_cart", "precheckout",
                          "submit_payment", "finalize"]

    def test_idem_state_committed_after_success(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.idempotency_state == "committed"


# ════════════════════════════════════════════════════════════════════
#  3. STEP FAILURES
# ════════════════════════════════════════════════════════════════════
class TestStepFailures:

    def test_attach_failure_stops_flow(self, _isolated):
        ctx = _ctx(_isolated)
        def bad_attach(c, e):
            return {"ok": False, "error": "inventory_changed",
                    "error_category": "inventory"}
        flow = PurchaseFlow(
            ctx, attach_cart=bad_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.success is False
        steps = [s.step.value for s in outcome.steps]
        # No debe llegar a precheckout
        assert "precheckout" not in steps
        assert "submit_payment" not in steps

    def test_precheckout_failure(self, _isolated):
        ctx = _ctx(_isolated)
        def bad_precheck(c, e):
            return {"ok": False, "error": "cart_stale"}
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=bad_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.success is False
        steps = [s.step.value for s in outcome.steps]
        assert "submit_payment" not in steps

    def test_payment_failure_transitions_to_failed(self, _isolated):
        ctx = _ctx(_isolated)
        def bad_pay(c, e):
            return {"ok": False, "error": "card_declined",
                    "error_category": "payment"}
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=bad_pay,
        )
        outcome = flow.run()
        assert outcome.success is False
        assert outcome.final_phase == "failed"

    def test_missing_callable_fails_safely(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(ctx)  # sin callables
        outcome = flow.run()
        assert outcome.success is False

    def test_error_summary_propagated(self, _isolated):
        ctx = _ctx(_isolated)
        def bad_pay(c, e):
            return {"ok": False, "error": "iframe missing"}
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=bad_pay,
        )
        outcome = flow.run()
        assert "iframe missing" in outcome.error_summary


# ════════════════════════════════════════════════════════════════════
#  4. IDEMPOTENCY INTEGRATION
# ════════════════════════════════════════════════════════════════════
class TestIdempotencyIntegration:

    def test_replay_after_success_skips(self, _isolated):
        ctx = _ctx(_isolated)
        # Primer run completo
        flow1 = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        flow1.run()

        # Segundo run con misma key — debe ser short-circuit
        ctx2 = _ctx(_isolated)
        called: list = []
        def tracked_attach(c, e):
            called.append("attach")
            return _ok_attach(c, e)
        flow2 = PurchaseFlow(
            ctx2, attach_cart=tracked_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow2.run()
        assert outcome.idempotency_state == "committed"
        assert outcome.success is True
        # attach_cart NO se llamó (short-circuit)
        assert called == []

    def test_orphan_intent_blocks_flow(self, _isolated):
        # Crear orphan manualmente
        from core.idempotency import write_intent, make_idempotency_key
        ctx = _ctx(_isolated)
        # El idempotency_key ya está en ctx; escribimos intent sin outcome
        write_intent(ctx.idempotency_key, {"step": "attach_cart"},
                      root=ctx.idem_root)

        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.success is False
        assert outcome.idempotency_state == "orphan"
        assert "orphan" in outcome.error_summary.lower()

    def test_no_repeated_payment_on_committed(self, _isolated):
        ctx = _ctx(_isolated)
        called: list = []
        def tracked_pay(c, e):
            called.append("pay")
            return _ok_pay(c, e)
        PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=tracked_pay,
        ).run()
        assert called == ["pay"]

        # Segundo intento
        ctx2 = _ctx(_isolated)
        PurchaseFlow(
            ctx2, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=tracked_pay,
        ).run()
        # NO se llamó otra vez
        assert called == ["pay"]

    def test_preflight_is_first_step(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.steps[0].step == PurchaseStep.PREFLIGHT


# ════════════════════════════════════════════════════════════════════
#  5. PHASE TRANSITIONS
# ════════════════════════════════════════════════════════════════════
class TestPhaseTransitions:

    def test_attach_transitions_to_attached(self, _isolated):
        ctx = _ctx(_isolated)
        # Sólo attach + precheckout que falla en mitad
        def precheck_fail(c, e):
            return {"ok": False, "error": "stale"}
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=precheck_fail, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        # CartState debe haber pasado por attached antes del fallo
        # final_phase = "attached" porque precheckout NO transiciona
        assert outcome.final_phase == "attached"

    def test_payment_transitions_to_checking_out(self, _isolated):
        ctx = _ctx(_isolated)
        # payment fail → transición a FAILED desde CHECKING_OUT
        def pay_fail(c, e):
            return {"ok": False, "error": "x"}
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=pay_fail,
        )
        outcome = flow.run()
        assert outcome.final_phase == "failed"

    def test_finalize_transitions_to_paid(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.final_phase == "paid"

    def test_cart_id_assigned_after_attach(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.cart_id == "CART-X"


# ════════════════════════════════════════════════════════════════════
#  6. EXCEPTION CAPTURE
# ════════════════════════════════════════════════════════════════════
class TestExceptionCapture:

    def test_callable_raising_caught(self, _isolated):
        ctx = _ctx(_isolated)
        def raising_attach(c, e):
            raise ConnectionError("proxy refused")
        flow = PurchaseFlow(
            ctx, attach_cart=raising_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        assert outcome.success is False

    def test_exception_classified_as_network(self, _isolated):
        ctx = _ctx(_isolated)
        def raising(c, e):
            raise ConnectionError("proxy down")
        flow = PurchaseFlow(
            ctx, attach_cart=raising,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        attach_result = [s for s in outcome.steps
                          if s.step == PurchaseStep.ATTACH_CART][0]
        assert attach_result.ok is False
        assert attach_result.error_category == "network"

    def test_flow_never_propagates_exception(self, _isolated):
        ctx = _ctx(_isolated)
        def crashes(c, e):
            raise RuntimeError("kaboom")
        flow = PurchaseFlow(
            ctx, attach_cart=crashes,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        # NO debe propagar
        outcome = flow.run()
        assert outcome.success is False


# ════════════════════════════════════════════════════════════════════
#  7. FlowOutcome SERIALIZATION
# ════════════════════════════════════════════════════════════════════
class TestFlowOutcomeSerialization:

    def test_outcome_is_frozen(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        outcome = flow.run()
        with pytest.raises((AttributeError, Exception)):
            outcome.success = False

    def test_outcome_to_dict(self, _isolated):
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        d = flow.run().to_dict()
        assert set(d.keys()) >= {
            "success", "final_phase", "cart_id", "order_id",
            "idempotency_state", "error_summary", "steps",
        }

    def test_outcome_json_serializable(self, _isolated):
        import json
        ctx = _ctx(_isolated)
        flow = PurchaseFlow(
            ctx, attach_cart=_ok_attach,
            precheckout=_ok_precheck, submit_payment=_ok_pay,
        )
        s = json.dumps(flow.run().to_dict())
        assert isinstance(s, str) and len(s) > 0
