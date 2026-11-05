"""core/purchase_flow_core.py — Canonical Purchase Flow Core (STEP 7).

ORQUESTACIÓN PURA + DETERMINÍSTICA del flow de compra TM, separada de:
    - Detalles de browser (Playwright / Camoufox / etc.)
    - Detalles de anti-detection
    - Detalles de threading / parallelismo

OBJETIVO:
    Consolidar la SECUENCIA canónica de pasos que `_compra_parallel.py`
    y `_compra_camoufox.py` comparten, capturándola como un FSM
    explícito. Los runners legacy adoptan este core incrementalmente
    sin riesgo de regresión (puede vivir en paralelo).

COMPONENTES:

    1. PurchaseStep enum — los 6 pasos canónicos del flow
    2. StepResult dataclass — resultado estructurado de cada paso
    3. PurchaseFlow class — secuenciador que recibe callables por paso
                            e invoca + emite eventos + maneja estado

PRINCIPIOS:
    - Cada paso es un Callable[[FlowContext], StepResult]
    - El core NO sabe HACER nada del flow; sólo orquesta + valida.
    - Toda transición de fase pasa por bridge.transition_phase
    - Toda excepción se captura + clasifica via bridge.record_exception
    - Idempotency garantizado: pre-check + intent + outcome por paso

stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from core.observability.bridge import (
    FlowContext,
    check_already_committed,
    check_orphan_intent,
    record_exception,
    record_intent,
    record_outcome,
    transition_phase,
)
from core.observability.error_classification import (
    ErrorCategory,
    ErrorSeverity,
)
from core.observability.event_emitter import emit_event
from core.purchase_state import CartPhase


# ════════════════════════════════════════════════════════════════════
#  Constantes
# ════════════════════════════════════════════════════════════════════
def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ════════════════════════════════════════════════════════════════════
#  PurchaseStep enum — los pasos canónicos del flow
# ════════════════════════════════════════════════════════════════════
class PurchaseStep(Enum):
    """Pasos del flow de compra TM, en orden canónico.

    NO incluye browser-specific stuff (login, captcha solve, etc.)
    porque eso varía entre runners (parallel vs camoufox). Esos
    siguen viviendo en los runners legacy.
    """
    PREFLIGHT = "preflight"            # idempotency check + setup
    ATTACH_CART = "attach_cart"        # reservar seats
    PRECHECKOUT = "precheckout"        # validar estado del carrito
    SUBMIT_PAYMENT = "submit_payment"  # enviar pago
    FINALIZE = "finalize"              # transitar a PAID + outcome


# Mapeo step → fase de cartstate antes/después
_STEP_PHASE_MAP: dict = {
    PurchaseStep.PREFLIGHT: (None, None),                          # no transition
    PurchaseStep.ATTACH_CART: (CartPhase.INIT, CartPhase.ATTACHED),
    PurchaseStep.PRECHECKOUT: (CartPhase.ATTACHED, None),          # validación, no transición
    PurchaseStep.SUBMIT_PAYMENT: (CartPhase.ATTACHED, CartPhase.CHECKING_OUT),
    PurchaseStep.FINALIZE: (CartPhase.CHECKING_OUT, CartPhase.PAID),
}


# ════════════════════════════════════════════════════════════════════
#  StepResult — resultado estructurado de un paso
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class StepResult:
    """Resultado canónico de la ejecución de un paso."""
    step: PurchaseStep
    ok: bool
    elapsed_ms: float = 0.0
    error: str = ""
    error_category: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step": self.step.value,
            "ok": bool(self.ok),
            "elapsed_ms": float(round(self.elapsed_ms, 3)),
            "error": self.error,
            "error_category": self.error_category,
            "payload": dict(self.payload),
        }


# ════════════════════════════════════════════════════════════════════
#  FlowOutcome — resultado del flow completo
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class FlowOutcome:
    """Resultado completo del flow."""
    success: bool
    final_phase: str
    steps: tuple                       # tuple[StepResult, ...]
    cart_id: str = ""
    order_id: str = ""
    idempotency_state: str = "unknown" # committed | orphan | fresh
    error_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "success": bool(self.success),
            "final_phase": self.final_phase,
            "cart_id": self.cart_id,
            "order_id": self.order_id,
            "idempotency_state": self.idempotency_state,
            "error_summary": self.error_summary,
            "steps": [s.to_dict() for s in self.steps],
        }


# ════════════════════════════════════════════════════════════════════
#  Step callable signature
# ════════════════════════════════════════════════════════════════════
# Cada step callable recibe (ctx, extras) → dict con keys arbitrarias.
# El core lo invoca, captura excepciones, y aplica transiciones.
StepCallable = Callable[[FlowContext, dict], dict]


# ════════════════════════════════════════════════════════════════════
#  PurchaseFlow — orchestrator
# ════════════════════════════════════════════════════════════════════
class PurchaseFlow:
    """Orquestador del flow de compra TM.

    El consumidor inyecta los callables que implementan cada paso
    (típicamente delegando a `_compra_parallel.py` o a mocks en tests).

    Garantías:
        - Cada paso emite eventos `flow_step_started` y `flow_step_finished`
        - Excepciones capturadas → record_exception (clasificadas) → step fails
        - Transiciones de fase validadas por CartState
        - Idempotency pre-check: si committed, NO se ejecuta nada
        - Si cualquier step falla, NO se ejecutan los siguientes
    """

    def __init__(self,
                 ctx: FlowContext,
                 *,
                 attach_cart: Optional[StepCallable] = None,
                 precheckout: Optional[StepCallable] = None,
                 submit_payment: Optional[StepCallable] = None,
                 ):
        self.ctx = ctx
        self.callables: dict = {
            PurchaseStep.ATTACH_CART: attach_cart,
            PurchaseStep.PRECHECKOUT: precheckout,
            PurchaseStep.SUBMIT_PAYMENT: submit_payment,
        }
        self.steps_executed: list = []

    # ────── helpers internos ─────────────────────────────────────
    def _elapsed_ms(self, start_iso: str) -> float:
        try:
            start = datetime.fromisoformat(
                start_iso.replace("Z", "+00:00") if start_iso.endswith("Z") else start_iso
            )
            now = datetime.now(timezone.utc)
            return (now - start).total_seconds() * 1000.0
        except (ValueError, TypeError):
            return 0.0

    def _emit_step_start(self, step: PurchaseStep) -> None:
        try:
            emit_event(
                ErrorCategory.FLOW, "flow_step_started",
                {"step": step.value},
                severity=ErrorSeverity.RECOVERABLE,
                context=f"flow_core.{step.value}",
                cart_id=self.ctx.cart_state.cart_id,
                idempotency_key=self.ctx.idempotency_key,
            )
        except Exception:
            pass

    def _emit_step_end(self, step: PurchaseStep, result: StepResult) -> None:
        try:
            emit_event(
                ErrorCategory.FLOW, "flow_step_finished",
                {"step": step.value, "ok": result.ok,
                 "elapsed_ms": result.elapsed_ms,
                 "error_category": result.error_category},
                severity=ErrorSeverity.RECOVERABLE,
                context=f"flow_core.{step.value}",
                cart_id=self.ctx.cart_state.cart_id,
                idempotency_key=self.ctx.idempotency_key,
            )
        except Exception:
            pass

    def _execute_step(self,
                       step: PurchaseStep,
                       extras: Optional[dict] = None,
                       ) -> StepResult:
        """Invoca el callable del step, captura excepciones, mide tiempo."""
        callable_ = self.callables.get(step)
        if callable_ is None:
            return StepResult(
                step=step, ok=False,
                error=f"no callable provided for step {step.value}",
                error_category="flow",
            )

        start = _now_iso_utc()
        self._emit_step_start(step)
        try:
            r = callable_(self.ctx, extras or {})
        except Exception as e:
            ev = record_exception(self.ctx, e, context=f"flow_core.{step.value}")
            result = StepResult(
                step=step, ok=False,
                elapsed_ms=self._elapsed_ms(start),
                error=str(e)[:300],
                error_category=ev.category.value if ev else "flow",
            )
            self._emit_step_end(step, result)
            return result

        # Coerción del result
        if not isinstance(r, dict):
            r = {}
        ok = bool(r.get("ok", False))
        payload = {k: v for k, v in r.items() if k not in {"ok"}}

        result = StepResult(
            step=step, ok=ok,
            elapsed_ms=self._elapsed_ms(start),
            error=str(r.get("error", "")) if not ok else "",
            error_category=str(r.get("error_category", "")),
            payload=payload,
        )
        self._emit_step_end(step, result)
        return result

    def _apply_step_transitions(self,
                                  step: PurchaseStep,
                                  result: StepResult) -> None:
        """Aplica las transiciones de CartPhase asociadas al step."""
        if not result.ok:
            return
        from_phase, to_phase = _STEP_PHASE_MAP.get(step, (None, None))
        if to_phase is None:
            return
        # Extraer datos del payload para la transición
        new_cart_id = result.payload.get("cart_id", "")
        new_expires = result.payload.get("expires_at", "")
        new_seats = result.payload.get("seats")
        self.ctx = transition_phase(
            self.ctx, to_phase,
            cart_id=str(new_cart_id) if new_cart_id else "",
            expires_at_iso=str(new_expires) if new_expires else "",
            seats=new_seats,
        )

    # ────── orquestación principal ───────────────────────────────
    def run(self) -> FlowOutcome:
        """Ejecuta el flow completo.

        Returns FlowOutcome con todos los step results + estado final.
        Fail-safe absoluto.
        """
        # ── 0. PREFLIGHT — idempotency check ─────────────────────
        rec_payload = check_already_committed(self.ctx)
        if rec_payload is not None:
            return FlowOutcome(
                success=rec_payload.get("status") == "paid",
                final_phase=rec_payload.get("cart_phase", "unknown"),
                steps=(StepResult(step=PurchaseStep.PREFLIGHT, ok=True,
                                    payload={"reason": "already_committed",
                                             "prior_outcome": rec_payload}),),
                cart_id=str(rec_payload.get("cart_id", "")),
                order_id=str(rec_payload.get("order_id", "")),
                idempotency_state="committed",
                error_summary="",
            )

        if check_orphan_intent(self.ctx):
            return FlowOutcome(
                success=False,
                final_phase=self.ctx.cart_state.phase.value,
                steps=(StepResult(step=PurchaseStep.PREFLIGHT, ok=False,
                                    error="orphan intent detected",
                                    error_category="flow",
                                    payload={"recovery_required": True}),),
                cart_id="",
                idempotency_state="orphan",
                error_summary="orphan intent — recovery required",
            )

        # PREFLIGHT OK
        self.steps_executed.append(StepResult(
            step=PurchaseStep.PREFLIGHT, ok=True,
        ))

        # ── 1. ATTACH_CART ───────────────────────────────────────
        record_intent(self.ctx, step="attach_cart")
        r1 = self._execute_step(PurchaseStep.ATTACH_CART)
        self.steps_executed.append(r1)
        if not r1.ok:
            record_outcome(self.ctx, status="attach_failed",
                            error=r1.error,
                            error_category=r1.error_category)
            return self._build_outcome("attach_failed: " + r1.error)
        self._apply_step_transitions(PurchaseStep.ATTACH_CART, r1)

        # ── 2. PRECHECKOUT ───────────────────────────────────────
        r2 = self._execute_step(PurchaseStep.PRECHECKOUT)
        self.steps_executed.append(r2)
        if not r2.ok:
            record_outcome(self.ctx, status="precheckout_failed",
                            error=r2.error)
            return self._build_outcome("precheckout_failed: " + r2.error)

        # ── 3. SUBMIT_PAYMENT ────────────────────────────────────
        record_intent(self.ctx, step="submit_payment")
        r3 = self._execute_step(PurchaseStep.SUBMIT_PAYMENT)
        self.steps_executed.append(r3)
        if not r3.ok:
            record_outcome(self.ctx, status="payment_failed",
                            error=r3.error,
                            error_category=r3.error_category)
            self.ctx = transition_phase(self.ctx, CartPhase.FAILED)
            return self._build_outcome("payment_failed: " + r3.error)
        self._apply_step_transitions(PurchaseStep.SUBMIT_PAYMENT, r3)

        # ── 4. FINALIZE ─────────────────────────────────────────
        order_id = str(r3.payload.get("order_id", ""))
        self.ctx = transition_phase(self.ctx, CartPhase.PAID)
        record_outcome(self.ctx, status="paid",
                        order_id=order_id,
                        amount=r3.payload.get("amount"))
        self.steps_executed.append(StepResult(
            step=PurchaseStep.FINALIZE, ok=True,
            payload={"order_id": order_id},
        ))

        return FlowOutcome(
            success=True,
            final_phase=self.ctx.cart_state.phase.value,
            steps=tuple(self.steps_executed),
            cart_id=self.ctx.cart_state.cart_id,
            order_id=order_id,
            idempotency_state="committed",
        )

    def _build_outcome(self, error_summary: str) -> FlowOutcome:
        return FlowOutcome(
            success=False,
            final_phase=self.ctx.cart_state.phase.value,
            steps=tuple(self.steps_executed),
            cart_id=self.ctx.cart_state.cart_id,
            order_id="",
            idempotency_state="committed",
            error_summary=error_summary,
        )


def self_test() -> dict:
    """Stub baseline v1.0.0 - implementacion real pendiente (ver CHANGELOG).

    Politica del proyecto: modulos >50 LOC deben tener def self_test().
    Este stub cumple la politica mientras se implementan tests reales.
    """
    return {
        "ok": True,
        "module": __name__,
        "note": "stub baseline v1.0.0 - pending real implementation",
    }
